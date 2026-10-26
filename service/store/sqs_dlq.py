"""DLQ on AWS: an SQS queue for the replay work, plus a DynamoDB table as the index.

Why both: SQS is the natural "work waiting to be retried" primitive (its queue depth is a
free CloudWatch metric to alarm on, and a worker can consume it), but you cannot look up,
list or filter messages in SQS. Operators need /dlq to answer "why did this call fail?"
and the report needs "is this event_id in the DLQ?", so every entry is also kept in a
DynamoDB table keyed by event_id. The table is the source of truth for status; a message
is a pointer saying "this event needs a replay".

Enqueue order: SQS message first, then the index. If the index write fails, the executor
answers 500 and the client retries; the message (which carries the full payload) still
exists, so nothing is lost. The reverse order could leave an index entry that no worker
would ever be told about.

A message is sent only when the entry is not already pending. A failed replay goes back
through enqueue (it is the same executor path) while its message is still outstanding, and
a client retry after a 500 does the same; both would otherwise duplicate the message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.types import TypeDeserializer

from service.aws_io import AwsIO
from service.store.dlq import DeadLetter, DLQStatus
from service.store.dynamo_util import existing_keys

_deserialize = TypeDeserializer().deserialize

LONG_POLL_S = 20  # SQS maximum


class SqsDLQ:
    def __init__(self, aws: AwsIO, table: str, queue_url: str) -> None:
        self._aws = aws
        self._ddb = aws.client("dynamodb")
        self._sqs = aws.client("sqs")
        # Receives only: its socket must outlast the long poll, while sends on the call
        # path keep the tight default so a hung SQS call fails fast.
        self._sqs_poll = aws.client("sqs", read_timeout=LONG_POLL_S + 10)
        self._table = table
        self._queue_url = queue_url

    async def enqueue(
        self,
        event_id: str,
        payload: dict[str, Any],
        reason: str,
        attempts: int,
        last_error: str | None,
    ) -> None:
        current = await self._get_item(event_id, consistent=True, projection="#s")
        if current is None or current["status"]["S"] != "pending":
            body = json.dumps({"event_id": event_id, "reason": reason, "payload": payload})
            await self._aws.run(self._sqs.send_message, QueueUrl=self._queue_url, MessageBody=body)

        now = _now()
        await self._aws.run(
            self._ddb.update_item,
            TableName=self._table,
            Key={"event_id": {"S": event_id}},
            UpdateExpression=(
                "SET payload_json = :p, reason = :r, attempts = :a, last_error = :e, "
                "last_failed_at = :now, #s = :pending, "
                "first_failed_at = if_not_exists(first_failed_at, :now), "
                "replay_count = if_not_exists(replay_count, :zero)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":p": {"S": json.dumps(payload)},
                ":r": {"S": reason},
                ":a": {"N": str(attempts)},
                ":e": {"S": last_error} if last_error is not None else {"NULL": True},
                ":now": {"S": now},
                ":pending": {"S": "pending"},
                ":zero": {"N": "0"},
            },
        )

    async def list(
        self, status: DLQStatus | None = None, reason: str | None = None, limit: int = 100
    ) -> list[DeadLetter]:
        """Scan with filters, sorted like SqliteDLQ. A scan is acceptable here: the DLQ is
        small by design (failures only) and this is an operator/replay path, not per-call."""
        filters, values = [], {}
        kwargs: dict[str, Any] = {"TableName": self._table}
        if status:
            filters.append("#s = :status")
            values[":status"] = {"S": status}
            # DynamoDB rejects placeholder names that the expression does not use.
            kwargs["ExpressionAttributeNames"] = {"#s": "status"}
        if reason:
            filters.append("begins_with(reason, :reason)")
            values[":reason"] = {"S": reason}
        if filters:
            kwargs |= {
                "FilterExpression": " AND ".join(filters),
                "ExpressionAttributeValues": values,
            }

        def scan() -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for page in self._ddb.get_paginator("scan").paginate(**kwargs):
                items.extend(page["Items"])
            return items

        entries = [_item_to_dead_letter(i) for i in await self._aws.run(scan)]
        entries.sort(key=lambda e: (e.first_failed_at, e.event_id))
        return entries[:limit]

    async def get(self, event_id: str) -> DeadLetter | None:
        item = await self._get_item(event_id, consistent=True)
        return _item_to_dead_letter(item) if item else None

    async def mark_replayed(self, event_id: str) -> None:
        await self._set_status(event_id, "replayed")

    async def mark_replay_failed(self, event_id: str) -> None:
        await self._set_status(event_id, "replay_failed")

    async def _set_status(self, event_id: str, status: DLQStatus) -> None:
        await self._aws.run(
            self._ddb.update_item,
            TableName=self._table,
            Key={"event_id": {"S": event_id}},
            UpdateExpression="SET #s = :s ADD replay_count :one",
            ConditionExpression="attribute_exists(event_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": {"S": status}, ":one": {"N": "1"}},
        )

    async def stats(self) -> dict[str, int]:
        def scan() -> dict[str, int]:
            counts: dict[str, int] = {}
            pages = self._ddb.get_paginator("scan").paginate(
                TableName=self._table,
                ProjectionExpression="#s",
                ExpressionAttributeNames={"#s": "status"},
            )
            for page in pages:
                for item in page["Items"]:
                    s = item["status"]["S"]
                    counts[s] = counts.get(s, 0) + 1
            return counts

        counts = await self._aws.run(scan)
        return {
            "pending": counts.get("pending", 0),
            "replayed": counts.get("replayed", 0),
            "replay_failed": counts.get("replay_failed", 0),
            "total": sum(counts.values()),
        }

    async def existing_ids(self, event_ids: list[str]) -> set[str]:
        return await existing_keys(self._aws, self._ddb, self._table, event_ids)

    # ---------- used by the replay worker ----------

    async def receive(self, max_messages: int = 10, wait_s: int = LONG_POLL_S) -> list[QueuedEvent]:
        """Long-poll the queue. Received messages stay invisible to other consumers for the
        queue's visibility timeout; one not deleted (a failed replay) comes back later."""
        resp = await self._aws.run(
            self._sqs_poll.receive_message,
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_s,
        )
        messages = []
        for m in resp.get("Messages", []):
            body = json.loads(m["Body"])
            messages.append(QueuedEvent(body["event_id"], body.get("payload"), m["ReceiptHandle"]))
        return messages

    async def delete(self, message: QueuedEvent) -> None:
        await self._aws.run(
            self._sqs.delete_message, QueueUrl=self._queue_url, ReceiptHandle=message.receipt
        )

    async def release(self, message: QueuedEvent) -> None:
        """Make a message visible again now (e.g. not attempted because the worker stopped),
        rather than hiding it for the rest of the visibility timeout."""
        await self._aws.run(
            self._sqs.change_message_visibility,
            QueueUrl=self._queue_url,
            ReceiptHandle=message.receipt,
            VisibilityTimeout=0,
        )

    async def _get_item(
        self, event_id: str, *, consistent: bool, projection: str | None = None
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "Key": {"event_id": {"S": event_id}},
            "ConsistentRead": consistent,
        }
        if projection:
            kwargs |= {
                "ProjectionExpression": projection,
                "ExpressionAttributeNames": {"#s": "status"},
            }
        resp = await self._aws.run(self._ddb.get_item, **kwargs)
        return resp.get("Item")


@dataclass(frozen=True)
class QueuedEvent:
    """One received SQS message. `payload` lets the worker replay even if the index entry
    is missing (the index write failed after the send)."""

    event_id: str
    payload: dict[str, Any] | None
    receipt: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _item_to_dead_letter(item: dict[str, Any]) -> DeadLetter:
    data = {k: _deserialize(v) for k, v in item.items()}
    return DeadLetter(
        event_id=data["event_id"],
        payload=json.loads(data["payload_json"]),
        reason=data["reason"],
        attempts=int(data["attempts"]),
        last_error=data.get("last_error"),
        first_failed_at=data["first_failed_at"],
        last_failed_at=data["last_failed_at"],
        status=data["status"],
        replay_count=int(data["replay_count"]),
    )
