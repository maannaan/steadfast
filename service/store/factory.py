"""Pick the call-log and DLQ backends from settings.

Local and CI runs use SQLite (no cloud needed); on AWS the orchestrator tasks share
DynamoDB and SQS. The choice is configuration only, so the same image runs in both places.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from service.config import Settings
from service.store.call_log import CallLogBackend, SqliteCallLog
from service.store.db import Database
from service.store.dlq import DLQBackend, SqliteDLQ


@dataclass
class Stores:
    call_log: CallLogBackend
    dlq: DLQBackend
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for close in reversed(self._closers):
            close()


def build_stores(settings: Settings) -> Stores:
    closers: list[Callable[[], None]] = []
    db: Database | None = None
    aws = None

    if "sqlite" in (settings.call_log_backend, settings.dlq_backend):
        db = Database(settings.db_path, settings.db_synchronous)
        db.open()
        closers.append(db.close)
    if settings.call_log_backend == "dynamodb" or settings.dlq_backend == "sqs":
        # Imported lazily so local runs never need AWS credentials or region config.
        from service.aws_io import AwsIO

        aws = AwsIO(settings.aws_region)
        closers.append(aws.close)

    call_log: CallLogBackend
    if settings.call_log_backend == "dynamodb":
        from service.store.dynamo_call_log import DynamoCallLog

        call_log = DynamoCallLog(aws, settings.call_log_table)
    else:
        call_log = SqliteCallLog(db)

    dlq: DLQBackend
    if settings.dlq_backend == "sqs":
        from service.store.sqs_dlq import SqsDLQ

        dlq = SqsDLQ(aws, settings.dead_letters_table, settings.sqs_queue_url or "")
    else:
        dlq = SqliteDLQ(db)

    return Stores(call_log=call_log, dlq=dlq, _closers=closers)
