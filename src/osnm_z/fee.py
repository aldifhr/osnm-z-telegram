"""Shared initial EIP-1559 fee policy."""

from __future__ import annotations

from .config import FeesConfig
from .domain import Eip1559Fees, InitialFeePolicy
from .rpc_protocol import FeeEstimate

IMMEDIATE_AUTOMATIC_FEE_MULTIPLIER_BPS = 12_500
SCHEDULED_AUTOMATIC_FEE_MULTIPLIER_BPS = 25_000


def initial_transaction_fees(
    config: FeesConfig,
    estimate: FeeEstimate,
    *,
    scheduled: bool = False,
) -> Eip1559Fees:
    """Use manual fees or apply the active/scheduled automatic fee multiplier."""
    if config.automatic:
        multiplier = (
            SCHEDULED_AUTOMATIC_FEE_MULTIPLIER_BPS
            if scheduled
            else IMMEDIATE_AUTOMATIC_FEE_MULTIPLIER_BPS
        )
        return InitialFeePolicy(multiplier).apply(
            Eip1559Fees(estimate.max_fee_per_gas, estimate.max_priority_fee_per_gas)
        )
    if config.max_fee_per_gas is None or config.max_priority_fee_per_gas is None:
        raise ValueError("manual fee configuration is incomplete")
    return Eip1559Fees(config.max_fee_per_gas, config.max_priority_fee_per_gas)


def configured_fee_estimate(config: FeesConfig) -> FeeEstimate | None:
    """Return manual fees so RPC snapshots can omit fee-oracle calls entirely."""
    if config.automatic:
        return None
    if config.max_fee_per_gas is None or config.max_priority_fee_per_gas is None:
        raise ValueError("manual fee configuration is incomplete")
    return FeeEstimate(config.max_priority_fee_per_gas, config.max_fee_per_gas)
