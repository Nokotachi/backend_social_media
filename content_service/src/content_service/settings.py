from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_FILE = ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "content-service"
    debug: bool = False

    content_db_password: str
    db_host: str = "localhost"
    db_port: int
    database_url: str = ""
    db_echo: bool = False

    jwt_public_key_file: Path = ROOT / "keys" / "jwt_public.pem"

    storage_backend: str = "s3"
    minio_root_user: str
    minio_root_password: str
    minio_port: int
    s3_endpoint: str = ""
    s3_secure: bool = False
    s3_bucket: str = "posts"
    s3_public_url: str = ""
    media_root: str = "media"
    media_base_url: str = "http://localhost:8001/media"
    max_images_per_post: int = 10
    max_image_bytes: int = 10 * 1024 * 1024
    allowed_aspect_ratios: str = "1:1,4:5,16:9"
    min_image_short_side: int = 560
    aspect_ratio_tolerance: float = 0.03

    rabbitmq_user: str
    rabbitmq_password: str
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    events_enabled: bool = True

    recommendation_service_url: str = ""
    recommendation_timeout_seconds: float = 10.0

    cors_origins: str

    @model_validator(mode="after")
    def _derive(self) -> "Settings":
        if not self.database_url:
            self.database_url = (
                f"postgresql+asyncpg://content_service:{self.content_db_password}"
                f"@{self.db_host}:{self.db_port}/content_db"
            )
        if not self.s3_endpoint:
            self.s3_endpoint = f"localhost:{self.minio_port}"
        if not self.s3_public_url:
            self.s3_public_url = f"http://localhost:{self.minio_port}"
        return self

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def aspect_ratio_list(self) -> list[str]:
        return [r.strip() for r in self.allowed_aspect_ratios.split(",") if r.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
