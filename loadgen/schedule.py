"""The Appendix C traffic profile as a pure function of simulated time of day.

00:00-08:00  10 calls/min (baseline)
08:00-10:00  ramp 10 -> 50 calls/min linearly over 15 min, then hold 50
10:00-14:00  10 calls/min
14:00-16:00  ramp 10 -> 50 over 15 min, hold 50
16:00-24:00  10 calls/min

Pure (no clock, no I/O) so it is trivially testable and the generator just samples it.
"""

BASELINE_PER_MIN = 10.0
PEAK_PER_MIN = 50.0
RAMP_MIN = 15.0
MINUTES_PER_DAY = 24 * 60
# Campaign windows as (start, end) in minutes since midnight.
CAMPAIGN_WINDOWS = ((8 * 60, 10 * 60), (14 * 60, 16 * 60))


def rate_per_min(sim_minute_of_day: float) -> float:
    """Target calls per minute at a simulated minute of the day (wraps every 24h)."""
    minute = sim_minute_of_day % MINUTES_PER_DAY
    for start, end in CAMPAIGN_WINDOWS:
        if start <= minute < end:
            into = minute - start
            if into < RAMP_MIN:
                return BASELINE_PER_MIN + (PEAK_PER_MIN - BASELINE_PER_MIN) * into / RAMP_MIN
            return PEAK_PER_MIN
    return BASELINE_PER_MIN


def parse_hhmm(value: str) -> float:
    """'07:45' -> 465.0 minutes since midnight."""
    hours, minutes = value.split(":")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time of day: {value}")
    return float(h * 60 + m)


def expected_calls(start_minute: float, duration_min: float, step: float = 0.25) -> float:
    """Integrate the profile: how many calls a run should send (for sanity checks)."""
    total, t = 0.0, 0.0
    while t < duration_min:
        total += rate_per_min(start_minute + t) * min(step, duration_min - t)
        t += step
    return total
