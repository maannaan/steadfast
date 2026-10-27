# Swapping the mock for a real open-weight model

The resilience layer (retry, circuit breaker, DLQ, replay) depends on only the `Pipeline`
protocol in `service/pipeline/base.py` and its two error types, `RetryableError` and
`NonRetryableError`. Swapping the mock for real models therefore never touches it: you set
`PIPELINE_BACKEND=open_weight` and `PIPELINE_URL=http://model:8080`.

Behind that URL, the model server in `model/` runs faster-whisper large-v3-turbo (MIT),
Mistral-7B-Instruct-v0.3 via llama.cpp (Apache-2.0) and Piper (MIT). It serves the same
`POST /pipeline/process` contract as the mock, and it maps its own failures onto the codes
the client already understands:
- injected faults, warm-up, queue timeouts and inference errors are 503s (retryable);
- a full admission queue is a 429 (retryable);
- a malformed response is not retried.

What changes is operational:
- `/health` stays unhealthy until every model is loaded and one warm-up turn has run, so
  no call ever reaches a cold model.
- The GPU runs one turn at a time with a short queue, and rejects fast (429) beyond it, so
  latency stays bounded.
- Attempt and call timeouts are re-derived from the model's measured p99s instead of the
  mock's sub-second values.
- Every model's licence must allow an Apache-2.0 release. Piper runs as a separate process
  because it links GPL-3.0 espeak-ng, and its CC BY-NC-SA Hindi voices are excluded.

## Details

- **CPU build (tested on AWS):** faster-whisper small (int8) and Qwen2.5-1.5B-Instruct
  Q4_K_M (Apache-2.0), with the same Piper voice. It runs on a c7i.xlarge because the
  account's GPU quota was 0, so the GPU build has not run yet.
- **Error contract** (`model/app.py`):

  | Response | Meaning |
  |---|---|
  | 503 `downstream_timeout` | injected fault (`FAULT_RATE`, the 20% test) |
  | 503 `warming_up` | models still loading |
  | 429 `overloaded` | admission queue full |
  | 503 `queue_timeout` | no free slot in time |
  | 503 `model_error` | inference raised |

  `service/pipeline/http_mock.py` treats 5xx and 429 as retryable, and any other 4xx or a
  malformed body as non-retryable.
- **Timeouts on the CPU tier:**
  - `ATTEMPT_TIMEOUT_S` is 16 s, about 2× the measured attempt p99 of 7.8 s.
  - `CALL_DEADLINE_S` is 50 s, under the ALB's 60 s idle timeout.
  - The model's queue timeout is 8 s, so the model gives up before the orchestrator does.
- **Measured latency, overload behaviour and licences:**
  [aws/model-tier.md](aws/model-tier.md).
