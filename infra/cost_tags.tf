# Lets Cost Explorer filter by Project=steadfast, so the receipts for this challenge
# exclude anything else in the account. See var.activate_cost_allocation_tag for why it
# is off on the first apply.
resource "aws_ce_cost_allocation_tag" "project" {
  count    = var.activate_cost_allocation_tag ? 1 : 0
  provider = aws.us_east_1
  tag_key  = "Project"
  status   = "Active"
}
