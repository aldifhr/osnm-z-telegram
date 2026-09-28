"""Per-stage mint counting from ERC721 Transfer logs — DIAGNOSTIC ONLY.

NOT wired into the bot, on purpose. Measured on Robinhood chain (2026-09-29):
the collection's 5 stages span 0..75,065,345, which at a 2,000-block request
window is ~37,500 eth_getLogs calls per stage. A full scan timed out at 260s and
returned nothing. A per-stage mint count is therefore not obtainable here within
any sane request budget on a public RPC.

It is kept because it is correct and fast for a NARROW range, which is what a
live stage actually needs: a stage that opened minutes ago spans a few thousand
blocks, so count_stage_mints() on (now - 30_000, now) returns in about a second.
Use it to watch an open stage, not to reconstruct history.

An ERC721 mint is a Transfer whose `from` is the zero address. Everything is
best-effort and bounded: a node that cannot serve logs yields None, and the
caller omits the line rather than guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import orjson

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS_TOPIC = "0x" + "0" * 64
ZERO_ADDRESS = "0x" + "0" * 40

# Providers commonly reject very wide ranges; keep each request small.
MAX_BLOCKS_PER_REQUEST = 2_000
# Refuse to scan more than this many logs, however wide the stage is.
MAX_LOGS_SCANNED = 20_000
RPC_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True, slots=True)
class StageMints:
    minted: int
    from_block: int
    to_block: int
    truncated: bool = False


def _rpc(url: str, method: str, params: list[object]) -> object:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = httpx.post(
        url,
        content=orjson.dumps(payload),
        headers={"content-type": "application/json"},
        timeout=RPC_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        raise RuntimeError("rate limited")
    response.raise_for_status()
    envelope = orjson.loads(response.content)
    if envelope.get("error"):
        raise RuntimeError(str(envelope["error"]))
    return envelope.get("result")


def _hex_to_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16)
    raise ValueError(f"unexpected block value: {value!r}")


def block_number(rpc_url: str) -> int | None:
    try:
        return _hex_to_int(_rpc(rpc_url, "eth_blockNumber", []))
    except Exception:  # noqa: BLE001 - caller treats None as unknown
        logging.warning("eth_blockNumber failed", exc_info=True)
        return None


def block_at_timestamp(rpc_url: str, timestamp: int) -> int | None:
    """Binary-search the first block mined at or after `timestamp` (UTC seconds)."""
    try:
        latest = block_number(rpc_url)
        if latest is None:
            return None
        low, high = 0, latest
        # Confirm the latest block really is past the target, else it is moot.
        if _rpc_timestamp(rpc_url, latest) < timestamp:
            return None
        if _rpc_timestamp(rpc_url, 0) >= timestamp:
            return 0
        while low < high:
            middle = (low + high) // 2
            if _rpc_timestamp(rpc_url, middle) < timestamp:
                low = middle + 1
            else:
                high = middle
        return low
    except Exception:  # noqa: BLE001
        logging.warning("block_at_timestamp failed", exc_info=True)
        return None


def _rpc_timestamp(rpc_url: str, block: int) -> int:
    data = _rpc(rpc_url, "eth_getBlockByNumber", [hex(block), False])
    if not isinstance(data, dict):
        raise ValueError("eth_getBlockByNumber returned no block")
    return _hex_to_int(data["timestamp"])


def count_stage_mints(
    rpc_url: str,
    contract: str,
    start_block: int,
    end_block: int,
) -> StageMints | None:
    """Count mints (Transfer from the zero address) in an inclusive block range."""
    if start_block > end_block:
        return StageMints(0, start_block, end_block)
    total = 0
    truncated = False
    scanned = 0
    block = start_block
    try:
        while block <= end_block:
            upper = min(block + MAX_BLOCKS_PER_REQUEST - 1, end_block)
            logs = _rpc(
                rpc_url,
                "eth_getLogs",
                [
                    {
                        "address": contract,
                        "fromBlock": hex(block),
                        "toBlock": hex(upper),
                        "topics": [TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC],
                    }
                ],
            )
            if not isinstance(logs, list):
                return None
            total += len(logs)
            scanned += len(logs)
            if scanned > MAX_LOGS_SCANNED:
                truncated = True
                break
            block = upper + 1
            if truncated:
                break
    except Exception:  # noqa: BLE001 - a node that refuses logs is not fatal
        logging.warning("eth_getLogs failed", exc_info=True)
        return None
    return StageMints(total, start_block, end_block, truncated)
