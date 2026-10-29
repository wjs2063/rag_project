"""Operator commands for inspection, preparation, embedding, import and updates."""

import argparse
import json
from pathlib import Path

from openai import OpenAI

from poi_service.batch import attach, collect, item_status, release_uncertain, status, submit, write_requests
from poi_service.catalog import Catalog, build_catalog_from_xlsx, catalog_path
from poi_service.geo import point_wkt
from poi_service.importer import export_bulk, import_status, start_import, verify_import
from poi_service.milvus_store import create_collection, sync_client
from poi_service.prepare import inspect_zip, prepare, sample_staging
from poi_service.settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poi")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-zip", help="list ZIP members and DBF header row counts")
    inspect.add_argument("zip", type=Path)
    inspect.add_argument("--headers", action="store_true", help="include DBF fields and XLSX sheet headers")
    catalog = commands.add_parser("catalog-build", help="build versioned category CSV from source workbook")
    catalog.add_argument("archive", type=Path)
    catalog.add_argument("member")
    catalog.add_argument("policy", type=Path)
    catalog.add_argument("output", type=Path)
    prep = commands.add_parser("prepare", help="normalize source archive regions into SQLite")
    prep.add_argument("manifest", type=Path)
    prep.add_argument("db", type=Path)
    sample = commands.add_parser("sample", help="select deterministic regional POI sample")
    sample.add_argument("source_db", type=Path)
    sample.add_argument("output_db", type=Path)
    sample.add_argument("--regions", nargs="+", required=True)
    sample.add_argument("--per-region", type=int, default=2000)
    sample.add_argument("--seed", default="poi-sample-v1")
    requests = commands.add_parser("batch-write", help="write OpenAI Batch JSONL shards")
    requests.add_argument("db", type=Path)
    requests.add_argument("directory", type=Path)
    requests.add_argument("--run-id", required=True)
    requests.add_argument("--max-rows", type=int, default=40_000)
    send = commands.add_parser("batch-submit", help="upload and submit Batch jobs")
    send.add_argument("directory", type=Path)
    send.add_argument("--run-id", required=True)
    send.add_argument("--max-shards", type=int, default=1)
    send.add_argument("--external-transfer-authorized", action="store_true", required=True)
    receive = commands.add_parser("batch-collect", help="retrieve completed embedding jobs")
    receive.add_argument("db", type=Path)
    receive.add_argument("directory", type=Path)
    receive.add_argument("--run-id", required=True)
    progress = commands.add_parser("batch-status", help="show local run and POI embedding progress")
    progress.add_argument("db", type=Path)
    progress.add_argument("directory", type=Path)
    progress.add_argument("--run-id", required=True)
    check = commands.add_parser("batch-check", help="check the embedding checkpoint for one NF_ID")
    check.add_argument("db", type=Path)
    check.add_argument("nf_id")
    reconcile = commands.add_parser(
        "batch-attach", help="attach an existing batch after uncertain submission"
    )
    reconcile.add_argument("directory", type=Path)
    reconcile.add_argument("--run-id", required=True)
    reconcile.add_argument("--file", required=True)
    reconcile.add_argument("--batch-id", required=True)
    release = commands.add_parser(
        "batch-release", help="release an uncertain create after confirming no batch exists"
    )
    release.add_argument("directory", type=Path)
    release.add_argument("--run-id", required=True)
    release.add_argument("--file", required=True)
    release.add_argument("--confirmed-no-batch", action="store_true", required=True)
    create = commands.add_parser("collection-create")
    create.add_argument("collection")
    create.add_argument("--nlist", type=int, default=64)
    export = commands.add_parser("bulk-export")
    export.add_argument("db", type=Path)
    export.add_argument("manifest", type=Path)
    start = commands.add_parser("import-start")
    start.add_argument("manifest", type=Path)
    start.add_argument("collection")
    status = commands.add_parser("import-status")
    status.add_argument("job_id")
    switch = commands.add_parser("alias-switch")
    switch.add_argument("collection")
    switch.add_argument("--expected-rows", required=True, type=int)
    switch.add_argument("--job-id", required=True)
    switch.add_argument("--initial", action="store_true", help="create alias on first deployment")
    update = commands.add_parser("upsert")
    update.add_argument("collection")
    update.add_argument("json_file", type=Path)
    update.add_argument("--external-transfer-authorized", action="store_true", required=True)
    delete = commands.add_parser("delete")
    delete.add_argument("collection")
    delete.add_argument("nf_id")
    get = commands.add_parser("get")
    get.add_argument("collection")
    get.add_argument("nf_id")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings()
    result = None
    if args.command == "inspect-zip":
        result = inspect_zip(args.zip, headers=args.headers)
    elif args.command == "catalog-build":
        result = build_catalog_from_xlsx(args.archive, args.member, args.policy, args.output)
    elif args.command == "prepare":
        result = prepare(args.manifest, args.db)
    elif args.command == "sample":
        result = sample_staging(args.source_db, args.output_db, args.regions, args.per_region, args.seed)
    elif args.command == "batch-write":
        result = [
            str(p) for p in write_requests(args.db, args.directory, args.run_id, settings, args.max_rows)
        ]
    elif args.command == "batch-submit":
        result = submit(args.directory, args.run_id, settings, args.external_transfer_authorized, args.max_shards)
    elif args.command == "batch-collect":
        result = collect(args.db, args.directory, args.run_id, settings)
    elif args.command == "batch-status":
        result = status(args.db, args.directory, args.run_id)
    elif args.command == "batch-check":
        result = item_status(args.db, args.nf_id)
    elif args.command == "batch-attach":
        result = attach(args.directory, args.run_id, args.file, args.batch_id, settings)
    elif args.command == "batch-release":
        result = release_uncertain(args.directory, args.run_id, args.file, settings, args.confirmed_no_batch)
    elif args.command == "collection-create":
        create_collection(sync_client(settings), args.collection, settings.embedding_dimensions, args.nlist)
        result = {"created": args.collection}
    elif args.command == "bulk-export":
        result = export_bulk(args.db, args.manifest, settings)
    elif args.command == "import-start":
        result = start_import(args.manifest, args.collection, settings)
    elif args.command == "import-status":
        result = import_status(args.job_id, settings)
    elif args.command == "alias-switch":
        result = verify_import(args.collection, args.job_id, args.expected_rows, settings)
        client = sync_client(settings)
        if args.initial:
            client.create_alias(collection_name=args.collection, alias=settings.collection_alias)
        else:
            client.alter_alias(collection_name=args.collection, alias=settings.collection_alias)
        result["alias"] = settings.collection_alias
    elif args.command == "upsert":
        if not args.external_transfer_authorized:
            raise ValueError("confirm that source terms permit sending POI text to OpenAI")
        row = json.loads(args.json_file.read_text(encoding="utf-8"))
        required = {
            "nf_id",
            "name",
            "aliases",
            "search_text",
            "address",
            "category_id",
            "category_name",
            "category_parent_id",
            "longitude",
            "latitude",
            "geo",
            "source_region",
            "source_version",
            "updated_at",
        }
        if missing := required - row.keys():
            raise ValueError(f"missing fields: {sorted(missing)}")
        catalog_file, catalog_version = catalog_path(Path("config/source_manifest.json"))
        catalog = Catalog.from_csv(catalog_file, catalog_version)
        category = catalog.categories.get(row["category_id"])
        if not category or not category.allowed:
            raise ValueError("category is not in the visit-purpose allowlist")
        if row["geo"] != point_wkt(float(row["longitude"]), float(row["latitude"])):
            raise ValueError("geo must match longitude and latitude")
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY (or POI_OPENAI_API_KEY) is required")
        embedded = OpenAI(api_key=settings.openai_api_key).embeddings.create(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            input=f"이름: {row['name']}; 별칭: {row['aliases']}; 분류: {row['category_name']}; 주소: {row['address']}",
        )
        row["embedding"] = embedded.data[0].embedding
        row["embedding_model"] = settings.embedding_model
        row["embedding_text_version"] = settings.embedding_text_version
        result = sync_client(settings).upsert(collection_name=args.collection, data=[row])
    elif args.command == "delete":
        result = sync_client(settings).delete(collection_name=args.collection, ids=[args.nf_id])
    elif args.command == "get":
        result = sync_client(settings).get(
            collection_name=args.collection,
            ids=[args.nf_id],
            output_fields=[
                "nf_id",
                "name",
                "category_id",
                "address",
                "longitude",
                "latitude",
                "source_version",
            ],
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
