from poi_service.settings import Settings


def test_standard_and_project_openai_key_names(monkeypatch) -> None:
    monkeypatch.delenv("POI_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "standard-key")
    assert Settings(_env_file=None).openai_api_key == "standard-key"

    monkeypatch.setenv("POI_OPENAI_API_KEY", "project-key")
    assert Settings(_env_file=None).openai_api_key == "project-key"
