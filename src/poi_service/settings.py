"""Runtime configuration, supplied through environment variables."""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POI_", env_file=".env", extra="ignore")

    milvus_uri: str = "http://localhost:19530"
    milvus_token: str | None = None
    milvus_database: str = "default"
    collection_alias: str = "pois_current"
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("POI_OPENAI_API_KEY", "OPENAI_API_KEY")
    )
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 512
    embedding_text_version: str = "poi-v1"
    openai_timeout_seconds: float = 3.0
    milvus_timeout_seconds: float = 2.0
    max_concurrent_searches: int = 128
    nprobe: int = 16
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str = "a-bucket"
    minio_secure: bool = False
