"""Synthetic call events in the Appendix A shape (no real borrower data, ever).

Inputs carry no outcome: deciding what happened on a call is the service's job.
"""

import random
import uuid
from datetime import datetime
from typing import Any

BUCKETS = ("0-30 DPD", "31-60 DPD", "61-90 DPD", "90+ DPD")
BUCKET_WEIGHTS = (40, 30, 20, 10)  # most collections volume is early-stage delinquency
PRODUCTS = ("credit_card", "bnpl", "personal_loan")


def make_event(rng: random.Random, campaign_id: str, timestamp: datetime) -> dict[str, Any]:
    return {
        # Always a fresh uuid4, even in seeded runs: re-running the same seed against the same
        # database must send NEW calls, not duplicates the service answers from call_log.
        # The seed still fixes arrival times and event attributes.
        "event_id": str(uuid.uuid4()),
        "campaign_id": campaign_id,
        "borrower_bucket": rng.choices(BUCKETS, weights=BUCKET_WEIGHTS)[0],
        "product_type": rng.choice(PRODUCTS),
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
    }
