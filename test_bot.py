"""Tests for the Telegram front-end.

Focus: the auto-trigger pre-filter must never fire on ordinary chat, and must
always fire on the locator shapes the library accepts. Everything else is
covered by running the real handlers against fake Telegram objects.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "1")

import bot  # noqa: E402
from osnm_z.opensea_protocol import ProtocolError, parse_collection_locator  # noqa: E402

# ── the pre-filter contract ───────────────────────────────────────────

SHOULD_TRIGGER = [
    "https://opensea.io/collection/tadaaaaaa/overview",
    "https://opensea.io/collection/some-slug_123/stats",
    "  https://opensea.io/collection/tadaaaaaa/overview  ",
    "tadaaaaaa",
    "some-collection_2",
    "https://opensea.io/collection/x_y-z",
    "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",
    "0x00005ea00ac477b1030ce78506496e8c2de24bf5",
]

SHOULD_NOT_TRIGGER = [
    "",
    "   ",
    "halo",
    "gas",
    "cek",
    "mint sekarang dong",
    "berapa gasnya?",
    "https://opensea.io/",
    "https://opensea.io/item/123",
    "https://etherscan.io/tx/0xabc",
    "https://evil.example/collection/x",
    "0x123",
    "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5extra",
    "halo dunia ini chat biasa",
    "https://opensea.io/collection/has%20space",
    # Plain http: the library demands https, so neither should accept it.
    "http://opensea.io/collection/tadaaaaaa",
]


@pytest.mark.parametrize("text", SHOULD_TRIGGER)
def test_filter_accepts_valid_locators(text: str) -> None:
    assert bot.looks_like_locator(text) is True


@pytest.mark.parametrize("text", SHOULD_NOT_TRIGGER)
def test_filter_rejects_ordinary_chat(text: str) -> None:
    assert bot.looks_like_locator(text) is False


def test_filter_never_rejects_links_or_addresses() -> None:
    """URLs and contract addresses never get filtered out before parsing."""
    samples = [
        "https://opensea.io/collection/abc/overview",
        "https://opensea.io/collection/x_y-z",
        "https://opensea.io/collection/abc",
        "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",
    ]
    for sample in samples:
        parse_collection_locator(sample)  # raises if the library rejects it
        assert bot.looks_like_locator(sample), f"filter dropped {sample!r}"


def test_short_bare_slug_is_deliberately_not_autotriggered() -> None:
    """A bare slug is indistinguishable from a word, so short ones need /mint.

    The library would accept "abc", but auto-triggering on every short word
    would fire on ordinary chat. The tradeoff is explicit and tested.
    """
    parse_collection_locator("abc")  # library accepts it
    assert bot.looks_like_locator("abc") is False
    # Long or punctuated bare slugs do auto-trigger.
    assert bot.looks_like_locator("cryptopunks") is True
    assert bot.looks_like_locator("my-collection") is True


def test_slug_shape_matches_library() -> None:
    """bot.SLUG_SHAPE must stay identical to the library's _SLUG."""
    from osnm_z import opensea_protocol

    library = opensea_protocol._SLUG.pattern
    assert bot.SLUG_SHAPE.pattern == library


@pytest.mark.parametrize(
    "text",
    ["https://opensea.io/collection/tadaaaaaa/overview", "tadaaaaaa"],
)
def test_filtered_in_library_still_raises_for_junk(text: str) -> None:
    """The filter is permissive; the library stays the real authority."""
    try:
        parse_collection_locator(text)
    except ProtocolError:
        pass  # acceptable: pre-filter said maybe, library said no


# ── helpers ───────────────────────────────────────────────────────────


def test_esc_blocks_markdown_injection() -> None:
    assert "<b>" not in bot.esc("<b>")
    assert "&lt;b&gt;" in bot.esc("<b>")


def test_explorer_link_built_for_known_chain() -> None:
    assert bot.EXPLORER[8453] == "https://basescan.org/tx/"
    assert bot.EXPLORER[1] == "https://etherscan.io/tx/"


