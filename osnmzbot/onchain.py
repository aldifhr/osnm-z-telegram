"""Direct eth_call and receipt reads against the chain.

totalSupply() is authoritative for sold-out detection because the OpenSea
availability flag is a cache that can lag a sold-out drop.

simulate_call() exists because the upstream contract_call() sends eth_call
without a `from`, which runs it as the zero address. SeaDrop's allowlist and
per-wallet quota live on msg.sender, so a zero-address simulation would report
"not allowlisted" for a wallet that is in fact eligible, and would happily pass
for a public stage regardless of quota. This module sends its own request with
the wallet attached.
"""

from __future__ import annotations

import logging as pylogging
from dataclasses import dataclass
from typing import Any

from osnm_z.errors import MintError
from osnm_z.rpc_protocol import is_transaction_hash

TOTAL_SUPPLY_SELECTOR = bytes.fromhex("18160ddd")


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Outcome of an eth_call run against pending state."""

    ok: bool
    gas_used: int | None = None
    revert_data: bytes | None = None
    error: str | None = None

    @property
    def revert_selector(self) -> str | None:
        """The 4-byte error selector, when the call reverted with one.

        SeaDrop uses custom errors, so the selector is the only reliable signal
        for "why"; the human-readable name needs an ABI the bot does not carry.
        """
        if self.revert_data and len(self.revert_data) >= 4:
            return "0x" + self.revert_data[:4].hex()
        return None


async def read_total_supply(gateway: Any, chain: Any, address: str) -> int | None:
    """Live totalSupply() of the NFT contract, or None if the read fails."""
    try:
        result = await gateway.contract_reads(
            chain, [(address, TOTAL_SUPPLY_SELECTOR)]
        )
    except Exception:  # noqa: BLE001
        pylogging.warning("totalSupply read failed", exc_info=True)
        return None
    if not result:
        return None
    value = result[0]
    if isinstance(value, Exception):
        return None
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            return None
    try:
        return int.from_bytes(value, "big")
    except (TypeError, ValueError):
        return None


async def read_receipt(gateway: Any, chain: Any, transaction_hash: str) -> Any:
    """The receipt for a hash, or None when it is not mined yet.

    Returns None both for "pending" and for "the node has never heard of this
    hash". A dropped or unknown hash is reported by /status as still-pending
    with the age attached, because a bot cannot prove absence.
    """
    if not is_transaction_hash(transaction_hash):
        raise MintError("bukan transaction hash yang valid")
    try:
        receipts = await gateway.transaction_receipts(chain, (transaction_hash,))
    except Exception:  # noqa: BLE001
        pylogging.warning("receipt read failed", exc_info=True)
        return None
    if not receipts:
        return None
    return receipts[0]


async def read_block_number(gateway: Any, chain: Any) -> int | None:
    """Latest block height, for confirmation counts. None when unreadable."""
    try:
        raw = await gateway._request(  # noqa: SLF001 - the gateway has no public block read
            chain.rpc_url, "eth_blockNumber", []
        )
    except Exception:  # noqa: BLE001
        pylogging.warning("block number read failed", exc_info=True)
        return None
    if isinstance(raw, str):
        try:
            return int(raw, 16)
        except ValueError:
            return None
    return None


async def simulate_call(
    gateway: Any,
    chain: Any,
    sender: str,
    target: str,
    calldata: bytes,
    value: int = 0,
) -> SimulationResult:
    """Run the mint as eth_call from `sender` against pending state.

    This never signs or broadcasts. It answers "would this mint succeed" using
    the same state the transaction would execute against, including per-wallet
    quota and allowlist checks that a from-less call would miss.

    Returns a SimulationResult rather than raising: a revert is an answer, not
    an error, and the caller wants to show it rather than traceback.
    """
    call: dict[str, object] = {
        "to": target,
        "input": f"0x{calldata.hex()}",
    }
    if sender:
        call["from"] = sender
    if value:
        call["value"] = hex(value)

    try:
        await gateway._request(  # noqa: SLF001 - no public call accepts a sender
            chain.rpc_url, "eth_call", [call, "pending"]
        )
    except Exception as error:  # noqa: BLE001
        text = str(error)
        revert = _extract_revert_data(text)
        return SimulationResult(ok=False, revert_data=revert, error=text)
    return SimulationResult(ok=True)


def _extract_revert_data(message: str) -> bytes | None:
    """Pull return data out of a JSON-RPC error message, if present.

    Nodes render it inconsistently -- some as {"data": "0x..."}, some as a bare
    0x-prefixed blob in the message -- so both shapes are accepted. A node that
    gives nothing means the revert carries no data, which is not an error.
    """
    marker = "0x"
    index = message.find(marker)
    if index < 0:
        return None
    candidate = message[index + 2 :]
    digits = []
    for char in candidate:
        if char in "0123456789abcdefABCDEF":
            digits.append(char)
        else:
            break
    if len(digits) < 8:
        return None
    try:
        return bytes.fromhex("".join(digits[: (len(digits) // 2) * 2]))
    except ValueError:
        return None
