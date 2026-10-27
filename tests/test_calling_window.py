from datetime import UTC, datetime

import pytest

from service.calling_window import CallingWindow
from service.config import Settings

IST_WINDOW = "08:00-19:00+05:30"


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 27, hour, minute, tzinfo=UTC)


def test_off_disables_the_gate() -> None:
    assert CallingWindow.parse("off") is None
    assert CallingWindow.parse(" OFF ") is None


def test_window_is_evaluated_in_its_own_offset() -> None:
    window = CallingWindow.parse(IST_WINDOW)
    assert window is not None
    assert not window.is_open(utc(2, 29))  # 07:59 IST
    assert window.is_open(utc(2, 30))  # 08:00 IST
    assert window.is_open(utc(13, 29))  # 18:59 IST
    assert not window.is_open(utc(13, 30))  # 19:00 IST: end is exclusive


def test_seconds_until_open() -> None:
    window = CallingWindow.parse(IST_WINDOW)
    assert window is not None
    assert window.seconds_until_open(utc(6)) == 0
    assert window.seconds_until_open(utc(2)) == 30 * 60  # 07:30 IST, opens same day
    assert window.seconds_until_open(utc(14, 30)) == 12 * 3600  # 20:00 IST, opens tomorrow


@pytest.mark.parametrize("spec", ["", "8-19", "08:00-19:00", "19:00-08:00+05:30", "25:00-26:00Z"])
def test_invalid_specs_are_rejected(spec: str) -> None:
    with pytest.raises(ValueError):
        CallingWindow.parse(spec)


def test_settings_validate_the_window_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().calling_window is None  # local default: off
    monkeypatch.setenv("REPLAY_CALLING_WINDOW", IST_WINDOW)
    assert Settings().calling_window == CallingWindow.parse(IST_WINDOW)
    monkeypatch.setenv("REPLAY_CALLING_WINDOW", "always")
    with pytest.raises(ValueError):
        Settings()
