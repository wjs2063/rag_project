"""Streaming export to Milvus bulk files and checked import jobs."""

import json
import sqlite3
from pathlib import Path

from pymilvus.bulk_writer import BulkFileType, RemoteBulkWriter, bulk_import, get_import_progress
from pymilvus.client.types import LoadState

from poi_service.milvus_store import build_schema, search_filter, sync_client
from poi_service.settings import Settings


def export_bulk(staging_db: Path, output_path: Path, settings: Settings) -> list[list[str]]:
    """Write Parquet chunks directly to configured object storage; require complete embeddings."""
    if not settings.minio_access_key or not settings.minio_secret_key:
        raise ValueError("POI_MINIO_ACCESS_KEY and POI_MINIO_SECRET_KEY are required")
    with sqlite3.connect(staging_db) as connection:
        specification = connection.execute("SELECT value FROM metadata WHERE key='embedding_spec'").fetchone()
        expected_specification = {
            "model": settings.embedding_model,
            "dimensions": settings.embedding_dimensions,
            "text_version": settings.embedding_text_version,
        }
        if not specification or json.loads(specification[0]) != expected_specification:
            raise ValueError("staging embedding specification differs from current settings")
        expected = connection.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
        embedded = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        if not expected or embedded != expected:
            raise ValueError(f"incomplete embeddings: {embedded}/{expected}")
        conn = RemoteBulkWriter.S3ConnectParam(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket_name=settings.minio_bucket,
            secure=settings.minio_secure,
        )
        writer = RemoteBulkWriter(
            schema=build_schema(settings.embedding_dimensions),
            remote_path="/poi-import",
            connect_param=conn,
            file_type=BulkFileType.PARQUET,
            chunk_size=256 * 1024 * 1024,
        )
        count = 0
        for payload, vector in connection.execute(
            "SELECT p.payload,e.vector FROM pois p JOIN embeddings e ON p.nf_id=e.nf_id ORDER BY p.nf_id"
        ):
            row = json.loads(payload)
            row["embedding"] = json.loads(vector)
            row["embedding_model"] = settings.embedding_model
            row["embedding_text_version"] = settings.embedding_text_version
            writer.append_row(row)
            count += 1
            if count % 10_000 == 0:
                writer.commit()
        writer.commit()
    files = writer.batch_files
    output_path.write_text(json.dumps({"rows": count, "files": files}, indent=2), encoding="utf-8")
    return files


def start_import(files_manifest: Path, collection: str, settings: Settings) -> dict:
    client = sync_client(settings)
    if not client.has_collection(collection_name=collection):
        raise ValueError(f"collection does not exist: {collection}")
    data = json.loads(files_manifest.read_text(encoding="utf-8"))
    response = bulk_import(
        url=settings.milvus_uri,
        collection_name=collection,
        files=data["files"],
        db_name=settings.milvus_database,
        api_key=settings.milvus_token or "",
    )
    payload = response.json()
    if payload.get("code") not in (0, 200):
        raise RuntimeError(payload)
    return {"expected_rows": data["rows"], "job_id": payload["data"]["jobId"]}


def import_status(job_id: str, settings: Settings) -> dict:
    response = get_import_progress(
        url=settings.milvus_uri,
        job_id=job_id,
        db_name=settings.milvus_database,
        api_key=settings.milvus_token or "",
    )
    payload = response.json()
    if payload.get("code") not in (0, 200):
        raise RuntimeError(payload)
    return payload["data"]


def verify_import(collection: str, job_id: str, expected_rows: int, settings: Settings) -> dict:
    """Gate alias activation on completed import, indexes, load state and both search paths."""
    if expected_rows <= 0:
        raise ValueError("expected rows must be positive")
    progress = import_status(job_id, settings)
    if (progress.get("state") != "Completed" or progress.get("collectionName") != collection
            or int(progress.get("importedRows", -1)) != expected_rows):
        raise ValueError("import job is not complete for the expected collection and row count")
    client = sync_client(settings)
    actual = int(client.get_collection_stats(collection_name=collection)["row_count"])
    if actual != expected_rows:
        raise ValueError(f"row count mismatch: {actual} != {expected_rows}")
    required = {
        "embedding": "IVF_SQ8", "bm25": "SPARSE_INVERTED_INDEX", "geo": "RTREE",
        "category_id": "INVERTED", "category_parent_id": "BITMAP",
    }
    actual_indexes = {
        name: client.describe_index(collection_name=collection, index_name=name)["index_type"]
        for name in client.list_indexes(collection_name=collection)
    }
    if any(actual_indexes.get(name) != kind for name, kind in required.items()):
        raise ValueError(f"required indexes missing: {actual_indexes}")
    client.refresh_load(collection_name=collection)
    if client.get_load_state(collection_name=collection)["state"] != LoadState.Loaded:
        raise ValueError("collection is not loaded")
    probes = client.query(
        collection_name=collection, filter="", limit=1,
        output_fields=["nf_id", "embedding", "search_text", "longitude", "latitude", "category_id"],
    )
    if not probes:
        raise ValueError("collection has no searchable rows")
    probe = probes[0]
    spatial_filter = search_filter(
        probe["longitude"], probe["latitude"], 1, [probe["category_id"]]
    )
    dense = client.search(
        collection_name=collection, data=[probe["embedding"]], anns_field="embedding",
        search_params={"metric_type": "COSINE", "params": {"nprobe": settings.nprobe}},
        filter=spatial_filter, limit=10, output_fields=["nf_id"],
    )
    sparse = client.search(
        collection_name=collection, data=[probe["search_text"]], anns_field="bm25",
        search_params={"metric_type": "BM25"}, filter=spatial_filter,
        limit=10, output_fields=["nf_id"],
    )
    if not dense[0] or not sparse[0]:
        raise ValueError("spatially filtered dense or BM25 search returned no rows")
    return {"collection": collection, "rows": actual, "indexes": actual_indexes,
            "probe_nf_id": probe["nf_id"], "dense_hits": len(dense[0]), "bm25_hits": len(sparse[0])}
