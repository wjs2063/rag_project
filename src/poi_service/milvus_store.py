"""Milvus collection definition and constrained hybrid search."""

import asyncio
import json
import re
from typing import Any

from pymilvus import (
    AnnSearchRequest,
    AsyncMilvusClient,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from poi_service.geo import haversine_m, point_wkt
from poi_service.settings import Settings

COLLECTION_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]{0,254}$")
OUTPUT_FIELDS = ["nf_id", "name", "category_id", "category_name", "address", "longitude", "latitude"]


def sync_client(settings: Settings) -> MilvusClient:
    kwargs: dict[str, Any] = {"uri": settings.milvus_uri, "db_name": settings.milvus_database}
    if settings.milvus_token:
        kwargs["token"] = settings.milvus_token
    return MilvusClient(**kwargs)


def build_schema(dimensions: int = 512):
    """Create the schema shared by collection creation and bulk export."""
    if dimensions != 512:
        raise ValueError("this schema contract requires 512 dimensions")
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("nf_id", DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field("name", DataType.VARCHAR, max_length=512)
    schema.add_field("aliases", DataType.VARCHAR, max_length=2048)
    schema.add_field(
        "search_text",
        DataType.VARCHAR,
        max_length=3072,
        enable_analyzer=True,
        analyzer_params={"tokenizer": {"type": "lindera", "dict_kind": "ko-dic"}},
    )
    schema.add_field("address", DataType.VARCHAR, max_length=1024)
    schema.add_field("category_id", DataType.VARCHAR, max_length=32)
    schema.add_field("category_name", DataType.VARCHAR, max_length=256)
    schema.add_field("category_parent_id", DataType.VARCHAR, max_length=32)
    schema.add_field("longitude", DataType.DOUBLE)
    schema.add_field("latitude", DataType.DOUBLE)
    schema.add_field("geo", DataType.GEOMETRY)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dimensions)
    schema.add_field("bm25", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field("source_region", DataType.VARCHAR, max_length=64)
    schema.add_field("source_version", DataType.VARCHAR, max_length=128)
    schema.add_field("updated_at", DataType.VARCHAR, max_length=32)
    schema.add_field("embedding_model", DataType.VARCHAR, max_length=64)
    schema.add_field("embedding_text_version", DataType.VARCHAR, max_length=32)
    schema.add_function(
        Function(
            name="search_text_bm25",
            input_field_names=["search_text"],
            output_field_names=["bm25"],
            function_type=FunctionType.BM25,
        )
    )
    return schema


def create_collection(client: MilvusClient, name: str, dimensions: int = 512, nlist: int = 1024) -> None:
    """Create a new physical collection. Existing collections are never replaced."""
    if not COLLECTION_NAME_PATTERN.fullmatch(name):
        raise ValueError("invalid collection name")
    if client.has_collection(collection_name=name):
        raise ValueError(f"collection already exists: {name}")
    schema = build_schema(dimensions)
    indexes = client.prepare_index_params()
    indexes.add_index(
        field_name="embedding", index_type="IVF_SQ8", metric_type="COSINE", params={"nlist": nlist}
    )
    indexes.add_index(field_name="bm25", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    indexes.add_index(field_name="geo", index_type="RTREE")
    indexes.add_index(field_name="category_id", index_type="INVERTED")
    indexes.add_index(field_name="category_parent_id", index_type="BITMAP")
    client.create_collection(collection_name=name, schema=schema, index_params=indexes)


def search_filter(longitude: float, latitude: float, radius_m: float, category_ids: list[str]) -> str:
    """Construct a filter from validated numeric values and JSON-escaped category IDs."""
    if not 1 <= radius_m <= 30_000:
        raise ValueError("radius out of range")
    expression = f"ST_DWITHIN(geo, '{point_wkt(longitude, latitude)}', {radius_m:.3f})"
    if category_ids:
        if len(category_ids) > 2500 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key) for key in category_ids
        ):
            raise ValueError("invalid category ID")
        expression += " and category_id in " + json.dumps(category_ids, ensure_ascii=True)
    return expression


async def hybrid_search(
    client: AsyncMilvusClient,
    settings: Settings,
    query: str,
    vector: list[float],
    longitude: float,
    latitude: float,
    radius_m: float,
    category_ids: list[str],
    limit: int,
) -> list[dict]:
    expression = search_filter(longitude, latitude, radius_m, category_ids)
    candidate_limit = min(100, max(limit * 5, 40))
    requests = [
        AnnSearchRequest(
            data=[vector],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": settings.nprobe}},
            limit=candidate_limit,
            expr=expression,
        ),
        AnnSearchRequest(
            data=[query],
            anns_field="bm25",
            param={"metric_type": "BM25"},
            limit=candidate_limit,
            expr=expression,
        ),
    ]
    results = await asyncio.wait_for(
        client.hybrid_search(
            collection_name=settings.collection_alias,
            reqs=requests,
            ranker=RRFRanker(60),
            limit=limit,
            output_fields=OUTPUT_FIELDS,
        ),
        timeout=settings.milvus_timeout_seconds,
    )
    output = []
    for hit in results[0]:
        entity = hit["entity"]
        output.append(
            {
                "nf_id": entity["nf_id"],
                "name": entity["name"],
                "category": {"id": entity["category_id"], "name": entity["category_name"]},
                "address": entity["address"],
                "longitude": entity["longitude"],
                "latitude": entity["latitude"],
                "distance_m": round(
                    haversine_m(longitude, latitude, entity["longitude"], entity["latitude"]), 2
                ),
                "score": hit["distance"],
            }
        )
    return output
