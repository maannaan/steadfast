"""Call log in DynamoDB, shared by every orchestrator task behind the load balancer.

Why DynamoDB: with several Fargate tasks, a per-task SQLite file means a lookup through the
ALB lands on a random task (so "zero unaccounted" cannot be proven) and rows vanish when a
task stops. One on-demand table keyed by event_id fixes both, costs pennies at this volume,
and is reached through a free VPC gateway endpoint (no NAT data charges).

Semantics match SqliteCallLog: one item per event_id, upserted. Fields describing the
event and when it first arrived are written only once (if_not_exists), so a DLQ replay
updates the outcome without rewriting history.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from service.aws_io import AwsIO
from service.models import CallResult, Outcome
from service.store.dynamo_util import existing_keys

_serialize = TypeSerializer().serialize
_deserialize = TypeDeserializer().deserialize

WRITE_ONCE = ("campaign_id", "borrower_bucket", "product_type", "event_timestamp", "received_at")


class DynamoCallLog:
    def __init__(self, aws: AwsIO, table: str) -> None:
        self._aws = aws
        self._client = aws.client("dynamodb")
        self._table = table

    async def upsert(
        self, result: CallResult, received_at: datetime, finished_at: datetime
    ) -> None:
        values: dict[str, Any] = {
            "campaign_id": result.campaign_id,
            "borrower_bucket": result.borrower_bucket.value,
            "product_type": result.product_type.value if result.product_type else None,
            "event_timestamp": result.timestamp.isoformat(),
            "received_at": received_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "outcome": result.outcome.value,
            "retry_count": result.retry_count,
            "call_duration_s": result.call_duration_s,
            "latency_ms": result.latency_ms,
            "status": result.status,
            "dlq_reason": result.dlq_reason,
        }
        assignments = [
            f"#{k} = if_not_exists(#{k}, :{k})" if k in WRITE_ONCE else f"#{k} = :{k}"
            for k in values
        ]
        await self._aws.run(
            self._client.update_item,
            TableName=self._table,
            Key={"event_id": {"S": result.event_id}},
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames={f"#{k}": k for k in values},
            ExpressionAttributeValues={f":{k}": _serialize(v) for k, v in values.items()},
        )

    async def get(self, event_id: str) -> CallResult | None:
        # Strongly consistent: the executor's duplicate check must see a write that another
        # task made a moment ago.
        response = await self._aws.run(
            self._client.get_item,
            TableName=self._table,
            Key={"event_id": {"S": event_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return _item_to_result(item) if item else None

    async def existing_ids(self, event_ids: list[str]) -> set[str]:
        return await existing_keys(self._aws, self._client, self._table, event_ids)

    async def stats(self) -> dict[str, int]:
        """Totals by scanning the table: fine for an operator endpoint at demo volume
        (tens of thousands of small items), never called on the hot path."""

        def scan() -> dict[str, int]:
            totals = {"received": 0, "first_try": 0, "after_retry": 0, "failed": 0}
            paginator = self._client.get_paginator("scan")
            pages = paginator.paginate(
                TableName=self._table,
                ProjectionExpression="#s, retry_count, outcome",
                ExpressionAttributeNames={"#s": "status"},
            )
            for page in pages:
                for item in page["Items"]:
                    totals["received"] += 1
                    retries = int(item["retry_count"]["N"])
                    if item["status"]["S"] == "succeeded":
                        totals["first_try" if retries == 0 else "after_retry"] += 1
                    if item["outcome"]["S"] == Outcome.FAILED_DOWNSTREAM.value:
                        totals["failed"] += 1
            return totals

        t = await self._aws.run(scan)
        return {
            "received": t["received"],
            "succeeded_first_try": t["first_try"],
            "succeeded_after_retry": t["after_retry"],
            "failed_downstream": t["failed"],
        }

    async def healthy(self) -> bool:
        try:
            await self._aws.run(
                self._client.get_item,
                TableName=self._table,
                Key={"event_id": {"S": "__readiness_probe__"}},
            )
        except Exception:
            return False
        return True


def _item_to_result(item: dict[str, Any]) -> CallResult:
    data = {k: _deserialize(v) for k, v in item.items()}
    data = {k: int(v) if isinstance(v, Decimal) else v for k, v in data.items()}
    return CallResult(
        event_id=data["event_id"],
        campaign_id=data["campaign_id"],
        borrower_bucket=data["borrower_bucket"],
        product_type=data.get("product_type"),
        timestamp=data["event_timestamp"],
        outcome=data["outcome"],
        call_duration_s=data["call_duration_s"],
        retry_count=data["retry_count"],
        latency_ms=data["latency_ms"],
        status=data["status"],
        dlq_reason=data.get("dlq_reason"),
    )
