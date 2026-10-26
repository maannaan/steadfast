variable "region" {
  description = "AWS region. ap-south-1 (Mumbai) keeps borrower data in India."
  type        = string
  default     = "ap-south-1"
}

variable "name" {
  description = "Prefix for resource names and the value of the Project tag."
  type        = string
  default     = "steadfast"
}

variable "https_cidrs" {
  description = "Who may open HTTPS connections to the ALB. Every request must still carry the access key, so the default lets reviewers in from any network; narrow it to lock down further."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = alltrue([for c in var.https_cidrs : can(cidrhost(c, 0))])
    error_message = "https_cidrs must be valid CIDRs."
  }
}

variable "enable_cloudfront" {
  description = "Put CloudFront in front of the ALB for an HTTPS URL with a publicly trusted certificate. New AWS accounts must first be verified by AWS Support before CloudFront can be used."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Image tag for the orchestrator and mock (git SHA, set by scripts/deploy.sh)."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC address range."
  type        = string
  default     = "10.0.0.0/16"
}

variable "orchestrator_desired_count" {
  description = "Orchestrator tasks at rest, and the autoscaling floor. Two, in different AZs, so one task or AZ can fail without an outage."
  type        = number
  default     = 2
}

variable "orchestrator_max_count" {
  description = "Autoscaling ceiling. 10 x 0.5 vCPU plus the mock and replay worker fits the account's 8 vCPU Fargate quota."
  type        = number
  default     = 10
}

variable "inflight_target_per_task" {
  description = "Autoscaling target: average calls in flight per orchestrator task. At speed 6 the peak is ~135 in flight, i.e. ~7 tasks."
  type        = number
  default     = 20
}

variable "enable_scheduled_prescale" {
  description = "Raise the task floor to prescale_min_count at 07:45 IST before the morning campaign window and restore it at 10:15."
  type        = bool
  default     = false
}

variable "prescale_min_count" {
  description = "Task floor during the morning campaign window when enable_scheduled_prescale is on."
  type        = number
  default     = 5
}

variable "orchestrator_cpu" {
  description = "Fargate CPU units per orchestrator task (512 = 0.5 vCPU)."
  type        = number
  default     = 512
}

variable "orchestrator_memory" {
  description = "Fargate memory (MiB) per orchestrator task."
  type        = number
  default     = 1024
}

variable "mock_cpu" {
  description = "Fargate CPU units for the mock pipeline."
  type        = number
  default     = 512
}

variable "mock_memory" {
  description = "Fargate memory (MiB) for the mock pipeline."
  type        = number
  default     = 1024
}

variable "turns_per_call" {
  description = "Pipeline turns per call. 6 turns with a 5 s gap makes a call ~27 s long, close to a real 30-120 s call, so in-flight load (and autoscaling) is realistic. Demo setting; 1 matches the challenge spec literally."
  type        = number
  default     = 6
}

variable "turn_gap_s" {
  description = "Seconds between turns of one call (the borrower talking)."
  type        = number
  default     = 5
}

variable "shutdown_drain_s" {
  description = "How long a stopping task waits for in-flight calls. Must stay below the container stopTimeout (90 s)."
  type        = number
  default     = 60
}

variable "mock_failure_rate" {
  description = "Mock pipeline failure probability per request."
  type        = number
  default     = 0.2
}

variable "mock_failure_mode" {
  description = "independent (each request fails with mock_failure_rate) or burst (periodic total outage)."
  type        = string
  default     = "independent"

  validation {
    condition     = contains(["independent", "burst"], var.mock_failure_mode)
    error_message = "mock_failure_mode must be independent or burst."
  }
}