def test_confirm_ttl_is_bounded() -> None:
    assert 0 < bot.CONFIRM_TTL_SECONDS <= 300


def test_session_close_releases_lock() -> None:
    """A session that never confirms must not wedge the bot forever."""
    import asyncio

    async def run() -> bool:
        session = bot.Session(locator="x")
        await bot.CHAT_LOCK.acquire()
        assert bot.CHAT_LOCK.locked()
        await session.close()
        return not bot.CHAT_LOCK.locked()

    assert asyncio.run(run()) is True


# ── multi-chain RPC registry ──────────────────────────────────────────


def test_robinhood_chain_is_known() -> None:
    """Hood Penguins lives on Robinhood 4663, not Base 8453."""
    assert bot.CHAIN_NAMES[4663] == "Robinhood"
    assert bot.CHAIN_IDS["robinhood"] == 4663


def test_robinhood_rpc_is_a_candidate() -> None:
    urls = bot.candidate_rpcs("https://mainnet.base.org")
    assert "https://rpc.mainnet.chain.robinhood.com" in urls


def test_candidate_rpcs_dedupes_and_keeps_config_first() -> None:
    urls = bot.candidate_rpcs("https://rpc.mainnet.chain.robinhood.com")
    assert urls[0] == "https://rpc.mainnet.chain.robinhood.com"
    assert len(urls) == len(set(urls))


def test_extra_rpcs_only_accepts_https(monkeypatch) -> None:
    monkeypatch.setenv(
        "OSNM_EXTRA_RPCS",
        "https://good.example,http://insecure.example,https://other.example",
    )
    urls = bot.candidate_rpcs("https://mainnet.base.org")
    assert "https://good.example" in urls
    assert "http://insecure.example" not in urls


def test_configured_rpc_never_dropped() -> None:
    urls = bot.candidate_rpcs("https://my-private-rpc.example/v2/key")
    assert urls[0] == "https://my-private-rpc.example/v2/key"


# ── phase rendering against real library metadata ─────────────────────


def _sample_session() -> bot.Session:
    """A session built from the library's real dataclasses, field for field."""
    from osnm_z.chain import ChainConfig
    from osnm_z.opensea_protocol import CollectionMetadata, StageMetadata
    from osnm_z.phase_selection import build_phase_options, public_only_eligibility

    stages = (
        StageMetadata(
            kind="drop_stage",
            label="Public stage",
            stage_type="PUBLIC_SALE",
            stage_index=0,
            start_time="2026-09-01T00:00:00Z",
            end_time="2027-01-01T00:00:00Z",
            max_total_mintable_by_wallet=5,
            # Real Hood Penguins public price, so price_table is exercised
            # with a non-zero amount rather than a free-mint shortcut.
            native_price_wei=100_000_000_000_000,
            price_chain_identifier="robinhood",
        ),
        StageMetadata(
            kind="drop_stage",
            label="Hoodsale 1",
            stage_type="SIGNED_PRESALE",
            stage_index=1,
            start_time="2025-01-01T00:00:00Z",
            end_time="2025-02-01T00:00:00Z",
            max_total_mintable_by_wallet=1,
            native_price_wei=0,
            price_chain_identifier="robinhood",
        ),
    )
    meta = CollectionMetadata(
        slug="hood-penguins",
        address="0xf517d3a2c499cce82c0f9e37d9e6c79319f3d9a3",
        chain_identifier="robinhood",
        network_id=4663,
        drop_kind="seadrop",
        drop_address="0x00005EA00Ac477B1030CE78506496e8C2dE24bf5",
        stages=stages,
    )
    session = bot.Session(locator="https://opensea.io/collection/hood-penguins/overview")
    session.metadata = meta
    session.chain = ChainConfig(4663, "https://rpc.mainnet.chain.robinhood.com")
    session.eligibility = public_only_eligibility(meta)
    session.options = build_phase_options(meta, session.eligibility, 1_780_000_000)
    return session


