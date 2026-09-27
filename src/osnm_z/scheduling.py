"""System-UTC launch scheduling."""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

NANOSECONDS_PER_MILLISECOND = 1_000_000
NANOSECONDS_PER_SECOND = 1_000_000_000
_FINAL_YIELD_WINDOW_NS = 20_000_000 if sys.platform == "win32" else 0


@dataclass(frozen=True, slots=True)
class SystemUtcClock:
    """Read the production host's ordinary Unix UTC clock."""

    def now_ns(self) -> int:
        return time.time_ns()

    def now_seconds(self) -> int:
        return self.now_ns() // NANOSECONDS_PER_SECOND


def validate_utc_timestamp_ns(utc_timestamp_ns: int) -> int:
    if utc_timestamp_ns < 0:
        raise ValueError("UTC deadline must not be negative")
    return utc_timestamp_ns


def format_launch_delta_ms(target_ns: int, observed_ns: int) -> str:
    delta_ms = (observed_ns - target_ns) / NANOSECONDS_PER_MILLISECOND
    prefix = "T+" if delta_ms >= 0 else "T-"
    return f"{prefix}{abs(delta_ms):.3f} ms"


@dataclass(frozen=True, slots=True)
class LaunchClock:
    """A launch target expressed directly in system UTC."""

    target_utc_ns: int

    def __post_init__(self) -> None:
        validate_utc_timestamp_ns(self.target_utc_ns)

    async def wait_until_lead_ns(self, lead_ns: int = 0) -> None:
        if lead_ns < 0:
            raise ValueError("launch lead must not be negative")
        await wait_until_utc_ns(max(0, self.target_utc_ns - lead_ns))

    def now_ns(self) -> int:
        return time.time_ns()


async def wait_until_utc_ns(target_utc_ns: int) -> None:
    """Wait for system UTC, yielding near Windows deadlines to avoid timer rounding."""
    validate_utc_timestamp_ns(target_utc_ns)
    while True:
        remaining_ns = target_utc_ns - time.time_ns()
        if remaining_ns <= 0:
            return
        sleep_ns = max(0, remaining_ns - _FINAL_YIELD_WINDOW_NS)
        await asyncio.sleep(min(sleep_ns / NANOSECONDS_PER_SECOND, 60.0))


_RFC3339 = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?(?P<zone>Z|[+-]\d{2}:\d{2})\Z"
)


def parse_rfc3339_ns(value: str) -> int:
    """Parse an RFC 3339 timestamp exactly, retaining up to nanosecond precision."""
    matched = _RFC3339.fullmatch(value)
    if matched is None:
        raise ValueError
    zone = "+00:00" if matched["zone"] == "Z" else matched["zone"]
    if int(zone[1:3]) > 23 or int(zone[4:6]) > 59:
        raise ValueError("invalid UTC offset")
    parsed = datetime.fromisoformat(matched["date"] + zone)
    if parsed.tzinfo is None:
        raise ValueError
    delta = parsed.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    seconds = delta.days * 86_400 + delta.seconds
    fraction = (matched["fraction"] or "").ljust(9, "0")
    timestamp_ns = seconds * NANOSECONDS_PER_SECOND + int(fraction)
    if timestamp_ns < 0:
        raise ValueError
    return timestamp_ns
