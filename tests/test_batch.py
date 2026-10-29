import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from poi_service.batch import attach, collect, item_status, release_uncertain, status, submit, write_requests
from poi_service.settings import Settings


def test_batch_requests_record_embedding_spec_and_stable_ids(tmp_path: Path) -> None:
    db = tmp_path / "stage.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE pois (nf_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE embeddings (nf_id TEXT PRIMARY KEY, vector TEXT NOT NULL)")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO pois VALUES (?,?)",
            (
                "NF1",
                json.dumps({"name": "식당", "aliases": "별칭", "category_name": "음식", "address": "서울"}),
            ),
        )
    settings = Settings(openai_api_key="test")
    paths = write_requests(db, tmp_path / "batch", "run-001", settings)
    assert len(paths) == 1
    line = json.loads(paths[0].read_text(encoding="utf-8"))
    assert line["custom_id"] == "NF1"
    assert line["body"]["dimensions"] == 512
    assert "별칭" in line["body"]["input"]
    with sqlite3.connect(db) as connection:
        spec = json.loads(connection.execute("SELECT value FROM metadata").fetchone()[0])
    assert spec["text_version"] == "poi-v1"
    assert write_requests(db, tmp_path / "batch", "run-001", settings) == paths


def test_partial_batch_resume_uses_only_missing_ids(tmp_path: Path) -> None:
    db = tmp_path / "stage.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE pois (nf_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE embeddings (nf_id TEXT PRIMARY KEY, vector TEXT NOT NULL)")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for nf_id in ("NF1", "NF2"):
            connection.execute(
                "INSERT INTO pois VALUES (?,?)",
                (
                    nf_id,
                    json.dumps({"name": nf_id, "aliases": "", "category_name": "음식", "address": "서울"}),
                ),
            )

    # The API is mocked, and no embedding vector is sent to Milvus.
    settings = Settings(openai_api_key="test", embedding_dimensions=0)
    root = tmp_path / "runs"
    paths = write_requests(db, root, "run-001", settings, max_rows=2)

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.files = SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(id="file-input"),
                with_streaming_response=SimpleNamespace(content=self.content),
            )
            self.batches = SimpleNamespace(create=self.create, retrieve=self.retrieve)

        def content(self, file_id):
            payloads = {
                "file-output": json.dumps(
                    {
                        "custom_id": "NF1",
                        "response": {"status_code": 200, "body": {"data": [{"embedding": []}]}},
                    }
                )
                + "\n",
                "file-error": json.dumps({"custom_id": "NF2", "error": {"code": "batch_expired"}}) + "\n",
            }

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def stream_to_file(self, path):
                    Path(path).write_text(payloads[file_id], encoding="utf-8")

            return Response()

        def create(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(id="batch-001")

        def retrieve(self, batch_id):
            return SimpleNamespace(
                id=batch_id,
                input_file_id="file-input",
                status="expired",
                output_file_id="file-output",
                error_file_id="file-error",
            )

    client = FakeClient()
    with pytest.raises(ValueError, match="unfinished embedding run"):
        write_requests(db, root, "run-002", settings)
    submit(root, "run-001", settings, external_transfer_authorized=True, client=client)
    assert client.calls == 1
    assert (
        submit(root, "run-001", settings, external_transfer_authorized=True, client=client)["newly_submitted_shards"]
        == 0
    )
    assert client.calls == 1

    report = collect(db, root, "run-001", settings, client=client)
    assert (paths[0].parent / f"{paths[0].name}.output.jsonl").exists()
    assert report["embedded"] == 1
    assert report["missing"] == 1
    assert report["errors"] == 1
    assert status(db, root, "run-001")["remaining"] == 1
    assert item_status(db, "NF1")["embedded"] is True
    assert item_status(db, "NF2")["embedded"] is False
    second = write_requests(db, root, "run-002", settings)
    assert [json.loads(line)["custom_id"] for line in second[0].read_text().splitlines()] == ["NF2"]


def test_uncertain_submission_does_not_create_second_batch(tmp_path: Path) -> None:
    db = tmp_path / "stage.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE pois (nf_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE embeddings (nf_id TEXT PRIMARY KEY, vector TEXT NOT NULL)")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO pois VALUES (?,?)",
            ("NF1", json.dumps({"name": "식당", "aliases": "", "category_name": "음식", "address": "서울"})),
        )
    settings = Settings(openai_api_key="test")
    root = tmp_path / "runs"
    write_requests(db, root, "run-001", settings)

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.files = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(id="file-input"))
            self.batches = SimpleNamespace(create=self.create, retrieve=self.retrieve)

        def create(self, **kwargs):
            self.calls += 1
            raise ConnectionError("response lost")

        def retrieve(self, batch_id):
            return SimpleNamespace(id=batch_id, input_file_id="file-input")

    client = FakeClient()
    with pytest.raises(ConnectionError):
        submit(root, "run-001", settings, external_transfer_authorized=True, client=client)
    with pytest.raises(RuntimeError, match="uncertain"):
        submit(root, "run-001", settings, external_transfer_authorized=True, client=client)
    assert client.calls == 1
    with pytest.raises(ValueError, match="confirm"):
        release_uncertain(root, "run-001", "embeddings-00000.jsonl", settings, False)
    assert (
        attach(root, "run-001", "embeddings-00000.jsonl", "batch-existing", settings, client)["batch_id"]
        == "batch-existing"
    )
