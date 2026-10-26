# A guard rail for the ~$30 reimbursement cap: warn at 80% of the monthly limit actually
# spent, and when the month's forecast passes 100%.
#
# Scope: once the Project cost allocation tag is active (var.activate_cost_allocation_tag),
# the budget counts only this project's resources. Before that AWS cannot filter by the tag,
# so it counts the whole account, which over-warns rather than under-warns.

resource "aws_budgets_budget" "project" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "cost_filter" {
    for_each = var.activate_cost_allocation_tag ? [1] : []
    content {
      name   = "TagKeyValue"
      values = ["user:Project$${var.name}"]
    }
  }

  dynamic "notification" {
    for_each = local.has_alert_email ? {
      ACTUAL     = 80
      FORECASTED = 100
    } : {}
    content {
      notification_type          = notification.key
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
