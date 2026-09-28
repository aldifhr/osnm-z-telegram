"""Direct eth_call reads against the NFT contract.

totalSupply() is authoritative for sold-out detection because the OpenSea
availability flag is a cache that can lag a sold-out drop.
"""

from __future__ import annotations

import logging as pylogging
from typing import Any

TOTAL_SUPPLY_SELECTOR = bytes.fromhex("18160ddd")


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

