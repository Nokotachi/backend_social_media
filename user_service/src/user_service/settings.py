from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]
ROOT_ENV_FILE = ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "user-service"
    debug: bool = False

    user_db_password: str
    db_host: str = "localhost"
    db_port: int
    database_url: str = ""
    db_echo: bool = False

    jwt_private_key_file: Path = ROOT / "keys" / "jwt_private.pem"
    access_token_ttl_minutes: int = 30

    refresh_token_ttl_days: int = 30
    refresh_token_bytes: int = 32

    email_code_length: int = 6
    email_code_ttl_minutes: int = 10
    email_code_max_attempts: int = 5
    email_code_resend_cooldown_seconds: int = 60

    email_backend: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 0
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    smtp_ssl: bool = False
    smtp_from: str = ""
    smtp_timeout_seconds: float = 15.0

    password_reset_token_ttl_minutes: int = 10
    password_reset_token_bytes: int = 32

    password_min_length: int = 8

    login_max_failures: int = 5
    login_failure_window_minutes: int = 15

    cors_origins: str

    storage_backend: str = "s3"
    minio_root_user: str
    minio_root_password: str
    minio_port: int
    s3_endpoint: str = ""
    s3_secure: bool = False
    s3_bucket: str = "avatars"
    s3_public_url: str = ""
    media_root: str = "media"
    media_base_url: str = "http://localhost:8000/media"
    avatar_max_bytes: int = 10 * 1024 * 1024

    rabbitmq_user: str
    rabbitmq_password: str
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    events_enabled: bool = True

    @model_validator(mode="after")
    def _derive(self) -> "Settings":
        if not self.database_url:
            self.database_url = (
                f"postgresql+asyncpg://user_service:{self.user_db_password}"
                f"@{self.db_host}:{self.db_port}/user_db"
            )
        if not self.s3_endpoint:
            self.s3_endpoint = f"localhost:{self.minio_port}"
        if not self.s3_public_url:
            self.s3_public_url = f"http://localhost:{self.minio_port}"
        if self.email_backend == "smtp":
            missing = [
                name.upper()
                for name in ("smtp_host", "smtp_port", "smtp_username", "smtp_password")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"EMAIL_BACKEND=smtp, но в .env не заданы: {', '.join(missing)}")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