variable "pipeline_target" {
  description = "Which pipeline the orchestrator calls: mock (Fargate test double) or model (open-weight STT -> LLM -> TTS on EC2). scripts/model_on.sh and model_off.sh switch it."
  type        = string
  default     = "mock"

  validation {
    condition     = contains(["mock", "model"], var.pipeline_target)
    error_message = "pipeline_target must be mock or model."
  }
  validation {
    condition     = var.pipeline_target == "mock" || var.model_desired_count > 0
    error_message = "pipeline_target = model needs model_desired_count >= 1."
  }
}

variable "builder_instance_type" {
  description = "One-shot instance that builds the model image (infra/build.tf); 2 vCPU fits the standard quota next to the model host."
  type        = string
  default     = "c7i.large"
}

variable "model_compute" {
  description = "Model tier hardware: gpu (g5.xlarge, Whisper large-v3-turbo + Mistral-7B) or cpu (c7i.xlarge, Whisper small + Qwen2.5-1.5B). GPU needs a G-instance vCPU quota."
  type        = string
  default     = "cpu"

  validation {
    condition     = contains(["cpu", "gpu"], var.model_compute)
    error_message = "model_compute must be cpu or gpu."
  }
}

variable "model_desired_count" {
  description = "Model tasks (one per instance). 0 = no instance runs and the tier costs nothing."
  type        = number
  default     = 0
}

variable "model_image_tag" {
  description = "Model image tag (scripts/model_tag.sh, set by model_on.sh). Unused while model_desired_count = 0."
  type        = string
  default     = "unset"
}

variable "model_fault_rate" {
  description = "Fraction of model requests that fail with a 503 before any work (the challenge's 20% fault injection)."
  type        = number
  default     = 0.2
}

variable "model_attempt_timeout_s" {
  description = "Orchestrator per-attempt timeout against the model: ~2x the measured single-turn p99 (7.8 s on c7i.xlarge CPU; docs/aws/model-tier.md)."
  type        = number
  default     = 16
}

variable "model_queue_timeout_s" {
  description = "Longest a request waits for the model's single slot before a retryable 503. Queue wait + p99 turn (8 + 7.8 s) stays under model_attempt_timeout_s, so the model gives up before the orchestrator does and never computes an answer nobody is waiting for."
  type        = number
  default     = 8
}

variable "model_call_deadline_s" {
  description = "Orchestrator per-call deadline against the model; must stay under the ALB idle timeout (60 s) so the client always gets the answer."
  type        = number
  default     = 50
}

variable "model_turns_per_call" {
  description = "Pipeline turns per call against the model. 1 = one STT -> LLM -> TTS turn per call event, as in the challenge spec."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. 7 days covers a review cycle without paying to keep demo logs forever."
  type        = number
  default     = 7
}

variable "replay_rate_per_s" {
  description = "Replays started per second by the DLQ worker, so a drain cannot stampede a pipeline that has just recovered."
  type        = number
  default     = 5
}

variable "replay_concurrency" {
  description = "Parallel receive loops in the replay worker, each with up to 10 replays in flight. A replay lasts as long as a call (~27 s), so one loop alone drains only ~0.4 calls/s."
  type        = number
  default     = 8
}

variable "baseline_p99_ms" {
  description = "Normal p99 call latency at the ALB. The latency alarm fires at 3x this. 30000 matches the measured ~29.5 s p99 of a 6-turn call; use ~1500 with turns_per_call = 1."
  type        = number
  default     = 30000
}

variable "alert_email" {
  description = "Optional email for alarm and budget notifications (AWS sends a confirmation link first). Pass via TF_VAR_alert_email, never commit it."
  type        = string
  default     = ""
  sensitive   = true
}

variable "monthly_budget_usd" {
  description = "AWS Budgets limit for this project. Notifies at 80% actual and 100% forecast."
  type        = number
  default     = 25
}

variable "activate_cost_allocation_tag" {
  description = "Activate Project as a cost allocation tag. AWS only accepts this once the tag has appeared in billing data (up to 24 h after the first tagged resource), so enable it on a later apply."
  type        = bool
  default     = false
}
