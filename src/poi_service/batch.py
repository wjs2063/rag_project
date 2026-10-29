"""Cost-conscious, resumable OpenAI Batch embedding runs."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from openai import OpenAI

from poi_service.settings import Settings

MAX_REQUESTS = 40_000
MAX_BYTES = 150 * 1024 * 1024
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
TERMINAL = {"completed", "failed", "expired", "cancelled"}


def _run_path(root: Path, run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must contain 1-64 ASCII letters, digits, '_' or '-'")
    return root / run_id


def _specification(settings: Settings) -> dict:
    return {
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
        "text_version": settings.embedding_text_version,
    }


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _verify_shard(run: Path, shard: dict) -> None:
    if _sha256(run / shard["file"]) != shard["sha256"]:
        raise ValueError(f"embedding request file changed: {shard['file']}")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    temporary.replace(path)


@contextmanager
def _root_lock(root: Path) -> Iterator[None]:
    """Serialize local run operations so two CLI processes cannot reserve the same POI."""
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load_run(root: Path, run_id: str) -> dict:
    return json.loads((_run_path(root, run_id) / "run.json").read_text(encoding="utf-8"))


def _save_run(root: Path, state: dict) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    _atomic_json(_run_path(root, state["run_id"]) / "run.json", state)


def _check_spec(connection: sqlite3.Connection, settings: Settings, *, create: bool) -> None:
    expected = _specification(settings)
    row = connection.execute("SELECT value FROM metadata WHERE key='embedding_spec'").fetchone()
    if row and json.loads(row[0]) != expected:
        raise ValueError("staging DB uses a different embedding specification")
    if not row and not create:
        raise ValueError("staging DB has no embedding specification")
    if create and not row:
        connection.execute("INSERT INTO metadata VALUES ('embedding_spec', ?)", (json.dumps(expected),))


def _assert_no_unfinished_runs(root: Path, except_run_id: str) -> None:
    if (root / "jobs.json").exists():
        raise ValueError("legacy Batch jobs exist in this directory; collect them before starting a run_id")
    for path in root.glob("*/run.json"):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["run_id"] != except_run_id and any(
            shard["status"] != "collected" for shard in state["shards"]
        ):
            raise ValueError(f"unfinished embedding run: {state['run_id']}")


def write_requests(
    staging_db: Path, root: Path, run_id: str, settings: Settings, max_rows: int = MAX_REQUESTS
) -> list[Path]:
    """Create one immutable run from POIs without an embedding checkpoint."""
    if not 1 <= max_rows <= 1_000_000:
        raise ValueError("max_rows must be between 1 and 1,000,000")
    run = _run_path(root, run_id)
    with _root_lock(root):
        if run.exists():
            state = _load_run(root, run_id)
            if state["staging_db"] != str(staging_db.resolve()) or state["specification"] != _specification(
                settings
            ):
                raise ValueError("run_id belongs to a different staging DB or embedding specification")
            return [run / shard["file"] for shard in state["shards"]]
        _assert_no_unfinished_runs(root, run_id)
        with sqlite3.connect(staging_db) as connection:
            _check_spec(connection, settings, create=True)
            connection.commit()
            rows = connection.execute(
                """
                SELECT p.nf_id,p.payload FROM pois p
                LEFT JOIN embeddings e ON e.nf_id=p.nf_id
                WHERE e.nf_id IS NULL ORDER BY p.nf_id LIMIT ?
            """,
                (max_rows,),
            )
            with tempfile.TemporaryDirectory(prefix=f".run-{run_id}-", dir=root) as temporary:
                temporary_path = Path(temporary)
                shards: list[dict] = []
                output = None
                size = count = total = 0
                try:
                    for nf_id, payload in rows:
                        row = json.loads(payload)
                        embedding_text = (
                            f"이름: {row['name']}; 별칭: {row['aliases']}; "
                            f"분류: {row['category_name']}; 주소: {row['address']}"
                        )
                        request = {
                            "custom_id": nf_id,
                            "method": "POST",
                            "url": "/v1/embeddings",
                            "body": {
                                "model": settings.embedding_model,
                                "dimensions": settings.embedding_dimensions,
                                "input": embedding_text,
                            },
                        }
                        line = (
                            json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                            + b"\n"
                        )
                        if len(line) > MAX_BYTES:
                            raise ValueError(f"single request too large: {nf_id}")
                        if output is None or count >= MAX_REQUESTS or size + len(line) > MAX_BYTES:
                            if output:
                                output.close()
                            name = f"embeddings-{len(shards):05d}.jsonl"
                            shards.append(
                                {
                                    "file": name,
                                    "rows": 0,
                                    "status": "prepared",
                                    "input_file_id": None,
                                    "batch_id": None,
                                    "output_file_id": None,
                                    "error_file_id": None,
                                }
                            )
                            output = (temporary_path / name).open("wb")
                            size = count = 0
                        output.write(line)
                        count += 1
                        size += len(line)
                        total += 1
                        shards[-1]["rows"] += 1
                finally:
                    if output:
                        output.close()
                if not total:
                    raise ValueError("all POIs already have embeddings")
                for shard in shards:
                    shard["sha256"] = _sha256(temporary_path / shard["file"])
                state = {
                    "run_id": run_id,
                    "staging_db": str(staging_db.resolve()),
                    "specification": _specification(settings),
                    "requested_rows": total,
                    "shards": shards,
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
                _atomic_json(temporary_path / "run.json", state)
                temporary_path.rename(run)
        return [run / shard["file"] for shard in shards]


def _validate_run(state: dict, settings: Settings) -> None:
    if state["specification"] != _specification(settings):
        raise ValueError("run embedding specification differs from current settings")


def submit(
    root: Path,
    run_id: str,
    settings: Settings,
    external_transfer_authorized: bool,
    max_shards: int = 1,
    client: OpenAI | None = None,
) -> dict:
    """Submit a bounded number of shards; pause on ambiguous create outcomes."""
    if not external_transfer_authorized:
        raise ValueError("confirm that source terms permit sending POI text to OpenAI")
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY (or POI_OPENAI_API_KEY) is required")
    if max_shards < 1:
        raise ValueError("max_shards must be positive")
    with _root_lock(root):
        state = _load_run(root, run_id)
        _validate_run(state, settings)
        with sqlite3.connect(state["staging_db"]) as connection:
            _check_spec(connection, settings, create=False)
        own_client = client is None
        client = client or OpenAI(api_key=settings.openai_api_key, max_retries=0)
        submitted = 0
        try:
            for shard in state["shards"]:
                if submitted >= max_shards:
                    break
                _verify_shard(_run_path(root, run_id), shard)
                if shard["status"] == "creating":
                    raise RuntimeError(
                        f"submission outcome is uncertain for {run_id}/{shard['file']}; "
                        "use batch-attach with the existing batch ID after checking OpenAI"
                    )
                if shard["status"] not in {"prepared", "uploading", "uploaded"}:
                    continue
                if shard["status"] in {"prepared", "uploading"}:
                    shard["status"] = "uploading"
                    _save_run(root, state)
                    with (_run_path(root, run_id) / shard["file"]).open("rb") as source:
                        uploaded = client.files.create(file=source, purpose="batch")
                    shard["input_file_id"] = uploaded.id
                    shard["status"] = "uploaded"
                    _save_run(root, state)
                shard["status"] = "creating"
                _save_run(root, state)
                batch = client.batches.create(
                    input_file_id=shard["input_file_id"],
                    endpoint="/v1/embeddings",
                    completion_window="24h",
                    metadata={"run_id": run_id, "source_file": shard["file"]},
                )
                shard["batch_id"] = batch.id
                shard["status"] = "submitted"
                _save_run(root, state)
                submitted += 1
        finally:
            if own_client:
                client.close()
        return {"run_id": run_id, "newly_submitted_shards": submitted, "shards": state["shards"]}


def attach(
    root: Path, run_id: str, file: str, batch_id: str, settings: Settings, client: OpenAI | None = None
) -> dict:
    """Resolve an ambiguous submission by verifying a known remote batch ID."""
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY (or POI_OPENAI_API_KEY) is required")
    with _root_lock(root):
        state = _load_run(root, run_id)
        _validate_run(state, settings)
        shard = next((item for item in state["shards"] if item["file"] == file), None)
        if not shard or shard["status"] != "creating":
            raise ValueError("shard is not awaiting batch reconciliation")
        own_client = client is None
        client = client or OpenAI(api_key=settings.openai_api_key, max_retries=0)
        try:
            batch = client.batches.retrieve(batch_id)
        finally:
            if own_client:
                client.close()
        if batch.input_file_id != shard["input_file_id"]:
            raise ValueError("batch input file does not match this run's uploaded file")
        shard["batch_id"] = batch.id
        shard["status"] = "submitted"
        _save_run(root, state)
        return {"run_id": run_id, "file": file, "batch_id": batch.id}


def release_uncertain(
    root: Path, run_id: str, file: str, settings: Settings, confirmed_no_batch: bool
) -> dict:
    """Release a create attempt only after an operator confirms no remote batch exists."""
    if not confirmed_no_batch:
        raise ValueError("confirm that no OpenAI Batch exists for this uploaded input file")
    with _root_lock(root):
        state = _load_run(root, run_id)
        _validate_run(state, settings)
        shard = next((item for item in state["shards"] if item["file"] == file), None)
        if not shard or shard["status"] != "creating" or shard["batch_id"]:
            raise ValueError("shard is not awaiting batch reconciliation")
        shard["status"] = "uploaded"
        _save_run(root, state)
        return {"run_id": run_id, "file": file, "status": "uploaded", "input_file_id": shard["input_file_id"]}


def status(staging_db: Path, root: Path, run_id: str) -> dict:
    """Read durable local progress without making an API request."""
    with _root_lock(root):
        state = _load_run(root, run_id)
        if state["staging_db"] != str(staging_db.resolve()):
            raise ValueError("run_id belongs to a different staging DB")
        with sqlite3.connect(staging_db) as connection:
            expected = connection.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
            embedded = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        return {
            "run_id": run_id,
            "requested_rows": state["requested_rows"],
            "expected": expected,
            "embedded": embedded,
            "remaining": expected - embedded,
            "shards": [
                {
                    "file": shard["file"],
                    "status": shard["status"],
                    "batch_id": shard["batch_id"],
                    "remote_status": shard.get("remote_status"),
                }
                for shard in state["shards"]
            ],
        }


def item_status(staging_db: Path, nf_id: str) -> dict:
    """Check whether a specific source POI has a persisted embedding."""
    with sqlite3.connect(staging_db) as connection:
        found = connection.execute("SELECT 1 FROM pois WHERE nf_id=?", (nf_id,)).fetchone() is not None
        embedded = (
            connection.execute("SELECT 1 FROM embeddings WHERE nf_id=?", (nf_id,)).fetchone() is not None
        )
    return {"nf_id": nf_id, "found": found, "embedded": embedded}


def _download(client: OpenAI, file_id: str, path: Path) -> None:
    if path.exists():
        return
    temporary = path.with_suffix(path.suffix + ".partial")
    with client.files.with_streaming_response.content(file_id) as response:
        response.stream_to_file(temporary)
    temporary.replace(path)


def _collect_shard(
    connection: sqlite3.Connection, run: Path, shard: dict, settings: Settings
) -> tuple[int, int, int]:
    with (run / shard["file"]).open(encoding="utf-8") as source:
        allowed = {json.loads(line)["custom_id"] for line in source}
    inserted = successes = errors = 0
    output_path = run / f"{shard['file']}.output.jsonl"
    error_path = run / f"{shard['file']}.errors.jsonl"
    if output_path.exists():
        with output_path.open(encoding="utf-8") as source:
            for line in source:
                item = json.loads(line)
                nf_id = item.get("custom_id")
                if (
                    nf_id not in allowed
                    or item.get("error")
                    or (item.get("response") or {}).get("status_code") != 200
                ):
                    errors += 1
                    continue
                try:
                    vector = item["response"]["body"]["data"][0]["embedding"]
                except (KeyError, IndexError, TypeError):
                    errors += 1
                    continue
                if len(vector) != settings.embedding_dimensions:
                    errors += 1
                    continue
                successes += 1
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO embeddings SELECT ?,? WHERE EXISTS (SELECT 1 FROM pois WHERE nf_id=?)",
                    (nf_id, json.dumps(vector), nf_id),
                )
                inserted += cursor.rowcount
    if error_path.exists():
        with error_path.open(encoding="utf-8") as source:
            errors += sum(1 for _ in source)
    return inserted, successes, errors


def collect(
    staging_db: Path, root: Path, run_id: str, settings: Settings, client: OpenAI | None = None
) -> dict:
    """Persist partial successes from every terminal batch; safe to rerun after a crash."""
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY (or POI_OPENAI_API_KEY) is required")
    with _root_lock(root):
        state = _load_run(root, run_id)
        _validate_run(state, settings)
        if state["staging_db"] != str(staging_db.resolve()):
            raise ValueError("run_id belongs to a different staging DB")
        run = _run_path(root, run_id)
        own_client = client is None
        client = client or OpenAI(api_key=settings.openai_api_key, max_retries=0)
        report = {"run_id": run_id, "new_embeddings": 0, "run_successes": 0, "errors": 0, "shards": []}
        try:
            with sqlite3.connect(staging_db) as connection:
                _check_spec(connection, settings, create=False)
                for shard in state["shards"]:
                    _verify_shard(run, shard)
                    if shard["status"] == "collected":
                        report["run_successes"] += shard.get("successful_results", 0)
                        report["errors"] += shard.get("errors", 0)
                        report["shards"].append({"file": shard["file"], "status": "collected"})
                        continue
                    if not shard["batch_id"]:
                        report["shards"].append({"file": shard["file"], "status": shard["status"]})
                        continue
                    batch = client.batches.retrieve(shard["batch_id"])
                    if batch.status not in TERMINAL:
                        report["shards"].append({"file": shard["file"], "status": batch.status})
                        continue
                    shard["remote_status"] = batch.status
                    shard["output_file_id"] = batch.output_file_id
                    shard["error_file_id"] = batch.error_file_id
                    _save_run(root, state)
                    completed_count = getattr(getattr(batch, "request_counts", None), "completed", 0) or 0
                    if completed_count and not batch.output_file_id:
                        raise RuntimeError(
                            f"batch {batch.id} reports {completed_count} completed requests but has no output file"
                        )
                    if batch.output_file_id:
                        _download(client, batch.output_file_id, run / f"{shard['file']}.output.jsonl")
                    if batch.error_file_id:
                        _download(client, batch.error_file_id, run / f"{shard['file']}.errors.jsonl")
                    inserted, successes, errors = _collect_shard(connection, run, shard, settings)
                    connection.commit()
                    shard["new_embeddings"] = inserted
                    shard["successful_results"] = successes
                    shard["errors"] = errors
                    shard["status"] = "collected"
                    _save_run(root, state)
                    report["new_embeddings"] += inserted
                    report["run_successes"] += successes
                    report["errors"] += errors
                    report["shards"].append(
                        {
                            "file": shard["file"],
                            "status": batch.status,
                            "new_embeddings": inserted,
                            "errors": errors,
                        }
                    )
                report["expected"] = connection.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
                report["embedded"] = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                report["missing"] = report["expected"] - report["embedded"]
        finally:
            if own_client:
                client.close()
        _atomic_json(run / "collection-report.json", report)
        return report