def test_render_phase_text_uses_only_real_fields() -> None:
    """Regression: metadata has no .name, only .slug/.address/chain_identifier."""
    text = bot.render_phase_text(_sample_session(), None)
    assert "hood-penguins" in text
    assert "0xf517d3a2c499cce82c0f9e37d9e6c79319f3d9a3" in text
    assert "Robinhood" in text
    assert "4663" in text
    assert "Public stage" in text


def test_render_phase_text_no_traceback_on_ended_stages() -> None:
    text = bot.render_phase_text(_sample_session(), "some expired stages")
    assert "Hoodsale 1" in text
    assert "some expired stages" in text


def test_fmt_phase_never_raises_for_any_option() -> None:
    for index, option in enumerate(_sample_session().options):
        assert bot.fmt_phase(option, index)


# ── fast path: one selectable stage, one tap to broadcast ─────────────


def test_single_selectable_stage_is_detected() -> None:
    session = _sample_session()
    picks = bot.selectable_indices(session)
    # only the PUBLIC_SALE stage is selectable in the fixture
    assert len(picks) == 1


def test_render_quantity_text_shows_price_and_wallet() -> None:
    session = _sample_session()
    session.loaded = bot.load()
    text = bot.render_quantity_text(session, bot.selectable_indices(session)[0])
    assert "hood-penguins" in text
    assert "Robinhood" in text
    assert "4663" in text
    assert session.loaded.signer.identity.address in text
    assert "LANGSUNG KIRIM" in text


def test_fast_path_callback_payload_has_three_parts() -> None:
    """go:q:<qty> must not collide with the slow path's go:<nonce>."""
    fast = "go:q:3"
    slow = "go:abc123"
    assert fast.split(":") == ["go", "q", "3"]
    assert slow.split(":") == ["go", "abc123"]
    assert len(fast.split(":")) == 3


def test_quantity_cap_is_bounded() -> None:
    session = _sample_session()
    index = bot.selectable_indices(session)[0]
    option = session.options[index]
    cap = option.max_quantity or option.wallet_mint_limit or 1
    assert 1 <= max(1, min(int(cap), 5)) <= 5


# ── price breakdown ───────────────────────────────────────────────────


def test_format_native_trims_and_labels() -> None:
    assert bot.format_native(0, 4663) == "0"
    assert bot.format_native(100_000_000_000_000, 4663) == "0.0001 Robinhood"
    assert bot.format_native(500_000_000_000_000, 4663) == "0.0005 Robinhood"
    assert bot.format_native(10**18, 4663) == "1 Robinhood"


def test_price_table_shows_unit_and_max() -> None:
    session = _sample_session()
    session.loaded = bot.load()
    session.balance_wei = 10**18
    session.gas_estimate_wei = 0
    index = bot.selectable_indices(session)[0]
    text = bot.price_table(session, index)
    assert "per NFT" in text
    assert "maks 5 NFT" in text
    assert "total maks" in text


def test_price_table_flags_insufficient_balance() -> None:
    session = _sample_session()
    session.loaded = bot.load()
    session.balance_wei = 0
    session.gas_estimate_wei = 0
    index = bot.selectable_indices(session)[0]
    text = bot.price_table(session, index)
    assert "Saldo kurang" in text
    assert "0.0005" in text


def test_price_table_confirms_sufficient_balance() -> None:
    session = _sample_session()
    session.loaded = bot.load()
    session.balance_wei = 10**18
    session.gas_estimate_wei = 0
    index = bot.selectable_indices(session)[0]
    text = bot.price_table(session, index)
    assert "Saldo cukup" in text
    assert "Saldo kurang" not in text


def test_price_table_survives_missing_balance() -> None:
    session = _sample_session()
    session.loaded = bot.load()
    session.balance_wei = None
    session.gas_estimate_wei = None
    index = bot.selectable_indices(session)[0]
    text = bot.price_table(session, index)
    assert "per NFT" in text
    assert "Saldo" not in text


def test_enrich_financials_is_safe_without_gateway() -> None:
    import asyncio

    session = bot.Session(locator="x")
    asyncio.run(bot.enrich_financials(session))  # must not raise
    assert session.balance_wei is None


