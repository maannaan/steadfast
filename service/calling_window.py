"""When replays may run: the permitted calling hours for borrowers.

Why a gate on replay: on a real deployment a replayed call is a borrower being dialled
again. RBI's conduct rules for recovery agents restrict calls to roughly 08:00-19:00
(verify current wording), so a DLQ backlog drained at 21:00 would put calls outside
permitted hours. Live traffic is the dialler's responsibility; replay is ours.

The window carries its own UTC offset ("08:00-19:00+05:30"): India has no daylight saving,
and a fixed offset needs no timezone database in the slim container image.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

_PATTERN = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})([+-])(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class CallingWindow:
    start: time
    end: time
    tz: timezone

    @classmethod
    def parse(cls, spec: str) -> "CallingWindow | None":
        """Parse "HH:MM-HH:MM+HH:MM". "off" (any case) means no gate and returns None."""
        if spec.strip().lower() == "off":
            return None
        m = _PATTERN.match(spec.strip())
        if not m:
            raise ValueError(f"calling window must look like 08:00-19:00+05:30 or off: {spec!r}")
        sh, sm, eh, em, sign, oh, om = m.groups()
        offset = timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
        start, end = time(int(sh), int(sm)), time(int(eh), int(em))
        if start >= end:
            raise ValueError(f"calling window must end after it starts: {spec!r}")
        return cls(start, end, timezone(offset))

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz).time()
        return self.start <= local < self.end

    def seconds_until_open(self, now: datetime) -> float:
        """0 inside the window; otherwise seconds until the next opening."""
        if self.is_open(now):
            return 0.0
        local = now.astimezone(self.tz)
        opening = local.replace(
            hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0
        )
        if opening <= local:
            opening += timedelta(days=1)
        return (opening - local).total_seconds()
