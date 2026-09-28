"""App paths, chain registry, and config loading.

Free of Telegram imports, so it is reusable from a CLI or another front-end.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import os
from pathlib import Path
from typing import Any

from osnm_z.config import LoadedConfig


# Three levels up: osnmzbot/config.py -> osnmzbot -> bot -> the app root.
# Getting this wrong points the mint library at bot/.env, which it rejects
# for holding TELEGRAM_* keys, so the bot fails to start with a config error
# that looks unrelated to the path.
APP_DIR = Path(__file__).resolve().parents[2]

_APP_DIR_OVERRIDE: Path | None = None


def app_dir() -> Path:
    """The app root, resolved at call time.

    A module-level constant is frozen at import time, so a test that points the
    bot at a scratch directory has to re-point this. Handlers call app_dir()
    rather than importing APP_DIR, which keeps redirection working without
    monkeypatching every module that reads the env files.
    """
    return _APP_DIR_OVERRIDE or APP_DIR


def set_app_dir(path: Path | None) -> Path:
    """Point the bot at a different app root. Pass None to restore."""
    global _APP_DIR_OVERRIDE
    _APP_DIR_OVERRIDE = Path(path).resolve() if path is not None else None
    return app_dir()


CONFIRM_TTL_SECONDS = 90


OWNER_ID = int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "0") or 0)


EXPLORER = {
    1: "https://etherscan.io/tx/",
    8453: "https://basescan.org/tx/",
    10: "https://optimistic.etherscan.io/tx/",
    42161: "https://arbiscan.io/tx/",
    137: "https://polygonscan.com/tx/",
    11155111: "https://sepolia.etherscan.io/tx/",
    84532: "https://sepolia.basescan.org/tx/",
}


DEFAULT_RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",  # 4663
    "https://mainnet.base.org",  # 8453
    "https://ethereum-rpc.publicnode.com",  # 1
    "https://mainnet.optimism.io",  # 10
    "https://arb1.arbitrum.io/rpc",  # 42161
    "https://polygon-rpc.com",  # 137
]


CHAIN_NAMES = {
    1: "Ethereum",
    10: "Optimism",
    137: "Polygon",
    8453: "Base",
    42161: "Arbitrum",
    4663: "Robinhood",
    11155111: "Sepolia",
    84532: "Base Sepolia",
}


CHAIN_IDS = {
    "ethereum": 1,
    "mainnet": 1,
    "eth": 1,
    "optimism": 10,
    "op": 10,
    "polygon": 137,
    "matic": 137,
    "base": 8453,
    "arbitrum": 42161,
    "arb": 42161,
    "robinhood": 4663,
    "robinhoodchain": 4663,
    "sepolia": 11155111,
}


def extra_rpcs() -> list[str]:
    """RPC_URLs added through OSNM_EXTRA_RPCS (comma separated)."""
    raw = os.environ.get("OSNM_EXTRA_RPCS", "")
    return [part.strip() for part in raw.split(",") if part.strip().startswith("https://")]


def candidate_rpcs(config_rpc_url: str) -> list[str]:
    """Configured RPC first, then extras, then the built-in public defaults."""
    seen: list[str] = []
    for url in [config_rpc_url, *extra_rpcs(), *DEFAULT_RPCS]:
        if url and url not in seen:
            seen.append(url)
    return seen


async def probe_chain_id(gateway: Any, url: str) -> int | None:
    try:
        chain = await gateway.prepare_chain(url)
    except Exception:  # noqa: BLE001 - a dead endpoint is simply not a candidate
        return None
    return int(chain.chain_id)


def load() -> LoadedConfig:
    """Load the mint config from the app .env, never the bot .env.

    LoadedConfig.load() searches from sys.argv[0]'s directory when the
    executable sits under the cwd, which for this bot is bot/ — it would find
    bot/.env (Telegram secrets) and reject every key in it. Pin the path.
    """
    root = app_dir()
    os.chdir(root)
    return LoadedConfig.load_from(root / ".env")