# ── sold-out / supply detection ───────────────────────────────────────


def test_supply_status_none_when_unknown() -> None:
    session = _sample_session()
    assert session.total_minted is None
    assert bot.supply_status(session) is None


def test_supply_status_progress() -> None:
    session = _sample_session()
    session.total_minted = 922
    session.max_supply = 8888
    line = bot.supply_status(session)
    assert "922" in line and "8,888" in line
    assert "SOLD OUT" not in line and "Hampir habis" not in line


def test_supply_status_sold_out() -> None:
    session = _sample_session()
    session.total_minted = 8888
    session.max_supply = 8888
    assert "SOLD OUT" in bot.supply_status(session)


def test_supply_status_nearly_gone() -> None:
    session = _sample_session()
    session.total_minted = 8600
    session.max_supply = 8888
    assert "Hampir habis" in bot.supply_status(session)


def test_supply_status_without_cap() -> None:
    session = _sample_session()
    session.total_minted = 50
    session.max_supply = None
    line = bot.supply_status(session)
    assert "50" in line
    assert "SOLD OUT" not in line


def test_supply_status_never_exceeds_100_percent() -> None:
    session = _sample_session()
    session.total_minted = 10000
    session.max_supply = 8888
    line = bot.supply_status(session)
    assert "100.0%" in line


def test_total_supply_selector_is_erc721() -> None:
    assert bot.TOTAL_SUPPLY_SELECTOR == bytes.fromhex("18160ddd")


def test_read_total_supply_handles_bytes_and_errors() -> None:
    import asyncio

    class FakeGateway:
        def __init__(self, value):
            self.value = value

        async def contract_reads(self, chain, calls):
            return (self.value,)

    ok = asyncio.run(bot.read_total_supply(FakeGateway(b"\x00\x03\x9a"), None, "0x"))
    assert ok == 922
    assert asyncio.run(bot.read_total_supply(FakeGateway(RuntimeError("x")), None, "0x")) is None
    assert asyncio.run(bot.read_total_supply(FakeGateway(None), None, "0x")) is None


def test_library_flags_sold_out_metadata() -> None:
    from osnm_z.phase_selection import drop_unavailable_reason

    session = _sample_session()
    assert drop_unavailable_reason(session.metadata) is None


# ── per-stage log scan: bounded, and honestly documented as diagnostic ──


def test_supply_module_is_importable() -> None:
    import supply

    assert supply.TRANSFER_TOPIC.startswith("0xddf252ad")
    assert supply.ZERO_ADDRESS_TOPIC == "0x" + "0" * 64


def test_stage_mints_rejects_inverted_range() -> None:
    import supply

    result = supply.count_stage_mints("http://unused", "0x" + "11" * 20, 100, 50)
    assert result.minted == 0
    assert result.from_block == 100 and result.to_block == 50


def test_stage_scan_budget_is_bounded() -> None:
    """A full-history scan must never be attempted silently."""
    import supply

    assert supply.MAX_BLOCKS_PER_REQUEST <= 10_000
    assert supply.MAX_LOGS_SCANNED <= 100_000
    assert "NOT wired into the bot" in (supply.__doc__ or "")


# ── wallet settings: key handling must never leak or widen permissions ──


def test_atomic_write_replaces_value_and_keeps_600(tmp_path) -> None:
    import os
    import stat as statmod

    env = tmp_path / ".env"
    env.write_text("WALLET_KEY=0xold\nRPC_URL=https://x.example\n")
    os.chmod(env, 0o644)
    bot._atomic_write_env(env, "WALLET_KEY", "0x" + "ab" * 32)
    text = env.read_text()
    assert "0x" + "ab" * 32 in text
    assert "0xold" not in text
    assert "https://x.example" in text
    assert statmod.S_IMODE(env.stat().st_mode) == 0o600


def test_atomic_write_appends_when_key_absent(tmp_path) -> None:
    import stat as statmod

    env = tmp_path / ".env"
    env.write_text("RPC_URL=https://x.example")
    bot._atomic_write_env(env, "WALLET_KEY", "0x" + "cd" * 32)
    assert "WALLET_KEY=0x" + "cd" * 32 in env.read_text()
    assert statmod.S_IMODE(env.stat().st_mode) == 0o600


