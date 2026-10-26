"""DynamoDB call log and SQS DLQ, against moto's in-process AWS fake.

These prove the AWS backends honour the same contract as SQLite (upsert semantics, write-once
fields, DLQ lifecycle, lookup) without touching a real account. They run in CI with fake
credentials; nothing here can reach AWS.
"""

import asyncio
import json
from dataclasses import dataclass

import boto3
import httpx
import pytest
from botocore.exceptions import ReadTimeoutError
from moto import mock_aws

from service.aws_io import AwsIO
from service.config import Settings
from service.main import create_app
from service.metrics import Metrics
from service.models import Outcome
from service.replay import replay_dead_letters
from service.replay_worker import WorkerStats, run_worker, run_workers
from service.resilience.breaker import CircuitBreaker
from service.resilience.executor import CallExecutor
from service.resilience.retry import RetryPolicy
from service.store.dynamo_call_log import DynamoCallLog
from service.store.sqs_dlq import LONG_POLL_S, QueuedEvent, SqsDLQ
from tests.conftest import OK, FakeClock, FakePipeline, make_event, r503

REGION = "ap-south-1"
CALL_TABLE = "test-call-log"
DLQ_TABLE = "test-dead-letters"


@dataclass
class AwsEnv:
    io: AwsIO
    queue_url: str

    def call_log(self) -> DynamoCallLog:
        return DynamoCallLog(self.io, CALL_TABLE)

    def dlq(self) -> SqsDLQ:
        return SqsDLQ(self.io, DLQ_TABLE, self.queue_url)

    def queue_depth(self) -> int:
        attrs = boto3.client("sqs", region_name=REGION).get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )
        return int(attrs["Attributes"]["ApproximateNumberOfMessages"])


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        for table in (CALL_TABLE, DLQ_TABLE):
            ddb.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        url = boto3.client("sqs", region_name=REGION).create_queue(QueueName="test-dlq")["QueueUrl"]
        io = AwsIO(REGION)
        yield AwsEnv(io, url)
        io.close()


def executor(aws: AwsEnv, pipeline: FakePipeline, clock: FakeClock) -> CallExecutor:
    return CallExecutor(
        pipeline=pipeline,
        breaker=CircuitBreaker(clock=clock),
        call_log=aws.call_log(),
        dlq=aws.dlq(),
        metrics=Metrics(),
        policy=RetryPolicy(),
        outcome_seed=7,
        sleep=clock.sleep,
        clock=clock,
    )


async def test_call_log_upsert_keeps_first_received_at(aws: AwsEnv, clock: FakeClock) -> None:
    log = aws.call_log()
    failed = await executor(aws, FakePipeline(default=r503()), clock).handle_call(make_event())
    assert (await log.get("evt-1")) == failed
    raw = boto3.client("dynamodb", region_name=REGION).get_item(
        TableName=CALL_TABLE, Key={"event_id": {"S": "evt-1"}}
    )["Item"]
    first_received = raw["received_at"]["S"]

    replayed = await executor(aws, FakePipeline([OK]), clock).handle_call(make_event(), replay=True)
    stored = await log.get("evt-1")
    raw = boto3.client("dynamodb", region_name=REGION).get_item(
        TableName=CALL_TABLE, Key={"event_id": {"S": "evt-1"}}
    )["Item"]
    assert stored == replayed and stored.outcome is not Outcome.FAILED_DOWNSTREAM
    assert raw["received_at"]["S"] == first_received
    assert stored.product_type is not None
    assert await log.get("missing") is None


async def test_call_log_lookup_stats_and_health(aws: AwsEnv, clock: FakeClock) -> None:
    ex = executor(aws, FakePipeline(), clock)
    for i in range(250):  # more than one BatchGetItem page (100 keys)
        await ex.handle_call(make_event(f"e{i}", product_type=None))
    log = aws.call_log()
    ids = [f"e{i}" for i in range(250)] + ["e0", "ghost"]  # duplicates must be tolerated
    assert await log.existing_ids(ids) == {f"e{i}" for i in range(250)}
    stats = await log.stats()
    assert stats["received"] == 250
    assert stats["succeeded_first_try"] == 250
    assert stats["failed_downstream"] == 0
    assert await log.healthy()
    assert not await DynamoCallLog(aws.io, "no-such-table").healthy()


