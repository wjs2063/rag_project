import asyncio

import httpx
import pytest
from pydantic import ValidationError
from pymilvus.client.types import LoadState

from poi_service.api import SearchRequest, create_app
from poi_service.catalog import Catalog, Category
from poi_service.milvus_store import hybrid_search, search_filter
from poi_service.settings import Settings


def test_filter_escapes_category_ids_and_keeps_spatial_constraint() -> None:
    expression = search_filter(127.0, 37.5, 1000, ["FOOD_1"])
    assert "ST_DWITHIN(geo, 'POINT(127.00000000 37.50000000)', 1000.000)" in expression
    assert 'category_id in ["FOOD_1"]' in expression
    with pytest.raises(ValueError):
        search_filter(127.0, 37.5, 1000, ['x"] or true'])


def test_search_bounds_and_category_expansion() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query=" ", longitude=127, latitude=37, radius_m=1)
    with pytest.raises(ValidationError):
        SearchRequest(query="식당", longitude=127, latitude=37, radius_m=30_001)
    catalog = Catalog(
        {"A": Category("A", "음식", None, False), "A1": Category("A1", "식당", "A", True)}, "test"
    )
    assert catalog.allowed_descendants(["A"]) == ["A1"]


def test_hybrid_paths_share_the_same_pre_filter() -> None:
    class FakeClient:
        async def hybrid_search(self, **kwargs):
            assert len(kwargs["reqs"]) == 2
            assert kwargs["reqs"][0].expr == kwargs["reqs"][1].expr
            assert "ST_DWITHIN" in kwargs["reqs"][0].expr
            assert 'category_id in ["A1"]' in kwargs["reqs"][0].expr
            return [
                [
                    {
                        "entity": {
                            "nf_id": "NF1",
                            "name": "식당",
                            "category_id": "A1",
                            "category_name": "식당",
                            "address": "서울",
                            "longitude": 127.0,
                            "latitude": 37.5,
                        },
                        "distance": 0.032,
                    }
                ]
            ]

    settings = Settings(openai_api_key="test")
    rows = asyncio.run(hybrid_search(FakeClient(), settings, "식당", [], 127.0, 37.5, 1000, ["A1"], 10))
    assert rows[0]["distance_m"] == 0
    assert rows[0]["score"] == 0.032


def test_readyz_requires_loaded_collection() -> None:
    app = create_app(Settings(openai_api_key="test"))

    class FakeMilvus:
        def __init__(self, state):
            self.state = state

        async def get_load_state(self, **kwargs):
            assert kwargs["collection_name"] == "pois_current"
            return {"state": self.state}

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            app.state.milvus = FakeMilvus(LoadState.NotLoad)
            assert (await client.get("/readyz")).status_code == 503
            app.state.milvus = FakeMilvus(LoadState.Loaded)
            assert (await client.get("/readyz")).json()["status"] == "ready"

    asyncio.run(check())