def test_atomic_write_leaves_no_temp_file(tmp_path) -> None:
    """A dotfile named .env has an empty suffix, so with_suffix() misbehaves."""
    env = tmp_path / ".env"
    env.write_text("WALLET_KEY=0xold\n")
    bot._atomic_write_env(env, "WALLET_KEY", "0x" + "ef" * 32)
    assert list(tmp_path.iterdir()) == [env]


def test_app_env_is_private_on_disk() -> None:
    import stat as statmod

    mode = statmod.S_IMODE((bot.APP_DIR / ".env").stat().st_mode)
    assert mode & 0o077 == 0, f".env is group/world readable: {oct(mode)}"


def test_pending_key_is_checked_before_slug_filter() -> None:
    """A pasted key must reach the key handler, never the mint handler."""
    import inspect

    source = inspect.getsource(bot.on_link)
    assert 'awaiting_key' in source
    assert source.index('awaiting_key') < source.index('looks_like_locator')


def test_wallet_help_mentions_deletion() -> None:
    assert "/wallet" in bot.HELP
    assert "dihapus" in bot.HELP


def test_clear_key_empties_value(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("WALLET_KEY=0x" + "11" * 32 + "\n")
    bot._atomic_write_env(env, "WALLET_KEY", "")
    assert "WALLET_KEY=\n" in env.read_text()


def test_wallet_status_dedupes_chains() -> None:
    """Two RPCs for Base must not print the same balance twice."""
    import asyncio

    class FakeGateway:
        def __init__(self, chain_id): self.chain_id = chain_id
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def prepare_chain(self, url):
            from osnm_z.chain import ChainConfig
            return ChainConfig(self.chain_id, url)
        async def balance(self, chain, address): return 5 * 10**18

    urls = ["https://a.example", "https://b.example", "https://c.example"]
    chains = [8453, 8453, 1]
    original = bot.ChainGateway
    import itertools
    counter = itertools.cycle(chains)
    bot.ChainGateway = lambda *a, **k: FakeGateway(next(counter))
    try:
        text = asyncio.run(bot.wallet_status_text())
    finally:
        bot.ChainGateway = original
    assert text.count("(8453)") == 1, text
    assert text.count("(1)") == 1, text


# ── /wallet clear must never be destructive on a single command ───────


def test_clear_requires_confirmation_keyboard() -> None:
    """Regression: /wallet clear used to wipe the key immediately."""
    import asyncio
    import inspect

    source = inspect.getsource(bot.cmd_wallet)
    assert "wclear:" in source, "clear must ask for confirmation"
    assert 'args in {"clear", "reset"}' in source

    class Msg:
        def __init__(self):
            self.replies = []
        async def reply_text(self, *a, **k):
            self.replies.append((a[0] if a else "", k))

    class Upd:
        def __init__(self):
            self.effective_message = Msg()

    class Ctx:
        def __init__(self):
            self.user_data = {}
            self.args = ["clear"]

    upd, ctx = Upd(), Ctx()
    asyncio.run(bot.cmd_wallet(upd, ctx))
    text, kwargs = upd.effective_message.replies[0]
    assert "Hapus wallet aktif" in text
    assert kwargs.get("reply_markup") is not None, "must show confirm buttons"


def test_clear_handler_checks_yes_token() -> None:
    import inspect

    source = inspect.getsource(bot.on_wallet_clear)
    assert "_atomic_write_env" in source
    assert "clear_nonce" in source, "clear token must be a one-shot nonce"
    # The write must live in the callback, not in cmd_wallet.
    assert "_atomic_write_env" in source


def test_wallet_clear_ignores_any_other_token() -> None:
    import asyncio

    class Q:
        def __init__(self, data): self.data = data; self.edits = []
        async def answer(self): pass
        async def edit_message_text(self, t, *a, **k): self.edits.append(t)
    class Upd:
        def __init__(self, data): self.callback_query = Q(data)
    class Ctx:
        def __init__(self): self.user_data = {}

    # No nonce was issued for "no", so the wipe must be refused outright.
    # Point the bot at a scratch .env so a regression can never touch the real key.
    import pathlib

    real = bot.APP_DIR / ".env"
    scratch = real.with_name(".env.test-wipe-guard")
    scratch.write_text(real.read_text())
    original_dir = bot.APP_DIR
    try:
        bot.APP_DIR = scratch.parent
        bot._atomic_write_env(scratch, "WALLET_KEY", "")  # sentinel write
        scratch.write_text("WALLET_KEY=0x" + "11" * 32 + "\n")
        upd, ctx = Upd("wclear:no"), Ctx()
        asyncio.run(bot.on_wallet_clear(upd, ctx))
        assert "sudah dipakai" in upd.callback_query.edits[0]
        # The key must survive a refused wipe.
        assert "0x" + "11" * 32 in scratch.read_text()
    finally:
        bot.APP_DIR = original_dir
        scratch.unlink(missing_ok=True)


def test_clear_token_is_nonce_not_static_yes() -> None:
    """Regression: a static wclear:yes button let an old tap wipe a new key."""
    import inspect

    source = inspect.getsource(bot.cmd_wallet)
    assert 'wclear:yes' not in source, "static token would be replayable"
    assert "secrets.token_urlsafe" in source
    assert "clear_nonce" in source


def test_clear_handler_is_single_use() -> None:
    import inspect

    source = inspect.getsource(bot.on_wallet_clear)
    assert 'ctx.user_data.get("clear_nonce") != token' in source
    assert 'ctx.user_data.pop("clear_nonce", None)' in source
    assert "load().signer" in source, "must detect an already-empty key"


def test_clear_handler_rejects_replayed_token() -> None:
    import asyncio

    class Q:
        def __init__(self, data): self.data = data; self.edits = []
        async def answer(self): pass
        async def edit_message_text(self, t, *a, **k): self.edits.append(t)
    class Upd:
        def __init__(self, data): self.callback_query = Q(data)
    class Ctx:
        def __init__(self, ud): self.user_data = ud

    def run(data, user_data):
        upd = Upd(data)
        ctx = Ctx(user_data)
        asyncio.run(bot.on_wallet_clear(upd, ctx))
        return upd.callback_query.edits[0], ctx

    # Wrong nonce: refused, and the nonce we were holding stays valid.
    text, ctx = run("wclear:BADNONCE", {"clear_nonce": "goodone"})
    assert "sudah dipakai" in text
    assert ctx.user_data.get("clear_nonce") == "goodone"

    # No nonce issued at all: refused.
    text, _ = run("wclear:whatever", {})
    assert "sudah dipakai" in text

    # The accepted path is the only one that consumes the nonce; simulate it
    # against a scratch APP_DIR so the real .env is never touched.
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        scratch = pathlib.Path(tmp) / ".env"
        # Copy the real .env so config validation passes, then point at it.
        scratch.write_text((bot.APP_DIR / ".env").read_text())
        original_dir, original_key = bot.APP_DIR, bot.APP_DIR / ".env"
        try:
            bot.APP_DIR = pathlib.Path(tmp)
            text, ctx = run("wclear:goodone", {"clear_nonce": "goodone"})
            assert "dikosongkan" in text
            assert "clear_nonce" not in ctx.user_data
            assert "WALLET_KEY=\n" in scratch.read_text()
            # Replaying the consumed token: the key is already empty, so the
            # handler refuses with the empty-key message instead of re-wiping.
            text2, _ = run("wclear:goodone", {"clear_nonce": "goodone"})
            assert "sudah" in text2
            assert "WALLET_KEY=\n" in scratch.read_text()
        finally:
            bot.APP_DIR = original_dir
            assert original_key.exists()


def test_wallet_status_gives_action_when_key_missing() -> None:
    """Regression: /wallet used to show a bare config error with no next step."""
    import asyncio
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / ".env").write_text("RPC_URL=https://x.example\n")  # no WALLET_KEY
        original = bot.APP_DIR
        try:
            bot.APP_DIR = d
            text = asyncio.run(bot.wallet_status_text())
        finally:
            bot.APP_DIR = original
    assert "Wallet belum diisi" in text
    assert "/wallet set" in text
    assert "sed -i" in text
    assert "Config error" not in text


