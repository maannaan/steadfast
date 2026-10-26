"""Circuit breaker over a rolling window of pipeline attempts.

Why a breaker at all: when the dependency is truly down, retrying every call triples the
load on it and makes every borrower wait the full retry deadline. Opening the circuit fails
calls fast (straight to the DLQ, still recorded), gives the dependency room to recover, and
probes it periodically.

Why these thresholds: the dependency's NORMAL failure rate is 20% (Appendix B). The breaker
must only react to failure rates well above that, or it "flaps" open on ordinary noise and
dumps healthy traffic into the DLQ. With a 20-attempt window and a 10-attempt minimum,
simulation showed ~18 accidental opens per day at 20% independent failure (5 failures out of
the first 10 attempts is a ~3% event).

Why a FULL window before judging (min_calls == window == 100, as in resilience4j's
defaults): partial windows are biased. A 503 comes back instantly while a success takes
~300ms, so right after startup, or right after the breaker closes and clears its window,
the first results to arrive are mostly failures. With a 20-attempt minimum the end-to-end
test saw the breaker open at a true 20% failure rate in half of its runs (10-12 instant
failures landed before the first success). Requiring 100 attempts dilutes that burst:
zero opens in simulation, both for steady 20% noise and for a 12-failure cold start. A real
outage still opens it within 50 failed attempts of a healthy window, or 100 from cold
(tests/test_breaker.py proves both).

Half-open probes are judged together, for the same reason: one probe success closes the
circuit, but it only re-opens once EVERY probe has failed. Re-opening on the first failed
probe let a fast baseline 503 beat a slower success in about half of all probe rounds, and
the outage drill saw a fully recovered mock kept behind an open breaker for 70s. A dead
dependency still fails all probes (and fast); a recovered one does so 0.2^3 = 0.8% of the time.

The window counts ATTEMPTS, not calls: each retry is a separate observation of dependency
health, so an outage is detected roughly three times faster than counting whole calls.

Concurrency: the service runs on one asyncio event loop and no method here awaits, so every
method runs atomically with respect to other coroutines. No lock is needed.
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from service.logging_setup import log_event

log = logging.getLogger("steadfast.breaker")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# Numeric encoding for the metrics gauge.
STATE_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}

TransitionHook = Callable[[BreakerState, BreakerState], None]


class CircuitBreaker:
    def __init__(
        self,
        window: int = 100,
        min_calls: int = 100,
        failure_threshold: float = 0.5,
        open_s: float = 10.0,
        half_open_max: int = 3,
        clock: Callable[[], float] = time.monotonic,
        on_transition: TransitionHook | None = None,
    ) -> None:
        self._window: deque[bool] = deque(maxlen=window)  # True = failed attempt
        self._min_calls = min_calls
        self._threshold = failure_threshold
        self._open_s = open_s
        self._half_open_max = half_open_max
        self._clock = clock
        self._on_transition = on_transition
        self._state = BreakerState.CLOSED
        self._state_since = clock()
        self._probes_issued = 0
        self._probe_failures = 0
        self._last_probe_at = self._state_since

    @property
    def state(self) -> BreakerState:
        """Current state. OPEN lazily becomes HALF_OPEN once the cool-off has elapsed.

        If every half-open probe was issued and open_s has passed since the last one without
        a verdict (e.g. the requests were cancelled), re-open so the breaker can never stall
        in HALF_OPEN.
        """
        now = self._clock()
        if self._state is BreakerState.OPEN and now - self._state_since >= self._open_s:
            self._transition(BreakerState.HALF_OPEN)
        elif (
            self._state is BreakerState.HALF_OPEN
            and self._probes_issued >= self._half_open_max
            and now - self._last_probe_at >= self._open_s
        ):
            self._transition(BreakerState.OPEN)
        return self._state

    def allow(self) -> bool:
        """May the caller make an attempt now? In HALF_OPEN each True consumes a probe slot."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._probes_issued < self._half_open_max:
            self._probes_issued += 1
            self._last_probe_at = self._clock()
            return True
        return False

    # record_* act on the raw state, not the lazy `state` property: a probe result that
    # arrives must count, not be pre-empted by the lost-probe safeguard and then ignored.
    def record_success(self) -> None:
        state = self._state
        if state is BreakerState.HALF_OPEN:
            # One good probe is enough evidence; start the window fresh so old failures
            # from the outage cannot immediately re-open the circuit.
            self._window.clear()
            self._transition(BreakerState.CLOSED)
        elif state is BreakerState.CLOSED:
            self._window.append(False)
        # In OPEN: a late result from an attempt started before opening; ignore it.

    def record_failure(self) -> None:
        state = self._state
        if state is BreakerState.HALF_OPEN:
            self._probe_failures += 1
            if self._probe_failures >= self._half_open_max:
                self._transition(BreakerState.OPEN)
        elif state is BreakerState.CLOSED:
            self._window.append(True)
            if len(self._window) >= self._min_calls and self.failure_ratio >= self._threshold:
                self._transition(BreakerState.OPEN)

    @property
    def failure_ratio(self) -> float:
        return sum(self._window) / len(self._window) if self._window else 0.0

    def _transition(self, new: BreakerState) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        self._state_since = self._clock()
        self._probes_issued = 0
        self._probe_failures = 0
        log_event(
            log,
            "breaker_transition",
            **{"from": old.value, "to": new.value, "at": datetime.now(UTC).isoformat()},
        )
        if self._on_transition:
            self._on_transition(old, new)
