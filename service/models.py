"""Data models for call events (Appendix A of the challenge) and pipeline results.

Input validation is deliberately lenient in two places, because the scoring harness may
send Predixion's own sample data:
- `event_id` is a non-empty string, not a strict UUID (the samples use IDs like
  "a1f2c3d4-0001", which are not valid UUIDs).
- `outcome`, `call_duration_s` and `retry_count` may be present on input but are ignored:
  the service is the only thing allowed to decide what happened on a call.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BorrowerBucket(StrEnum):
    DPD_0_30 = "0-30 DPD"
    DPD_31_60 = "31-60 DPD"
    DPD_61_90 = "61-90 DPD"
    DPD_90_PLUS = "90+ DPD"


class ProductType(StrEnum):
    CREDIT_CARD = "credit_card"
    BNPL = "bnpl"
    PERSONAL_LOAN = "personal_loan"


class Outcome(StrEnum):
    CONNECTED = "connected"
    NO_ANSWER = "no_answer"
    PTP_TAKEN = "ptp_taken"
    DISPUTE = "dispute"
    FAILED_DOWNSTREAM = "failed_downstream"


# URL-safe characters only, so an event_id can always be used in /calls/{id} and /dlq/{id}.
EVENT_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"


class CallEvent(BaseModel):
    """A call event as received from the dialler / traffic generator."""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1, max_length=128, pattern=EVENT_ID_PATTERN)
    campaign_id: str = Field(min_length=1, max_length=64)
    borrower_bucket: BorrowerBucket
    product_type: ProductType | None = None
    timestamp: datetime
    # Accepted for schema compatibility, ignored by the service.
    call_duration_s: int | None = Field(default=None, ge=0)
    outcome: Outcome | None = None
    retry_count: int | None = Field(default=None, ge=0)


class CallResult(BaseModel):
    """What the service recorded for a call. Returned by POST /calls and stored in call_log."""

    event_id: str
    campaign_id: str
    borrower_bucket: BorrowerBucket
    product_type: ProductType | None
    timestamp: datetime
    outcome: Outcome
    call_duration_s: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    status: Literal["succeeded", "failed"]
    dlq_reason: str | None = None


class PipelineResult(BaseModel):
    """A successful STT -> LLM -> TTS turn (Appendix B success body)."""

    status: Literal["ok"]
    transcript: str
    response_audio_ref: str
    latency_ms: int = Field(ge=0)
    # Model server only: per-stage timings (stt, llm, tts, queue) in ms.
    stage_ms: dict[str, int] | None = None