async def test_dlq_lifecycle_and_single_message_per_pending_entry(aws: AwsEnv) -> None:
    dlq = aws.dlq()
    payload = make_event("evt-9").model_dump(mode="json", exclude_none=True)
    await dlq.enqueue("evt-9", payload, "retries_exhausted", 3, "http_503:x")
    entry = await dlq.get("evt-9")
    assert entry is not None
    assert (entry.status, entry.attempts, entry.replay_count) == ("pending", 3, 0)
    assert entry.payload == payload
    assert aws.queue_depth() == 1

    # Client retry after a 500, or a failed replay, re-enqueues while still pending.
    await dlq.enqueue("evt-9", payload, "retries_exhausted", 3, "http_503:y")
    assert aws.queue_depth() == 1

    await dlq.mark_replay_failed("evt-9")
    await dlq.enqueue("evt-9", payload, "circuit_open", 0, None)
    entry = await dlq.get("evt-9")
    assert entry is not None and entry.last_error is None and entry.replay_count == 1
    assert aws.queue_depth() == 2  # a new retry is owed: tell the worker again

    await dlq.enqueue("evt-10", payload | {"event_id": "evt-10"}, "non_retryable:http_400", 1, "x")
    assert [e.event_id for e in await dlq.list(reason="non_retryable")] == ["evt-10"]
    assert [e.event_id for e in await dlq.list(status="pending")] == ["evt-9", "evt-10"]
    await dlq.mark_replayed("evt-10")
    assert await dlq.stats() == {"pending": 1, "replayed": 1, "replay_failed": 0, "total": 2}
    assert await dlq.existing_ids(["evt-9", "evt-10", "nope"]) == {"evt-9", "evt-10"}
    assert await dlq.get("nope") is None


async def test_failed_call_lands_in_both_and_replay_converges(
    aws: AwsEnv, clock: FakeClock
) -> None:
    failing = executor(aws, FakePipeline(default=r503()), clock)
    result = await failing.handle_call(make_event())
    dlq = aws.dlq()
    assert result.dlq_reason == "retries_exhausted"
    assert (await dlq.get("evt-1")).status == "pending"  # type: ignore[union-attr]

    healthy = executor(aws, FakePipeline(), clock)
    summary = await replay_dead_letters(
        healthy, dlq, Metrics(), replay_all=True, rate_per_s=1000, sleep=clock.sleep
    )
    assert summary.replayed == 1
    assert (await dlq.get("evt-1")).status == "replayed"  # type: ignore[union-attr]
    assert (await aws.call_log().stats())["failed_downstream"] == 0


