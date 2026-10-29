"""HTTP API for spatially filtered hybrid POI search."""

import asyncio
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator
from pymilvus import AsyncMilvusClient
from pymilvus.client.types import LoadState

from poi_service.catalog import Catalog, catalog_path
from poi_service.milvus_store import hybrid_search
from poi_service.settings import Settings


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    radius_m: float = Field(ge=1, le=30_000)
    category_ids: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("longitude", "latitude", "radius_m")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be finite")
        return value

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class CategoryOut(BaseModel):
    id: str
    name: str


class PoiOut(BaseModel):
    nf_id: str
    name: str
    category: CategoryOut
    address: str
    longitude: float
    latitude: float
    distance_m: float
    score: float


class SearchResponse(BaseModel):
    results: list[PoiOut]


def create_app(settings: Settings | None = None, catalog: Catalog | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if catalog is None:
            path, version = catalog_path(Path("config/source_manifest.json"))
            app.state.catalog = Catalog.from_csv(path, version)
        else:
            app.state.catalog = catalog
        app.state.settings = settings
        app.state.semaphore = asyncio.Semaphore(settings.max_concurrent_searches)
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY (or POI_OPENAI_API_KEY) is required")
        app.state.openai = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds, max_retries=0
        )
        kwargs = {"uri": settings.milvus_uri, "db_name": settings.milvus_database}
        if settings.milvus_token:
            kwargs["token"] = settings.milvus_token
        try:
            async with AsyncMilvusClient(**kwargs) as milvus:
                app.state.milvus = milvus
                yield
        finally:
            await app.state.openai.close()

    app = FastAPI(title="Public POI Search", version="1.0.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict:
        try:
            state = await asyncio.wait_for(
                request.app.state.milvus.get_load_state(
                    collection_name=settings.collection_alias,
                    timeout=settings.milvus_timeout_seconds,
                ),
                timeout=settings.milvus_timeout_seconds + 0.1,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Milvus collection unavailable") from exc
        if state.get("state") != LoadState.Loaded:
            raise HTTPException(status_code=503, detail="Milvus collection is not loaded")
        return {"status": "ready", "collection": settings.collection_alias}

    @app.get("/v1/categories")
    async def categories(request: Request) -> dict:
        return request.app.state.catalog.as_tree()

    @app.post("/v1/pois/search", response_model=SearchResponse)
    async def search(payload: SearchRequest, request: Request) -> SearchResponse:
        catalog = request.app.state.catalog
        try:
            allowed = catalog.allowed_descendants(payload.category_ids) if payload.category_ids else []
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"unknown category ID: {exc.args[0]}") from exc
        if payload.category_ids and not allowed:
            return SearchResponse(results=[])
        async with request.app.state.semaphore:
            try:
                embedding = await asyncio.wait_for(
                    request.app.state.openai.embeddings.create(
                        model=settings.embedding_model,
                        dimensions=settings.embedding_dimensions,
                        input=payload.query,
                    ),
                    timeout=settings.openai_timeout_seconds,
                )
                rows = await hybrid_search(
                    request.app.state.milvus,
                    settings,
                    payload.query,
                    embedding.data[0].embedding,
                    payload.longitude,
                    payload.latitude,
                    payload.radius_m,
                    allowed,
                    payload.limit,
                )
            except TimeoutError as exc:
                raise HTTPException(status_code=504, detail="search dependency timed out") from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail="search dependency failed") from exc
        return SearchResponse(results=rows)

    return app


app = create_app()
