"""Telegram front-end for the osnm-z mint engine.

This module is a facade. The implementation lives in the osnmzbot package next
to it, one concern per module:

    osnmzbot.config      app paths, chain registry, config loading
    osnmzbot.models      the in-flight mint Session
    osnmzbot.locator     is this chat message a collection reference?
    osnmzbot.onchain     direct eth_call reads (totalSupply)
    osnmzbot.render      message text and price formatting
    osnmzbot.wallet      reading the wallet, persisting a private key
    osnmzbot.flows       session lifecycle
    osnmzbot.handlers    Telegram command and callback handlers
    osnmzbot.app         entry point

Everything is re-exported here so the entry point, the tests, and any existing
callers keep working. New code should import from the specific module.

The minting engine itself -- SIWE sign-in, eligibility, calldata, signing,
broadcast, receipt -- is the upstream osnm_z library, imported and driven
directly. No terminal output is ever parsed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from osnmzbot import app  # noqa: E402
from osnmzbot.app import _configure_logging, _log_dir, main  # noqa: E402
from osnmzbot.text import HELP  # noqa: E402
from osnmzbot.locks import CHAT_LOCK  # noqa: E402
from osnmzbot.config import (  # noqa: E402
    APP_DIR,
    CHAIN_IDS,
    CHAIN_NAMES,
    CONFIRM_TTL_SECONDS,
    DEFAULT_RPCS,
    EXPLORER,
    OWNER_ID,
    app_dir,
    candidate_rpcs,
    extra_rpcs,
    load,
    probe_chain_id,
    set_app_dir,
)
from osnmzbot.flows import (  # noqa: E402
    begin_session,
    drop_session,
    enrich_financials,
    render_phases,
    selectable_indices,
    show_quantity_buttons,
)
from osnmzbot.handlers import (  # noqa: E402
    apply_wallet_key,
    cmd_cancel,
    cmd_doctor,
    cmd_help,
    cmd_mint,
    cmd_start,
    cmd_wallet,
    error_handler,
    guard,
    on_cancel,
    on_go,
    on_link,
    on_no,
    on_phase,
    on_quantity,
    on_wallet_clear,
    on_wallet_key,
    on_wallet_set_button,
)
from osnmzbot.locator import (  # noqa: E402
    ADDRESS_SHAPE,
    BARE_SLUG_MIN_LENGTH,
    SLUG_SHAPE,
    looks_like_locator,
)
from osnmzbot.models import Session  # noqa: E402
from osnmzbot.onchain import TOTAL_SUPPLY_SELECTOR, read_total_supply  # noqa: E402
from osnmzbot.render import (  # noqa: E402
    esc,
    fmt_phase,
    format_native,
    price_table,
    render_phase_text,
    render_quantity_text,
    supply_status,
)
from osnmzbot.wallet import _atomic_write_env, wallet_status_text  # noqa: E402


__all__ = [
    "APP_DIR", "ADDRESS_SHAPE", "app", "app_dir", "BARE_SLUG_MIN_LENGTH", "CHAIN_IDS",
    "CHAIN_NAMES", "CHAT_LOCK", "CONFIRM_TTL_SECONDS", "DEFAULT_RPCS",
    "EXPLORER", "HELP", "OWNER_ID", "SLUG_SHAPE", "Session",
    "TOTAL_SUPPLY_SELECTOR", "_atomic_write_env", "_configure_logging",
    "_log_dir", "apply_wallet_key", "begin_session", "candidate_rpcs",
    "cmd_cancel", "cmd_doctor", "cmd_help", "cmd_mint", "cmd_start",
    "cmd_wallet", "drop_session", "enrich_financials", "error_handler",
    "esc", "extra_rpcs", "fmt_phase", "format_native", "guard", "load",
    "looks_like_locator", "main", "on_cancel", "on_go", "on_link", "on_no",
    "on_phase", "on_quantity", "on_wallet_clear", "on_wallet_key",
    "on_wallet_set_button", "price_table", "probe_chain_id", "read_total_supply",
    "render_phase_text", "render_phases", "render_quantity_text",
    "selectable_indices", "set_app_dir", "show_quantity_buttons",
    "supply_status",
    "wallet_status_text",
]

if __name__ == "__main__":
    main()