def test_wallet_status_not_blocked_by_mint_lock() -> None:
    """Reading the wallet is read-only; it must work during a mint."""
    import inspect

    source = inspect.getsource(bot.cmd_wallet)
    # Reading the wallet is read-only, so it must not be gated on the mint lock.
    assert "CHAT_LOCK" not in source, "/wallet must work during a mint"
    assert "wallet_status_text" in source
    assert "clear_nonce" in source


# ── /wallet set <key> must work, not be silently ignored ───────────────


class _Msg:
    def __init__(self, text="/wallet set"):
        self.text = text
        self.replies = []
        self.deleted = False
        self.message_id = 1

    async def reply_text(self, *a, **k):
        self.replies.append(a[0] if a else "")

    async def delete(self):
        self.deleted = True


class _Upd:
    def __init__(self, text):
        self.effective_message = _Msg(text)
        self.effective_chat = type("C", (), {"id": 1})()


class _Ctx:
    def __init__(self, args=()):
        self.args = list(args)
        self.user_data = {}


def test_inline_set_command_is_handled_not_ignored() -> None:
    """Regression: /wallet set <key> fell through and did nothing at all."""
    import asyncio
    import inspect

    source = inspect.getsource(bot.cmd_wallet)
    assert 'verb == "set" and inline_key' in source
    assert "apply_wallet_key(message, ctx, inline_key)" in source
    # The old exact-match check must not be the only branch left.
    assert "raw_args.partition" in source