async def test_api_with_aws_backends(aws: AwsEnv) -> None:
    settings = Settings(
        call_log_backend="dynamodb",
        dlq_backend="sqs",
        aws_region=REGION,
        call_log_table=CALL_TABLE,
        dead_letters_table=DLQ_TABLE,
        sqs_queue_url=aws.queue_url,
        outcome_seed=1,
        backoff_base_s=0.0,
    )
    app = create_app(settings, pipeline=FakePipeline(default=r503()), setup_logging=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc")
    event = make_event("a1f2c3d4-0001").model_dump(mode="json")
    async with app.router.lifespan_context(app), client:
        assert (await client.post("/calls", json=event)).json()["outcome"] == "failed_downstream"
        assert (await client.get("/ready")).json()["call_log_writable"] is True
        lookup = await client.post("/calls/lookup", json={"event_ids": ["a1f2c3d4-0001", "zz"]})
        assert lookup.json() == {"requested": 2, "in_call_log": 1, "in_dlq": 1, "missing": ["zz"]}
        stats = (await client.get("/stats")).json()
        assert (stats["received"], stats["dlq_pending"]) == (1, 1)


# ---------- replay worker ----------


async def drain(aws: AwsEnv, ex: CallExecutor, clock: FakeClock) -> WorkerStats:
    return await run_worker(
        ex,
        aws.dlq(),
        Metrics(),
        rate_per_s=1000,
        stop=asyncio.Event(),
        exit_when_empty=True,
        wait_s=0,
        sleep=clock.sleep,
    )


async def test_worker_drains_queue_when_dependency_recovers(aws: AwsEnv, clock: FakeClock) -> None:
    failing = executor(aws, FakePipeline(default=r503()), clock)
    for i in range(3):
        await failing.handle_call(make_event(f"e{i}"))
    assert aws.queue_depth() == 3

    stats = await drain(aws, executor(aws, FakePipeline(), clock), clock)
    assert stats.replayed == 3 and stats.failed == 0
    assert aws.queue_depth() == 0
    assert (await aws.dlq().stats())["replayed"] == 3
    assert (await aws.call_log().stats())["failed_downstream"] == 0


async def test_worker_keeps_messages_while_dependency_is_down(
    aws: AwsEnv, clock: FakeClock
) -> None:
    failing = executor(aws, FakePipeline(default=r503()), clock)
    await failing.handle_call(make_event("e1"))

    stats = await drain(aws, executor(aws, FakePipeline(default=r503()), clock), clock)
    assert stats.failed == 1 and stats.replayed == 0
    entry = await aws.dlq().get("e1")
    assert entry is not None and entry.status == "replay_failed"
    # Not deleted: hidden for the visibility timeout, then retried.
    attrs = boto3.client("sqs", region_name=REGION).get_queue_attributes(
        QueueUrl=aws.queue_url, AttributeNames=["ApproximateNumberOfMessagesNotVisible"]
    )
    assert attrs["Attributes"]["ApproximateNumberOfMessagesNotVisible"] == "1"


async def test_worker_replays_orphan_message_and_dedupes(aws: AwsEnv, clock: FakeClock) -> None:
    sqs = boto3.client("sqs", region_name=REGION)
    payload = make_event("orphan").model_dump(mode="json", exclude_none=True)
    body = json.dumps({"event_id": "orphan", "reason": "x", "payload": payload})
    sqs.send_message(QueueUrl=aws.queue_url, MessageBody=body)
    sqs.send_message(QueueUrl=aws.queue_url, MessageBody=body)

    stats = await drain(aws, executor(aws, FakePipeline(), clock), clock)
    assert stats.recovered_from_payload == 1 and stats.duplicates_deleted == 1
    assert await aws.call_log().get("orphan") is not None
    assert aws.queue_depth() == 0


async def test_concurrent_loops_drain_without_double_counting(
    aws: AwsEnv, clock: FakeClock
) -> None:
    failing = executor(aws, FakePipeline(default=r503()), clock)
    for i in range(25):
        await failing.handle_call(make_event(f"e{i}"))

    stats = await run_workers(
        4,
        executor(aws, FakePipeline(), clock),
        aws.dlq(),
        Metrics(),
        rate_per_s=1000,
        stop=asyncio.Event(),
        exit_when_empty=True,
        wait_s=0,
        sleep=clock.sleep,
    )
    assert stats.replayed == 25 and aws.queue_depth() == 0
    assert (await aws.dlq().stats())["replayed"] == 25


def test_long_poll_client_outlasts_the_poll(aws: AwsEnv) -> None:
    """Regression: with the shared 5 s read timeout every empty 20 s long poll raised
    ReadTimeoutError and crashed the worker on AWS (moto answers instantly, so only a
    config check catches it)."""
    dlq = aws.dlq()
    assert dlq._sqs_poll.meta.config.read_timeout > LONG_POLL_S
    assert dlq._sqs.meta.config.read_timeout == 5


async def test_worker_survives_transient_aws_errors(aws: AwsEnv, clock: FakeClock) -> None:
    failing = executor(aws, FakePipeline(default=r503()), clock)
    await failing.handle_call(make_event("e1"))
    dlq = aws.dlq()
    real_receive = dlq.receive
    calls = 0

    async def flaky_receive(**kwargs: int) -> list[QueuedEvent]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReadTimeoutError(endpoint_url="https://sqs")
        return await real_receive(**kwargs)

    dlq.receive = flaky_receive  # type: ignore[method-assign]
    stats = await run_worker(
        executor(aws, FakePipeline(), clock),
        dlq,
        Metrics(),
        rate_per_s=1000,
        stop=asyncio.Event(),
        exit_when_empty=True,
        wait_s=0,
        sleep=clock.sleep,
    )
    assert stats.replayed == 1 and aws.queue_depth() == 0


def test_sqs_backend_requires_queue_url() -> None:
    with pytest.raises(ValueError, match="SQS_QUEUE_URL"):
        Settings(dlq_backend="sqs")
