"""Private mint-action classification and bounded RPC preparation recovery."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from enum import Enum

from . import logging
from .config import DEFAULT_OPENSEA_ATTEMPTS
from .errors import MintError
from .opensea_protocol import ErrorKind, ProtocolError, is_transient_http_status
from .phase_selection import current_unix_timestamp
from .rpc_protocol import ChainError
from .scheduling import NANOSECONDS_PER_SECOND


class PrivateActionRetryDisposition(Enum):
    TRANSIENT = "transient"
    REAUTHENTICATE = "reauthenticate"
    PREOPEN = "preopen"
    TERMINAL = "terminal"


def private_action_retry_message(disposition: PrivateActionRetryDisposition) -> str:
    """Explain why the mint-data request is being retried."""
    return {
        PrivateActionRetryDisposition.TRANSIENT: (
            "OpenSea mint-data request failed temporarily; retrying."
        ),
        PrivateActionRetryDisposition.REAUTHENTICATE: (
            "OpenSea session needs to be refreshed; retrying the mint-data request."
        ),
        PrivateActionRetryDisposition.PREOPEN: (
            "OpenSea mint data is not available yet; retrying."
        ),
        PrivateActionRetryDisposition.TERMINAL: "OpenSea mint-data request failed.",
    }[disposition]


async def retry_committed_operation[T](
    label: str,
    operation: Callable[[], Awaitable[T]],
    phase_ends_at: int | None,
    configured_attempts: int,
    retry_interval_ms: int,
    *,
    recovery_deadline_ns: int | None = None,
    attempt_timeout_seconds: Sequence[float] | None = None,
) -> T:
    """Use the recovery deadline when supplied; otherwise use the attempt budget."""
    if attempt_timeout_seconds is not None and (
        not attempt_timeout_seconds or any(value <= 0 for value in attempt_timeout_seconds)
    ):
        raise ValueError("attempt timeout ladder must contain only positive values")
    attempt_limit = max(configured_attempts, DEFAULT_OPENSEA_ATTEMPTS)
    attempt = 0
    last_error: ChainError | TimeoutError | None = None
    while True:
        if phase_ends_at is not None and current_unix_timestamp() >= phase_ends_at:
            raise MintError(f"{label} could not complete before the selected stage ended")
        if recovery_deadline_ns is not None and time.time_ns() >= recovery_deadline_ns:
            if last_error is not None:
                raise last_error
            raise MintError(f"{label} did not finish before the mint-data window")
        attempt += 1
        try:
            remaining_seconds: float | None = None
            if phase_ends_at is not None:
                remaining_seconds = phase_ends_at - time.time_ns() / NANOSECONDS_PER_SECOND
            if recovery_deadline_ns is not None:
                recovery_seconds = (recovery_deadline_ns - time.time_ns()) / NANOSECONDS_PER_SECOND
                remaining_seconds = (
                    recovery_seconds
                    if remaining_seconds is None
                    else min(remaining_seconds, recovery_seconds)
                )
            if attempt_timeout_seconds is not None:
                ladder_timeout = attempt_timeout_seconds[
                    min(attempt - 1, len(attempt_timeout_seconds) - 1)
                ]
                remaining_seconds = (
                    ladder_timeout
                    if remaining_seconds is None
                    else min(remaining_seconds, ladder_timeout)
                )
            async with asyncio.timeout(remaining_seconds):
                return await operation()
        except (ChainError, TimeoutError) as error:
            last_error = error
            if recovery_deadline_ns is None and attempt >= attempt_limit:
                raise
            if recovery_deadline_ns is not None and time.time_ns() >= recovery_deadline_ns:
                raise
            detail = str(error) or (
                "request timed out" if isinstance(error, TimeoutError) else "RPC request failed"
            )
            logging.warn(f"{label} attempt {attempt} failed; retrying: {detail}")
            sleep_seconds = retry_interval_ms / 1_000
            if recovery_deadline_ns is not None:
                sleep_seconds = min(
                    sleep_seconds,
                    max(
                        0,
                        (recovery_deadline_ns - time.time_ns()) / NANOSECONDS_PER_SECOND,
                    ),
                )
            await asyncio.sleep(sleep_seconds)


def classify_private_action_retry(
    error: ProtocolError,
    stage_started_when_requested: bool,
) -> PrivateActionRetryDisposition:
    """Retry transient errors and allow selected rejections before the local stage start."""
    if error.kind in {ErrorKind.TRANSPORT, ErrorKind.RATE_LIMITED}:
        return PrivateActionRetryDisposition.TRANSIENT
    if (
        error.kind is ErrorKind.HTTP
        and error.status is not None
        and is_transient_http_status(error.status)
    ):
        return PrivateActionRetryDisposition.TRANSIENT
    if error.kind in {
        ErrorKind.AUTHENTICATION,
        ErrorKind.AUTHENTICATION_REQUIRED,
        ErrorKind.AUTHENTICATION_SESSION_MISMATCH,
        ErrorKind.SESSION_REQUIRED,
    }:
        return PrivateActionRetryDisposition.REAUTHENTICATE
    if error.kind is ErrorKind.MINT_STAGE_NOT_OPEN:
        return PrivateActionRetryDisposition.PREOPEN
    if not stage_started_when_requested and error.kind in {
        ErrorKind.MINT_INSUFFICIENT_FUNDS,
        ErrorKind.MINT_WALLET_INELIGIBLE,
        ErrorKind.MINT_LIMIT_EXCEEDED,
        ErrorKind.MINT_ACTION_REJECTED,
    }:
        return PrivateActionRetryDisposition.PREOPEN
    return PrivateActionRetryDisposition.TERMINAL
