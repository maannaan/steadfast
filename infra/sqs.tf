# Standard (not FIFO) queue: replay is idempotent by event_id, so an occasional duplicate
# delivery is harmless, and FIFO's throughput limits and 5-minute dedup window add nothing.
resource "aws_sqs_queue" "dlq" {
  name = "${var.name}-collections-dlq"

  message_retention_seconds  = 1209600 # 14 days, the SQS maximum: time to fix and replay
  visibility_timeout_seconds = 300     # a failed replay reappears after 5 minutes
  receive_wait_time_seconds  = 20      # long polling: fewer empty receives
  sqs_managed_sse_enabled    = true
}

data "aws_iam_policy_document" "dlq" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage"]
    resources = [aws_sqs_queue.dlq.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy    = data.aws_iam_policy_document.dlq.json
}
