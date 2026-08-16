"""Centralized environment configuration for local, test and Kubernetes execution."""

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings prevent request handlers from changing process configuration."""

    database_url: str = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://sre_app:sre_app_dev_only@mysql:3306/sre_lab",
    )
    version: str = os.getenv("SERVICE_VERSION", "dev")
    pod_name: str = os.getenv("POD_NAME", "local")
    otlp_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")


settings = Settings()
