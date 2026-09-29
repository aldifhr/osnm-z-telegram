"""Tracking a broadcast transaction after the session is gone.

on_go closes the session the moment the transaction is sent, so the question
"did it land" outlives the mint flow. This module answers it from a TrackedMint
plus a live receipt read, and is independent of Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from osnm_z.chain import ChainGateway
from osnm_z.config import ChainConfig

from .models import TrackedMint
from .onchain import read_block_number, read_receipt


@dataclass(frozen=True, slots=True)
class TxStatus:
    """What is known about a tracked transaction right now."""

    state: str  # succeeded | reverted | pending | dropped | unknown
    confirmations: int | None = None
    block_number: int | None = None
    detail: str | None = None


# Past this age a pending tx is reported as dropped rather than pending. A
# dropped transaction is not the same as a failed one: the nonce was never
# consumed, so the funds were never spent on it and a retry is still valid.
PENDING_AGE_LIMIT_SECONDS = 300

# Explorer links, for a hash the bot cannot otherwise make clickable.
EXPLORERS: dict[int, str] = {
    1: "https://etherscan.io/tx/",
    10: "https://optimistic.etherscan.io/tx/",
    137: "https://polygonscan.com/tx/",
    8453: "https://basescan.org/tx/",
    42161: "https://arbiscan.io/tx/",
    4663: "https://explorer.testnet.chain.robinhood.com/tx/",
}

_ICON = {
    "succeeded": "✅",
    "reverted": "❌",
    "pending": "⏳",
    "dropped": "🫥",
    "unknown": "❔",
}

_HEADLINE = {
    "succeeded": "Sukses",
    "reverted": "Revert",
    "pending": "Masih pending",
    "dropped": "Kemungkinan drop",
    "unknown": "Tidak diketahui",
}


async def track_status(gateway: Any, tracked: TrackedMint) -> TxStatus:
    """Read the receipt and turn it into a disposition.

    Distinguishes four things the bot must not conflate:
      succeeded - mined with status 1
      reverted  - mined with status 0
      pending   - not mined yet, still young
      dropped   - not mined, and old enough that the node has probably
                  forgotten it
    """
    chain = ChainConfig(tracked.chain_id, tracked.rpc_url)
    receipt = await read_receipt(gateway, chain, tracked.transaction_hash)

    if isinstance(receipt, Exception):
        return TxStatus(
            state="unknown",
            detail=f"RPC error saat baca receipt: {type(receipt).__name__}",
        )

    if receipt is None:
        if tracked.age_seconds() > PENDING_AGE_LIMIT_SECONDS:
            return TxStatus(
                state="dropped",
                detail=(
                    f"Tidak ada receipt setelah {int(tracked.age_seconds())}s. "
                    "Kemungkinan tx-nya dibuang dari mempool, bukan revert."
                ),
            )
        return TxStatus(
            state="pending",
            detail=f"Menunggu konfirmasi ({int(tracked.age_seconds())}s)",
        )

    block_number = int(getattr(receipt, "block_number", 0) or 0)
    head = await read_block_number(gateway, chain)
    confirmations = (head - block_number + 1) if head is not None else None

    if bool(getattr(receipt, "is_success", False)):
        return TxStatus(
            state="succeeded", confirmations=confirmations, block_number=block_number
        )
    return TxStatus(
        state="reverted",
        confirmations=confirmations,
        block_number=block_number,
        detail="Tx sudah mined dengan status 0 — gas tetap terpakai.",
    )


def describe(tracked: TrackedMint, status: TxStatus) -> str:
    """Render a status for display. No Telegram types, so it is reusable."""
    lines = [
        f"{_ICON.get(status.state, '❔')} **{_HEADLINE.get(status.state, status.state)}**",
        f"Tx `{tracked.transaction_hash}`",
    ]
    if status.state in ("succeeded", "reverted") and status.block_number:
        lines.append(f"Block: `{status.block_number}`")
    if status.confirmations is not None:
        lines.append(f"Confirmations: `{status.confirmations}`")
    if not tracked.simulated:
        lines.append(
            f"Stage: {tracked.stage_name} · qty: {tracked.quantity} · "
            f"chain: `{tracked.chain_id}`"
        )
    if status.detail:
        lines.append(status.detail)
    explorer = EXPLORERS.get(tracked.chain_id)
    if explorer:
        lines.append(f"[Buka di explorer]({explorer}{tracked.transaction_hash})")
    return "\n".join(lines)


def build_gateway(loaded: Any) -> ChainGateway:
    """A gateway with the same timeouts the mint flow uses.

    Not a context manager here: it is entered and closed by the caller so the
    gateway's lifetime matches the status read, not the module.
    """
    timeout = float(getattr(loaded.app, "rpc_request_timeout_ms", 10_000)) / 1000
    return ChainGateway(timeout, max_connections=4)
