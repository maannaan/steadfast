# On-demand billing: no capacity to size or pay for while idle, and bursts (the 09:00
# spike) are absorbed without throttling at this volume. Keyed by event_id, matching the
# upsert-by-event_id semantics of the SQLite tables.
#
# Point-in-time recovery: the call log is a compliance record, so it can be restored to
# any second in the last 35 days. Costs cents at demo size.

resource "aws_dynamodb_table" "call_log" {
  name         = "${var.name}-call-log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "dead_letters" {
  name         = "${var.name}-dead-letters"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
