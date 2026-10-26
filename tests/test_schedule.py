import random
from datetime import UTC, datetime

import pytest

from loadgen.events import make_event
from loadgen.report import percentile
from loadgen.schedule import expected_calls, parse_hhmm, rate_per_min
from service.models import CallEvent


@pytest.mark.parametrize(
    ("hhmmss", "rate"),
    [
        ("03:00:00", 10),
        ("08:00:00", 10),  # ramp starts at baseline
        ("08:07:30", 30),  # halfway up the 15-minute ramp
        ("08:15:00", 50),
        ("09:00:00", 50),
        ("09:59:59", 50),
        ("10:00:00", 10),
        ("12:00:00", 10),
        ("14:15:00", 50),
        ("23:59:00", 10),
    ],
)
def test_rate_matches_appendix_c(hhmmss: str, rate: float) -> None:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    assert rate_per_min(h * 60 + m + s / 60) == pytest.approx(rate)


def test_schedule_repeats_daily() -> None:
    assert rate_per_min(24 * 60 + 9 * 60) == 50


def test_daily_volume() -> None:
    # 20h x 600 + 2 windows x (15 min ramp avg 30/min + 105 min at 50/min) = 23,400
    assert expected_calls(0, 24 * 60) == pytest.approx(23_400, rel=0.001)


def test_parse_hhmm() -> None:
    assert parse_hhmm("07:45") == 465
    with pytest.raises(ValueError):
        parse_hhmm("25:00")


def test_events_are_valid_seeded_and_uniquely_identified() -> None:
    ts = datetime(2026, 9, 1, 9, tzinfo=UTC)
    a = [make_event(random.Random(5), "CAMP-2026-09-A", ts) for _ in range(3)]
    without_ids = [{k: v for k, v in e.items() if k != "event_id"} for e in a]
    assert without_ids[0] == without_ids[1] == without_ids[2]  # seed fixes attributes
    assert len({e["event_id"] for e in a}) == 3  # but every run sends new calls
    event = CallEvent.model_validate(a[0])
    assert event.outcome is None
    assert a[0]["timestamp"].endswith("Z")


def test_percentile_nearest_rank() -> None:
    values = sorted(float(v) for v in range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 99) == 99
    assert percentile([], 50) is None
