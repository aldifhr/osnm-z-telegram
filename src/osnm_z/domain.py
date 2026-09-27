"""Shared Ethereum protocol constants, fee rules, and phase timing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

BASIS_POINTS = 10_000
UINT256_MAX = (1 << 256) - 1
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class FeeError(ValueError):
    """A fee policy or calculation is invalid."""


@dataclass(frozen=True, slots=True)
class Eip1559Fees:
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True, slots=True)
class InitialFeePolicy:
    multiplier_bps: int

    def __post_init__(self) -> None:
        if self.multiplier_bps < BASIS_POINTS:
            raise FeeError("the initial fee multiplier must not reduce fees")

    def apply(self, estimate: Eip1559Fees) -> Eip1559Fees:
        return Eip1559Fees(
            max_fee_per_gas=_multiply_ceil(estimate.max_fee_per_gas, self.multiplier_bps),
            max_priority_fee_per_gas=_multiply_ceil(
                estimate.max_priority_fee_per_gas, self.multiplier_bps
            ),
        )


class ExecutionKind(Enum):
    IMMEDIATE = "immediate"
    SCHEDULED_AT = "scheduled_at"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class PhaseWindow:
    starts_at: int
    ends_at: int | None

    def __post_init__(self) -> None:
        if self.starts_at < 0 or (self.ends_at is not None and self.ends_at <= self.starts_at):
            raise ValueError("phase end must be later than its start")

    def execution_kind(self, current_timestamp: int) -> ExecutionKind:
        if current_timestamp < self.starts_at:
            return ExecutionKind.SCHEDULED_AT
        if self.ends_at is not None and current_timestamp >= self.ends_at:
            return ExecutionKind.ENDED
        return ExecutionKind.IMMEDIATE


def _multiply_ceil(value: int, basis_points: int) -> int:
    if not 0 <= value <= UINT256_MAX:
        raise FeeError("fee calculation overflowed")
    numerator = value * basis_points + BASIS_POINTS - 1
    if numerator > UINT256_MAX:
        raise FeeError("fee calculation overflowed")
    return numerator // BASIS_POINTS


@dataclass(frozen=True, slots=True)
class FundingRequirement:
    mint_value: int
    gas_limit: int
    max_fee_per_gas: int

    def maximum_native_cost(self) -> int:
        values = (self.mint_value, self.gas_limit, self.max_fee_per_gas)
        if any(value < 0 for value in values):
            raise FeeError("native-currency funding arithmetic overflowed")
        result = self.gas_limit * self.max_fee_per_gas + self.mint_value
        if result > UINT256_MAX:
            raise FeeError("native-currency funding arithmetic overflowed")
        return result

    def shortfall(self, balance: int) -> int:
        if balance < 0 or balance > UINT256_MAX:
            raise FeeError("native-currency funding arithmetic overflowed")
        return max(0, self.maximum_native_cost() - balance)
