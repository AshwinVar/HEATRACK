"""Runtime configuration. All secrets come from the environment; nothing is hard-coded."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FP_", extra="ignore")

    # "development" enables local token minting and the fake push transport.
    # "production" refuses to start with either.
    app_env: Literal["development", "test", "production"] = "development"

    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:5432/familypulse"

    # --- Supabase Auth JWT verification -------------------------------------------------
    # Asymmetric keys (current Supabase default): set auth_jwks_url to
    #   https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
    # Legacy shared-secret projects: set auth_jwt_secret instead (HS256).
    auth_jwks_url: str | None = None
    auth_jwt_secret: str | None = None
    # e.g. https://<project-ref>.supabase.co/auth/v1
    auth_issuer: str = "http://127.0.0.1:8000/dev-auth"
    auth_audience: str = "authenticated"
    auth_algorithms: list[str] = Field(default_factory=lambda: ["ES256", "RS256", "HS256"])
    auth_leeway_seconds: int = 30

    # Development-only HS256 token minting endpoint (/v1/dev/token). Never in production.
    dev_token_mint_enabled: bool = False
    dev_jwt_secret: str | None = None
    # Networks allowed to call the dev mint endpoint (e.g. add the Docker bridge or
    # 10.0.2.0/24 for the Android emulator). Loopback only by default.
    dev_token_allowed_cidrs: list[str] = Field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])

    # --- Push ----------------------------------------------------------------------------
    push_transport: Literal["fcm", "fake", "disabled"] = "disabled"
    # Path to a Firebase service-account JSON (mounted secret). If unset, Application
    # Default Credentials are used.
    firebase_credentials_file: str | None = None
    push_max_attempts: int = 8
    push_backoff_base_seconds: float = 30.0
    push_backoff_max_seconds: float = 3600.0
    push_lease_seconds: int = 60

    # --- Ingest limits -------------------------------------------------------------------
    ingest_max_records: int = 500
    ingest_max_samples_per_record: int = 2000
    ingest_max_total_samples: int = 20000
    ingest_max_future_skew_seconds: int = 600
    ingest_max_age_days: int = 400

    # --- Freshness budgets (engineering defaults, seconds). Tune from observed cadence. ---
    freshness_heart_rate_s: int = 3 * 3600
    freshness_resting_heart_rate_s: int = 36 * 3600
    freshness_steps_s: int = 12 * 3600
    freshness_sleep_s: int = 36 * 3600
    freshness_hrv_rmssd_s: int = 36 * 3600
    heartbeat_stale_s: int = 6 * 3600

    invitation_ttl_minutes: int = 30
    consent_version: str = "2026-10-04.v1"

    cors_origins: list[str] = Field(default_factory=list)

    worker_interval_seconds: int = 60

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if self.app_env == "production":
            if self.dev_token_mint_enabled:
                raise ValueError("FP_DEV_TOKEN_MINT_ENABLED must be false in production")
            if self.push_transport == "fake":
                raise ValueError("FP_PUSH_TRANSPORT=fake is not allowed in production")
            if not (self.auth_jwks_url or self.auth_jwt_secret):
                raise ValueError("Configure FP_AUTH_JWKS_URL or FP_AUTH_JWT_SECRET in production")
            if any(o == "*" for o in self.cors_origins):
                raise ValueError("Wildcard CORS is not allowed in production")
        if self.dev_token_mint_enabled and not self.dev_jwt_secret:
            raise ValueError("FP_DEV_JWT_SECRET is required when dev token minting is enabled")
        return self

    def freshness_budget(self, metric: str) -> int:
        return int(getattr(self, f"freshness_{metric}_s"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
