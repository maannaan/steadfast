"""Service configuration.

Every tunable lives here and is read from environment variables (or a local `.env`),
so the same image runs locally, in CI and on AWS without code changes, and no value
that differs between environments is ever hard-coded. `.env.example` documents each one.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Which pipeline implementation sits behind the Pipeline protocol, and where it lives.
    pipeline_backend: Literal["mock", "open_weight"] = "mock"
    pipeline_url: str = "http://localhost:8081"
    # Keep this near real peak concurrency (~25 at 50 calls/s). httpcore scans every pooled
    # connection on each request, so an oversized pool burns the event loop at peak and
    # the resulting attempt timeouts trip the breaker. Measured: 200 opened it, 50 did not.
    http_max_connections: int = Field(default=50, ge=1)

    db_path: str = "./data/steadfast.db"
    # FULL fsyncs every commit: a call is only acknowledged once it is on disk.
    db_synchronous: Literal["FULL", "NORMAL"] = "FULL"

    # Retry policy. 3 attempts at a 20% independent failure rate leaves 0.2^3 = 0.8% of
    # calls for the DLQ; the deadline bounds how long a borrower can be left waiting.
    max_attempts: int = Field(default=3, ge=1)
    backoff_base_s: float = Field(default=0.2, ge=0)
    backoff_cap_s: float = Field(default=2.0, ge=0)
    attempt_timeout_s: float = Field(default=2.5, gt=0)
    call_deadline_s: float = Field(default=8.0, gt=0)

    # Circuit breaker. A full 100-attempt window at 50% sits far above the 20% baseline
    # failure rate, so it does not trip on normal noise (see resilience/breaker.py).
    breaker_window: int = Field(default=100, ge=1)
    breaker_min_calls: int = Field(default=100, ge=1)
    breaker_failure_threshold: float = Field(default=0.5, gt=0, le=1)
    breaker_open_s: float = Field(default=10.0, gt=0)
    breaker_half_open_max: int = Field(default=3, ge=1)

    # A call can be several pipeline turns (a real conversation). 1 = one pipeline request
    # per call event, exactly as the challenge spec describes.
    turns_per_call: int = Field(default=1, ge=1)
    turn_gap_s: float = Field(default=0.0, ge=0)

    # Seed for business-outcome sampling; unset means non-reproducible outcomes.
    outcome_seed: int | None = None

    # How long shutdown waits for in-flight calls to finish and be persisted.
    shutdown_drain_s: float = Field(default=15.0, ge=0)
    log_level: str = "INFO"
