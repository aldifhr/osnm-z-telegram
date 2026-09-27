"""Capture wallet state and enforce the mint funding requirement."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from . import logging
from .chain import SINGLE_RPC_WARMUP_TIMEOUT_SECONDS, ChainGateway
from .config import AppConfig, ChainConfig, FeesConfig
from .domain import FundingRequirement
from .errors import MintError
from .fee import configured_fee_estimate, initial_transaction_fees
from .rpc_protocol import ChainError, FeeEstimate, WalletSnapshot
from .scheduling import NANOSECONDS_PER_SECOND

WALLET_RPC_RETRY_BASE_DELAY_SECONDS = 0.1
WALLET_RPC_RETRY_MAX_DELAY_SECONDS = 0.5


def ensure_single_wallet_funding(
    config: AppConfig,
    gas_limit: int,
    mint_value: int,
    estimate: FeeEstimate,
    balance: int,
    *,
    scheduled: bool = False,
) -> None:
    fees = initial_transaction_fees(config.fees, estimate, scheduled=scheduled)
    requirement = FundingRequirement(mint_value, gas_limit, fees.max_fee_per_gas)
    shortfall = requirement.shortfall(balance)
    if shortfall:
        raise MintError(
            f"wallet is underfunded by {shortfall} wei for mint value and maximum gas",
        )


async def retry_wallet_balance(
    gateway: ChainGateway, chain: ChainConfig, wallet: str, deadline_ns: int | None = None
) -> int:
    """Refresh the funding balance within its recovery window."""
    return await _retry_wallet_read(
        "Wallet balance check",
        wallet,
        lambda timeout: gateway.balance(chain, wallet, timeout_seconds=timeout),
        deadline_ns,
    )


async def retry_wallet_launch_snapshot(
    gateway: ChainGateway,
    chain: ChainConfig,
    wallet: str,
    fees: FeesConfig,
    *,
    deadline_ns: int | None = None,
) -> WalletSnapshot:
    """Refresh balance, nonce, and fees before the submission cutoff."""
    return await _retry_wallet_read(
        "Final wallet data refresh",
        wallet,
        lambda timeout: gateway.wallet_snapshot(
            chain, wallet, configured_fee_estimate(fees), timeout
        ),
        deadline_ns,
    )


async def _retry_wallet_read[T](
    label: str,
    wallet: str,
    operation: Callable[[float], Awaitable[T]],
    deadline_ns: int | None,
) -> T:
    last_error: ChainError | None = None
    attempt = 0
    while deadline_ns is not None or attempt < len(SINGLE_RPC_WARMUP_TIMEOUT_SECONDS):
        remaining = (
            SINGLE_RPC_WARMUP_TIMEOUT_SECONDS[-1]
            if deadline_ns is None
            else (deadline_ns - time.time_ns()) / NANOSECONDS_PER_SECOND
        )
        if remaining <= 0:
            break
        timeout = min(
            SINGLE_RPC_WARMUP_TIMEOUT_SECONDS[
                min(attempt, len(SINGLE_RPC_WARMUP_TIMEOUT_SECONDS) - 1)
            ],
            remaining,
        )
        attempt += 1
        try:
            async with asyncio.timeout(timeout):
                return await operation(timeout)
        except TimeoutError:
            last_error = ChainError(f"{label} request timed out")
        except ChainError as error:
            last_error = error
        can_retry = (
            time.time_ns() < deadline_ns
            if deadline_ns is not None
            else attempt < len(SINGLE_RPC_WARMUP_TIMEOUT_SECONDS)
        )
        if can_retry:
            logging.warn(f"{label} attempt {attempt} failed: {last_error}")
            retry_delay = min(
                WALLET_RPC_RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1),
                WALLET_RPC_RETRY_MAX_DELAY_SECONDS,
            )
            if deadline_ns is not None:
                retry_delay = min(
                    retry_delay,
                    max(0.0, (deadline_ns - time.time_ns()) / NANOSECONDS_PER_SECOND),
                )
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)
    raise last_error or ChainError(f"{label} recovery window elapsed for {wallet}")
