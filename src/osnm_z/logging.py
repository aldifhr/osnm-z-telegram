"""Compact UTC terminal logging and async progress animation."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
import unicodedata
from collections.abc import Awaitable
from datetime import UTC, datetime
from enum import Enum
from typing import TextIO

from .scheduling import NANOSECONDS_PER_SECOND

SPINNER_FRAMES = ("⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷")
SPINNER_INTERVAL = 0.08
MAX_LOG_LINES = 2
_ETHEREUM_IDENTIFIER = re.compile(r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])")
_LOG_VALUE = re.compile(
    rf"(?P<address>{_ETHEREUM_IDENTIFIER.pattern})"
    r"|(?<!\w)(?:T[+-])?\d+(?:\.\d+)?(?:\s*(?:wei|ms|seconds?))?(?!\w)"
)
_interactive_line_depth = 0


class Style(Enum):
    CYAN_BOLD = "1;36"
    GREEN_BOLD = "1;32"
    BLUE_BOLD = "1;34"
    MAGENTA_BOLD = "1;35"
    YELLOW_BOLD = "1;33"
    RED_BOLD = "1;31"
    CYAN = "36"
    GREEN = "32"
    YELLOW = "33"
    RED = "31"
    DARK_GREY = "90"


def styled(
    message: object,
    style: Style,
    *,
    stream: TextIO | None = None,
    bold: bool = False,
    underline: bool = False,
) -> str:
    output = stream if stream is not None else sys.stdout
    force_color = os.environ.get("FORCE_COLOR") not in (None, "", "0")
    if not force_color and (os.environ.get("NO_COLOR") or not output.isatty()):
        return str(message)
    attributes = style.value + (";1" if bold else "") + (";4" if underline else "")
    return f"\x1b[{attributes}m{message}\x1b[0m"


def utc_timestamp() -> str:
    return _format_utc_timestamp(datetime.now(UTC))


def utc_timestamp_at_unix_ns(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    instant = datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanoseconds // 1_000)
    return _format_utc_timestamp(instant)


def section(title: str) -> None:
    """Separate related terminal messages with one blank line and a named heading."""
    _clear_interactive_line()
    print(flush=True)
    info(f"--- {title} ---")


def info(message: object) -> None:
    info_at_timestamp(utc_timestamp(), message)


def info_at_unix_ns(timestamp_ns: int, message: object) -> None:
    info_at_timestamp(utc_timestamp_at_unix_ns(timestamp_ns), message)


def info_at_timestamp(timestamp: str, message: object) -> None:
    _clear_interactive_line()
    print(_render_log_line("[INFO]", Style.CYAN_BOLD, timestamp, message, sys.stdout), flush=True)


def input_message(message: object) -> None:
    _clear_interactive_line()
    print(
        _render_log_line(
            "[INPUT]",
            Style.BLUE_BOLD,
            utc_timestamp(),
            message,
            sys.stdout,
            message_style=Style.MAGENTA_BOLD,
        ),
        flush=True,
    )


def success(message: object) -> None:
    _clear_interactive_line()
    print(
        _render_log_line(
            "[INFO]",
            Style.GREEN_BOLD,
            utc_timestamp(),
            message,
            sys.stdout,
            message_style=Style.GREEN,
        ),
        flush=True,
    )


def warn(message: object) -> None:
    _clear_interactive_line()
    print(
        _render_log_line(
            "[WARN]",
            Style.YELLOW_BOLD,
            utc_timestamp(),
            message,
            sys.stdout,
            message_style=Style.YELLOW,
        ),
        flush=True,
    )


def error(message: object) -> None:
    _clear_interactive_line()
    sys.stdout.flush()
    print(
        _render_log_line(
            "[ERROR]",
            Style.RED_BOLD,
            utc_timestamp(),
            message,
            sys.stderr,
            message_style=Style.RED_BOLD,
        ),
        file=sys.stderr,
        flush=True,
    )


async def animate[T](
    message: object, awaitable: Awaitable[T], *, countdown_utc_ns: int | None = None
) -> T:
    text = _single_line_message(message)
    if not sys.stdout.isatty():
        info(f"{_progress_message(text, countdown_utc_ns)}...")
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    started_at = utc_timestamp() if countdown_utc_ns is None else ""
    frame_index = 0
    _begin_interactive_line()
    try:
        while not task.done():
            line = _render_log_line(
                "[INFO]",
                Style.CYAN_BOLD,
                started_at,
                _progress_message(text, countdown_utc_ns),
                sys.stdout,
                suffix=f" {SPINNER_FRAMES[frame_index]}",
                max_lines=1,
            )
            print(
                f"\r\x1b[2K{line}",
                end="",
                flush=True,
            )
            frame_index = (frame_index + 1) % len(SPINNER_FRAMES)
            try:
                return await asyncio.wait_for(asyncio.shield(task), SPINNER_INTERVAL)
            except TimeoutError:
                pass
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _end_interactive_line()
        print("\r\x1b[2K", end="", flush=True)


def _progress_message(message: str, countdown_utc_ns: int | None) -> str:
    if countdown_utc_ns is None:
        return message
    remaining_ns = max(0, countdown_utc_ns - time.time_ns())
    seconds = (remaining_ns + NANOSECONDS_PER_SECOND - 1) // NANOSECONDS_PER_SECOND
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    remaining = (
        f"{hours}h {minutes:02d}m {seconds:02d}s"
        if hours
        else f"{minutes}m {seconds:02d}s"
        if minutes
        else f"{seconds}s"
    )
    return f"Mint starts in {remaining} | {message}"


def _begin_interactive_line() -> None:
    global _interactive_line_depth
    _interactive_line_depth += 1


def _end_interactive_line() -> None:
    global _interactive_line_depth
    _interactive_line_depth = max(0, _interactive_line_depth - 1)


def _clear_interactive_line() -> None:
    if _interactive_line_depth and sys.stdout.isatty():
        print("\r\x1b[2K", end="", flush=True)


def _single_line_message(message: object) -> str:
    printable = "".join(character if character.isprintable() else " " for character in str(message))
    return " ".join(printable.split())


def _render_log_line(
    label: str,
    label_style: Style,
    timestamp: object,
    message: object,
    stream: TextIO,
    *,
    message_style: Style = Style.CYAN,
    suffix: str = "",
    max_lines: int = MAX_LOG_LINES,
) -> str:
    label_text = _single_line_message(label)
    timestamp_text = _single_line_message(timestamp)
    message_text = _single_line_message(message)
    suffix_text = "" if not suffix else f" {_single_line_message(suffix)}"
    if stream.isatty():
        line_limit = max(1, min(max_lines, MAX_LOG_LINES))
        # A wide character can leave one unused cell when it wraps onto the next row.
        maximum_width = _terminal_columns(stream) * line_limit - (line_limit - 1)
        identifiers = " ".join(dict.fromkeys(_ETHEREUM_IDENTIFIER.findall(message_text)))
        prefix = f"{label_text} {timestamp_text} "
        suffix_width = _display_width(suffix_text)
        if identifiers and _display_width(prefix + identifiers) + suffix_width > maximum_width:
            timestamp_text = ""
            prefix = f"{label_text} "
        available_width = maximum_width - _display_width(prefix) - suffix_width
        if available_width < 0:
            return styled(
                _truncate_to_width(prefix + message_text + suffix_text, maximum_width),
                label_style,
                stream=stream,
            )
        if identifiers and _display_width(message_text) > available_width:
            # Reserve recovery identifiers before shortening the surrounding explanation.
            description = _single_line_message(_ETHEREUM_IDENTIFIER.sub("", message_text))
            description_width = available_width - _display_width(identifiers) - 1
            description = _truncate_to_width(description, description_width)
            message_text = f"{description} {identifiers}".strip()
        message_text = _truncate_to_width(message_text, available_width)
    timestamp = (
        f"{styled(timestamp_text, Style.DARK_GREY, stream=stream)} " if timestamp_text else ""
    )
    return (
        f"{styled(label_text, label_style, stream=stream)} {timestamp}"
        f"{_format_message(message_text, message_style, stream)}"
        f"{styled(suffix_text, label_style, stream=stream)}"
    )


def _format_message(message: str, style: Style, stream: TextIO) -> str:
    parts: list[str] = []
    position = 0
    for match in _LOG_VALUE.finditer(message):
        parts.append(styled(message[position : match.start()], style, stream=stream))
        parts.append(
            styled(
                match.group(),
                style,
                stream=stream,
                bold=True,
                underline=match.group("address") is not None,
            )
        )
        position = match.end()
    parts.append(styled(message[position:], style, stream=stream))
    return "".join(parts)


def _terminal_columns(stream: TextIO) -> int:
    try:
        return max(1, os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, OSError):
        return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _display_width(text: str) -> int:
    return sum(
        0
        if unicodedata.category(character) in {"Mn", "Me", "Cf"}
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in text
    )


def _truncate_to_width(text: str, maximum_width: int) -> str:
    if _display_width(text) <= maximum_width:
        return text
    if maximum_width < 1:
        return ""
    available_width = maximum_width - 1
    width = 0
    result: list[str] = []
    for character in text:
        character_width = _display_width(character)
        if width + character_width > available_width:
            break
        result.append(character)
        width += character_width
    return f"{''.join(result)}…"


def _format_utc_timestamp(instant: datetime) -> str:
    return instant.strftime("[%Y-%m-%d %H:%M:%S.") + f"{instant.microsecond // 1000:03d} UTC]"