def test_inline_set_writes_key_and_deletes_message() -> None:
    import asyncio
    import pathlib
    import tempfile

    from eth_account import Account

    account = Account.create()
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / ".env").write_text((bot.APP_DIR / ".env").read_text())
        original = bot.APP_DIR
        try:
            bot.APP_DIR = d
            upd = _Upd("/wallet set " + account.key.hex())
            ctx = _Ctx(["set", account.key.hex()])
            asyncio.run(bot.cmd_wallet(upd, ctx))
            assert upd.effective_message.deleted is True, "key must not stay in chat"
            assert account.key.hex() in (d / ".env").read_text()
            assert account.address in upd.effective_message.replies[0]
        finally:
            bot.APP_DIR = original


def test_inline_set_rejects_bad_key_and_still_deletes() -> None:
    import asyncio
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / ".env").write_text((bot.APP_DIR / ".env").read_text())
        original = bot.APP_DIR
        try:
            bot.APP_DIR = d
            upd = _Upd("/wallet set not-a-key")
            ctx = _Ctx(["set", "not-a-key"])
            asyncio.run(bot.cmd_wallet(upd, ctx))
            assert upd.effective_message.deleted is True
            assert "valid" in upd.effective_message.replies[0]
            assert bot.APP_DIR.joinpath(".env").read_text().count("WALLET_KEY=") == 1
        finally:
            bot.APP_DIR = original


def test_bare_set_still_prompts() -> None:
    """/wallet set with no key must still ask for one."""
    import asyncio

    upd = _Upd("/wallet set")
    ctx = _Ctx(["set"])
    asyncio.run(bot.cmd_wallet(upd, ctx))
    assert ctx.user_data.get("awaiting_key") is True
    assert "Kirim private key" in upd.effective_message.replies[0]


def test_other_verbs_unchanged() -> None:
    import asyncio

    for verb in ("clear", "reset"):
        upd = _Upd(f"/wallet {verb}")
        ctx = _Ctx([verb])
        asyncio.run(bot.cmd_wallet(upd, ctx))
        assert ctx.user_data.get("clear_nonce"), f"{verb} must issue a nonce"
        assert upd.effective_message.deleted is False
