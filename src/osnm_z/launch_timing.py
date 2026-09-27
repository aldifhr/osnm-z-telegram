"""Chain-neutral system-UTC launch windows and waits."""

from __future__ import annotations

from dataclasses import dataclass

from . import logging
from .chain import (
    ENDPOINT_PREPARATION_TOTAL_TIMEOUT_SECONDS,
    SINGLE_RPC_WARMUP_TIMEOUT_SECONDS,
    SUBMISSION_RPC_REWARM_TIMEOUT_SECONDS,
    ChainGateway,
)
from .config import ChainConfig
from .errors import MintError
from .public_mint import PublicMintBroadcastPlan
from .rpc_protocol import ChainError
from .scheduling import (
    NANOSECONDS_PER_MILLISECOND,
    NANOSECONDS_PER_SECOND,
    LaunchClock,
    SystemUtcClock,
)

PRIVATE_PREPARATION_LEAD_SECONDS = 60
PRIVATE_REAUTHENTICATION_LEAD_SECONDS = 20
PRIVATE_CONNECTION_PREPARATION_LEAD_SECONDS = 10
PRIVATE_CONNECTION_PREPARATION_TIMEOUT_SECONDS = 3.0
PRIVATE_CALLDATA_LEAD_MS = 2_000
NONCE_REFRESH_LEAD_SECONDS = 10
NONCE_CUTOFF_LEAD_SECONDS = 2
NONCE_RECOVERY_WINDOW_SECONDS = NONCE_REFRESH_LEAD_SECONDS - NONCE_CUTOFF_LEAD_SECONDS
FUNDING_CHECK_LEAD_SECONDS = 45
FUNDING_RECOVERY_BUDGET_SECONDS = int(sum(SINGLE_RPC_WARMUP_TIMEOUT_SECONDS))


@dataclass(frozen=True, slots=True)
class NonceRefreshWindow:
    refresh_utc_ns: int
    cutoff_utc_ns: int


def is_future_phase(
    starts_at_seconds: int,
    system_clock: SystemUtcClock,
) -> bool:
    return starts_at_seconds * NANOSECONDS_PER_SECOND > system_clock.now_ns()


def deadline_before(target_ns: int, lead_ns: int) -> int:
    if target_ns < 0 or target_ns > (1 << 64) - 1 or lead_ns < 0:
        raise MintError("selected stage has an invalid or incompatible time window")
    return max(0, target_ns - lead_ns)


def preparation_deadline(
    launch_utc_ns: int,
) -> int:
    """Keep private preparation retries out of the T-2 calldata window."""
    return deadline_before(
        launch_utc_ns,
        NONCE_CUTOFF_LEAD_SECONDS * NANOSECONDS_PER_SECOND,
    )


def funding_check_deadline(launch_utc_ns: int, broadcast_utc_ns: int) -> int:
    """Reserve the full recovery budget before the funding decision."""
    return min(
        deadline_before(
            launch_utc_ns,
            FUNDING_CHECK_LEAD_SECONDS * NANOSECONDS_PER_SECOND,
        ),
        deadline_before(
            broadcast_utc_ns,
            FUNDING_RECOVERY_BUDGET_SECONDS * NANOSECONDS_PER_SECOND,
        ),
    )


def nonce_refresh_window(launch_utc_ns: int, broadcast_utc_ns: int) -> NonceRefreshWindow:
    """Finish nonce recovery before T-2 or an earlier public broadcast."""
    cutoff_utc_ns = min(
        deadline_before(
            launch_utc_ns,
            NONCE_CUTOFF_LEAD_SECONDS * NANOSECONDS_PER_SECOND,
        ),
        broadcast_utc_ns,
    )
    refresh_utc_ns = min(
        deadline_before(
            launch_utc_ns,
            NONCE_REFRESH_LEAD_SECONDS * NANOSECONDS_PER_SECOND,
        ),
        deadline_before(
            cutoff_utc_ns,
            NONCE_RECOVERY_WINDOW_SECONDS * NANOSECONDS_PER_SECOND,
        ),
    )
    return NonceRefreshWindow(refresh_utc_ns, cutoff_utc_ns)


async def warm_submission_endpoint(
    gateway: ChainGateway, chain: ChainConfig, *, deadline_ns: int | None = None
) -> None:
    timeout_seconds = ENDPOINT_PREPARATION_TOTAL_TIMEOUT_SECONDS
    if deadline_ns is not None:
        timeout_seconds = min(
            timeout_seconds,
            max(0, (deadline_ns - SystemUtcClock().now_ns()) / NANOSECONDS_PER_SECOND),
        )
    await gateway.warm_submission_endpoint(chain, timeout_seconds=timeout_seconds)


async def rewarm_submission_endpoint(
    gateway: ChainGateway, chain: ChainConfig, *, deadline_ns: int | None = None
) -> None:
    """Check the RPC connection within the remaining preparation window."""
    timeout_seconds = SUBMISSION_RPC_REWARM_TIMEOUT_SECONDS
    if deadline_ns is not None:
        timeout_seconds = min(
            timeout_seconds,
            max(0, (deadline_ns - SystemUtcClock().now_ns()) / NANOSECONDS_PER_SECOND),
        )
    if timeout_seconds <= 0:
        return
    try:
        await gateway.rewarm_submission_endpoint(chain, timeout_seconds=timeout_seconds)
    except ChainError as error:
        logging.warn(f"Final RPC check failed; submission will still use this endpoint: {error}")


async def wait_for_public_broadcast(
    public_plan: PublicMintBroadcastPlan | None,
    launch_clock: LaunchClock | None,
) -> LaunchClock | None:
    """Wait for the configured public broadcast time."""
    if public_plan is None:
        return launch_clock
    active_clock = launch_clock or LaunchClock(public_plan.starts_at_ns)
    await active_clock.wait_until_lead_ns(public_plan.offset_ms * NANOSECONDS_PER_MILLISECOND)
    return active_clock


def ensure_mint_not_expired(launch_clock: LaunchClock | None, phase_ends_at: int | None) -> None:
    """Stop setup, calldata requests, or submission once the selected phase ends."""
    if (
        phase_ends_at is not None
        and (launch_clock or SystemUtcClock()).now_ns() >= phase_ends_at * NANOSECONDS_PER_SECOND
    ):
        raise MintError("selected stage has ended")
