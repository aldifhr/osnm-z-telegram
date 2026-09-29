"""Dry-run a mint: build the calldata, simulate it, never sign it.

The point is to answer "would this mint actually work for my wallet" before
spending gas on a transaction that is going to revert. Two failures it catches
that the OpenSea availability flag cannot:

  - per-wallet quota exhausted, even though the stage still looks open
  - an allowlist (GTD) that does not contain this wallet

Why the simulation sends its own eth_call: upstream contract_call() omits `from`,
so it runs as the zero address. SeaDrop reads msg.sender for both the allowlist
and the per-wallet quota, so a from-less call would report an eligible wallet as
ineligible and an exhausted wallet as fine.

Nothing here signs or broadcasts. The signer is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from osnm_z.opensea import WalletOpenSeaClient
from osnm_z.public_mint import (
    build_public_mint_action,
    resolve_public_mint_context_and_stats,
)

from .onchain import SimulationResult, simulate_call


@dataclass(frozen=True, slots=True)
class SimulationReport:
    """Everything the dry-run learned, formatted independently of Telegram."""

    ok: bool
    stage_name: str
    quantity: int
    target: str | None
    value_wei: int
    calldata: bytes | None
    balance_wei: int | None
    fee_estimate_wei: int | None
    result: SimulationResult | None
    error: str | None = None

    @property
    def affordable(self) -> bool | None:
        """None when the balance could not be read, so nothing is claimed."""
        if self.balance_wei is None or self.fee_estimate_wei is None:
            return None
        return self.balance_wei >= self.value_wei + self.fee_estimate_wei

    @property
    def missing_wei(self) -> int:
        if self.balance_wei is None or self.fee_estimate_wei is None:
            return 0
        need = self.value_wei + self.fee_estimate_wei
        return max(0, need - self.balance_wei)


async def simulate_mint(
    *,
    client: WalletOpenSeaClient,
    gateway: Any,
    chain: Any,
    config: Any,
    metadata: Any,
    eligibility: Any,
    stage: Any,
    phase: Any,
    quantity: int,
    wallet: str,
    public_target: str | None,
) -> SimulationReport:
    """Build and simulate the exact mint that would be broadcast.

    Read-only: it asks OpenSea for the action and calls eth_call. It does not
    sign, does not broadcast, and does not touch the nonce.
    """
    stage_name = getattr(stage, "name", "stage")
    try:
        if stage.stage_type == "PUBLIC_SALE":
            if not public_target:
                return _failed(stage_name, quantity, "SeaDrop address tidak ditemukan")
            public_context, stats = await resolve_public_mint_context_and_stats(
                gateway, chain, metadata, wallet
            )
            action = build_public_mint_action(
                metadata, stage, public_context, stats, wallet, quantity
            )
        else:
            # The private/allowlisted action only exists on the OpenSea side, so
            # it has to be fetched; that is the only way to simulate a GTD mint.
            # _mint_action is private but is the only entry point, and it needs
            # the SIWE session the session already established.
            action = await client._mint_action(metadata, wallet, quantity)  # noqa: SLF001

        balance = await gateway.balance(chain, wallet)
        estimate = _gas_cost_estimate(config, quantity)

        simulation = await simulate_call(
            gateway, chain, wallet, action.target, action.calldata, action.value
        )
        return SimulationReport(
            ok=simulation.ok,
            stage_name=stage_name,
            quantity=quantity,
            target=action.target,
            value_wei=int(action.value),
            calldata=action.calldata,
            balance_wei=int(balance),
            fee_estimate_wei=estimate,
            result=simulation,
        )
    except Exception as error:  # noqa: BLE001 - report, never traceback at the user
        return _failed(stage_name, quantity, f"{type(error).__name__}: {error}")


def _gas_cost_estimate(config: Any, quantity: int) -> int | None:
    """Worst-case gas cost in wei, or None when the fees are automatic.

    Manual fees are known up front, so the number is exact and the affordability
    check is trustworthy. With automatic fees the price is only knowable at
    signing time, so no estimate is claimed rather than a guess.
    """
    from osnm_z.fee import configured_fee_estimate

    try:
        fees = configured_fee_estimate(config.fees)
    except ValueError:
        return None
    if fees is None:
        return None
    return int(config.gas_limit) * int(fees.max_fee_per_gas)


def _failed(stage_name: str, quantity: int, message: str) -> SimulationReport:
    return SimulationReport(
        ok=False,
        stage_name=stage_name,
        quantity=quantity,
        target=None,
        value_wei=0,
        calldata=None,
        balance_wei=None,
        fee_estimate_wei=None,
        result=None,
        error=message,
    )
