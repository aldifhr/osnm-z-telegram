"""Conversation state for one in-flight mint.
"""

from __future__ import annotations

import logging as pylogging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from osnm_z.config import LoadedConfig


@dataclass
class Session:
    """One in-flight mint conversation. Single wallet, single phase by design."""

    locator: str
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    loaded: LoadedConfig | None = None
    chain: Any = None
    gateway: Any = None
    client: Any = None
    metadata: Any = None
    eligibility: Any = None
    options: list[Any] = field(default_factory=list)
    selected_index: int | None = None
    quantity: int | None = None
    nonce: str | None = None
    nonce_born: float = 0.0
    sign_in_failed: bool = False
    balance_wei: int | None = None
    gas_estimate_wei: int | None = None
    total_minted: int | None = None
    max_supply: int | None = None

    @property
    def gas_limit(self) -> int:
        return int(self.loaded.app.gas_limit) if self.loaded else 0

    async def close(self) -> None:
        try:
            await self.stack.aclose()
        except Exception:  # noqa: BLE001 - teardown must never mask the real error
            pylogging.exception("session teardown failed")
        from .locks import CHAT_LOCK

        if CHAT_LOCK.locked():
            CHAT_LOCK.release()

