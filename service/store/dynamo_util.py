"""DynamoDB helpers shared by the call log and the DLQ index."""

import asyncio
from typing import Any

from service.aws_io import AwsIO

BATCH_GET_MAX = 100  # DynamoDB BatchGetItem limit per request
UNPROCESSED_RETRIES = 8


async def existing_keys(aws: AwsIO, client: Any, table: str, event_ids: list[str]) -> set[str]:
    """Which event_ids exist in `table`. Used by the report to prove zero unaccounted.

    BatchGetItem may return only part of a batch under throttling (UnprocessedKeys); those
    are retried with backoff, and if some still remain we raise rather than report an
    event as missing when it was merely not read.
    """
    ids = list(dict.fromkeys(event_ids))  # BatchGetItem rejects duplicate keys
    found: set[str] = set()
    for i in range(0, len(ids), BATCH_GET_MAX):
        keys = [{"event_id": {"S": e}} for e in ids[i : i + BATCH_GET_MAX]]
        request: dict[str, Any] = {table: {"Keys": keys, "ProjectionExpression": "event_id"}}
        for attempt in range(UNPROCESSED_RETRIES):
            resp = await aws.run(client.batch_get_item, RequestItems=request)
            found |= {item["event_id"]["S"] for item in resp["Responses"].get(table, [])}
            request = resp.get("UnprocessedKeys") or {}
            if not request:
                break
            await asyncio.sleep(0.05 * 2**attempt)
        else:
            raise RuntimeError("BatchGetItem left unprocessed keys after retries")
    return found
