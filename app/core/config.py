import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote_plus

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIN_JWT_SECRET_BYTES = 32

CommaList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Lamsa API"
    APP_ENV: str = "development"
    DEBUG: bool = False

    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_NAME: str
    DB_USER: str
    DB_PASSWORD: str
    TEST_DATABASE_NAME: str = "lamsa_test"

    STORAGE_BACKEND: str = "local"
    MEDIA_ROOT: Path = Path("./media")
    MEDIA_URL: str = "http://127.0.0.1:8000/media"
    MAX_UPLOAD_MB: int = 5

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 60
    REFRESH_REUSE_GRACE_SECONDS: int = 20
    SIGNUP_TOKEN_MINUTES: int = 10

    GOOGLE_CLIENT_IDS: CommaList = []
    GOOGLE_DEV_MODE: bool = False

    DEFAULT_PHONE_REGION: str = "DE"
    CORS_ORIGINS: CommaList = ["http://localhost:3000", "http://127.0.0.1:3000"]
    TRUST_PROXY_HEADERS: bool = False

    PUSH_BACKEND: str = "log"  # log | firebase
    FIREBASE_CREDENTIALS_PATH: Path | None = None
    RUN_WORKER_IN_APP: bool | None = None  # default: true only when APP_ENV=development

    @field_validator("GOOGLE_CLIENT_IDS", "CORS_ORIGINS", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_long_enough(cls, value: str) -> str:
        if len(value.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
            raise ValueError(
                f"JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes; generate one with "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value

    @model_validator(mode="after")
    def _google_dev_mode_only_in_development(self) -> "Settings":
        if self.GOOGLE_DEV_MODE and self.APP_ENV != "development":
            raise ValueError("GOOGLE_DEV_MODE=true is only allowed when APP_ENV=development")
        return self

    @model_validator(mode="after")
    def _push_backend(self) -> "Settings":
        if self.PUSH_BACKEND not in ("log", "firebase"):
            raise ValueError("PUSH_BACKEND must be 'log' or 'firebase'")
        if self.PUSH_BACKEND == "firebase":
            path = self.FIREBASE_CREDENTIALS_PATH
            if path is None or not path.is_file() or not os.access(path, os.R_OK):
                raise ValueError("PUSH_BACKEND=firebase needs a readable FIREBASE_CREDENTIALS_PATH (service account JSON)")
        return self

    @property
    def run_worker_in_app(self) -> bool:
        return self.RUN_WORKER_IN_APP if self.RUN_WORKER_IN_APP is not None else self.APP_ENV == "development"

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+aiomysql://{self.DB_USER}:{quote_plus(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def MAX_UPLOAD_BYTES(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
