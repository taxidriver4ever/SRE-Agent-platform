"""Immutable runtime identity for version-aware telemetry."""
from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    version: str = os.getenv("SERVICE_VERSION", "dev")
    pod_name: str = os.getenv("POD_NAME", "local")


settings = Settings()
