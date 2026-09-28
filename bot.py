"""Telegram front-end for the osnm-z mint engine.

Replaces osnm_z.command's three interactive prompts (collection locator, phase,
quantity) with Telegram inline keyboards. Everything else — SIWE sign-in,
eligibility, calldata, signing, broadcast, receipt — is the upstream library,
imported and driven directly, so no terminal output is ever parsed.

The AsyncExitStack that owns the RPC gateway and OpenSea session lives on the
Session for as long as the conversation is open, because execute_mint_phase needs
both still alive. It is closed exactly once, on cancel, on confirm, or on error.

No transaction is broadcast without an explicit inline-keyboard confirmation
whose callback data carries a short-lived nonce.
"""

from __future__ import annotations

import asyncio
import html
import logging as pylogging
import os
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from osnm_z.chain import ChainGateway  # noqa: E402
from osnm_z.command import run_diagnostics  # noqa: E402
from osnm_z.config import ConfigError, LoadedConfig  # noqa: E402
from osnm_z.errors import MintError  # noqa: E402
from osnm_z.execution import MintExecutionContext, execute_mint_phase  # noqa: E402
from osnm_z.launch_timing import ensure_mint_not_expired, is_future_phase  # noqa: E402
from osnm_z.opensea import WalletOpenSeaClient  # noqa: E402
from osnm_z.opensea_protocol import (  # noqa: E402
    ErrorKind,
    ProtocolError,
    parse_collection_locator,
)
from osnm_z.outcomes import ExecutionState  # noqa: E402
from osnm_z.phase_selection import (  # noqa: E402
    build_phase_options,
    public_only_eligibility,
    stage_window,
    validate_mint_limits,
    validate_snapshot_shape,
    validate_stage_windows,
    warn_exhausted_stages,
)
from osnm_z.public_mint import discover_seadrop_address  # noqa: E402
from osnm_z.scheduling import SystemUtcClock  # noqa: E402

pylogging.getLogger("httpx").setLevel(pylogging.WARNING)
pylogging.getLogger("telegram").setLevel(pylogging.WARNING)
pylogging.getLogger("apscheduler").setLevel(pylogging.WARNING)

APP_DIR = Path(__file__).resolve().parent.parent
CONFIRM_TTL_SECONDS = 90
CHAT_LOCK = asyncio.Lock()

HELP = (
    "*osnm-z bot*\n\n"
    "/mint — mulai sesi mint, kirim link/slug/contract OpenSea\n"
    "/doctor — cek wallet, RPC, dan koneksi OpenSea\n"
    "/wallet — address aktif + saldo per chain\n"
    "/wallet set <key> — ganti private key (pesan lo dihapus)\n"
    "/wallet clear — kosongkan key (perlu konfirmasi)\n"
    "/cancel — batalkan sesi\n\n"
    "Bot nampilin daftar phase, lo pilih pake tombol, "
    "qty, konfirmasi, baru tx dikirim."
)

EXPLORER = {
    1: "https://etherscan.io/tx/",
    8453: "https://basescan.org/tx/",
    10: "https://optimistic.etherscan.io/tx/",
    42161: "https://arbiscan.io/tx/",
    137: "https://polygonscan.com/tx/",
    11155111: "https://sepolia.etherscan.io/tx/",
    84532: "https://sepolia.basescan.org/tx/",
}

OWNER_ID = int(os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "0") or 0)

# Mirrors osnm_z.opensea_protocol._SLUG (anchored, same character class) so
# ordinary chat is not mistaken for a collection. bot/test_bot.py asserts the
# pattern stays byte-identical to the library's, so an upstream change fails
# the test instead of silently widening what triggers a mint session.
SLUG_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")

# A bare slug is indistinguishable from a normal word without hitting OpenSea,
# so require a shape real collections have: a digit, a separator, or enough
# length. "halo" and "gas" stay chat; "some-collection_2" is a collection.
# Short real slugs remain reachable via /mint.
BARE_SLUG_MIN_LENGTH = 5
ADDRESS_SHAPE = re.compile(r"0x[0-9a-fA-F]{40}\Z")


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


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
        if CHAT_LOCK.locked():
            CHAT_LOCK.release()


# osnm-z configures a single RPC_URL, but a collection can live on any chain
# (Hood Penguins is on Robinhood chain 4663, not Base). Probe every candidate
# endpoint and keep the ones that answer, so the session can be matched to the
# collection's chain instead of failing with a chain mismatch.
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
    os.chdir(APP_DIR)
    return LoadedConfig.load_from(APP_DIR / ".env")


def fmt_phase(option: Any, index: int) -> str:
    mark = "✅" if option.is_selectable else "⛔"
    kind = "Public" if option.stage_type == "PUBLIC_SALE" else "Allowlist"
    return (
        f"{mark} *{index}.* {esc(option.name)[:60]}\n"
        f"    `{kind}` · {esc(option.state)}\n"
        f"    {esc(option.eligibility)}\n"
        f"    max: {option.max_quantity if option.max_quantity is not None else '—'}"
    )


def drop_session(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pop and synchronously schedule teardown of any open session."""
    session: Session | None = ctx.user_data.pop("session", None)
    if session is not None:
        asyncio.get_event_loop().create_task(session.close())


# ── commands ──────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP, parse_mode=ParseMode.MARKDOWN
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP, parse_mode=ParseMode.MARKDOWN
    )


def _atomic_write_env(path: Path, key: str, value: str) -> None:
    """Replace one setting in an .env file atomically, keeping mode 0600.

    The file holds the private key, so a crash mid-write must never leave a
    truncated key behind, and the permissions must never widen.
    """
    path = path.resolve()
    original = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(original):
        updated = pattern.sub(f"{key}={value}", original)
    else:
        separator = "" if original.endswith("\n") or not original else "\n"
        updated = f"{original}{separator}{key}={value}\n"
    # path.with_suffix() is wrong for a dotfile named ".env" (suffix is empty),
    # so name the temp file explicitly.
    temp = path.with_name(f".{path.name}.tmp")
    # Create restricted from the start: never a window where the key is 0644.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


async def wallet_status_text() -> str:
    """Current wallet identity and balance per reachable chain. Never the key."""
    lines = ["*Wallet*"]
    try:
        loaded = load()
    except ConfigError as error:
        return (
            "❌ *Wallet belum diisi*\n\n"
            f"`{esc(error)}`\n\n"
            "Isi dengan `/wallet set` (pesan lo akan dihapus).\n"
            "Atau isi langsung di server:\n"
            f"`sed -i 's|^WALLET_KEY=.*|WALLET_KEY=0xKEY|' "
            f"{APP_DIR / '.env'}`"
        )
    address = loaded.signer.identity.address
    lines.append(f"`{address}`")
    lines.append(f"key: `{APP_DIR / '.env'}` (600, never displayed)")
    lines.append("")
    balances = []
    for url in candidate_rpcs(loaded.app.rpc_url):
        chain_id = None
        balance = None
        try:
            async with AsyncExitStack() as stack:
                gateway = await stack.enter_async_context(
                    ChainGateway(loaded.app.rpc_request_timeout_ms / 1000, 4)
                )
                chain = await gateway.prepare_chain(url)
                chain_id = int(chain.chain_id)
                balance = await gateway.balance(chain, address)
        except Exception:  # noqa: BLE001 - one dead RPC must not hide the rest
            continue
        balances.append((chain_id, int(balance), url))
    if not balances:
        lines.append("⚠️ Nggak ada RPC yang bisa dibaca.")
        return "\n".join(lines)
    # Several candidate RPCs can serve the same chain (RPC_URL plus a default),
    # so collapse by chain id rather than printing the same balance twice.
    by_chain: dict[int, int] = {}
    for chain_id, balance, _ in balances:
        by_chain.setdefault(chain_id, balance)
    for chain_id, balance in by_chain.items():
        name = CHAIN_NAMES.get(chain_id, f"chain {chain_id}")
        pretty = f"{balance / 10**18:.6f}".rstrip("0").rstrip(".") or "0"
        lines.append(f"• {name} ({chain_id}): `{pretty}`")
    return "\n".join(lines)


async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/wallet — show the active wallet and how to change it."""
    message = update.effective_message
    raw_args = " ".join(ctx.args).strip()
    verb, _, inline_key = raw_args.partition(" ")
    verb = verb.lower()
    args = verb
    if verb == "set" and inline_key:
        # /wallet set <key> — the key rides in on the command, so the command
        # message itself must be deleted exactly like a pasted key would be.
        # Without this the key is silently ignored and left in the chat.
        try:
            await message.delete()
        except (Forbidden, BadRequest):
            pass
        await apply_wallet_key(message, ctx, inline_key)
        return
    if args == "set":
        ctx.user_data["awaiting_key"] = True
        await message.reply_text(
            "Kirim private key (64 hex, `0x` opsional).\n\n"
            "⚠️ Key akan lewat server Telegram dan masuk chat history. "
            "Bot akan HAPUS pesan lo begitu diterima, tapi kalau lo mau "
            "zero-trace, edit `.env` langsung di server.\n\n"
            "_Ketik /cancel untuk batal._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if args in {"clear", "reset"}:
        # Destructive and unrecoverable: the key is gone once written, and there
        # is no backup. Never do this on a single command, and never with a
        # static callback token — an old button tapped twice would wipe the
        # replacement key. The nonce is single-use for exactly that reason.
        clear_nonce = secrets.token_urlsafe(6)
        ctx.user_data["clear_nonce"] = clear_nonce
        await message.reply_text(
            "⚠️ *Hapus wallet aktif?*\n\n"
            "`WALLET_KEY` akan dikosongkan dari `.env`. Bot nggak bisa mint "
            "sampai diisi lagi, dan key lama nggak bisa dipulihkan.\n\n"
            "Kalau cuma mau ganti, pakai `/wallet set`.\n\n"
            "_Ketik /cancel untuk batal._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Ya, kosongkan",
                            callback_data=f"wclear:{clear_nonce}",
                        ),
                        InlineKeyboardButton("❌ Batal", callback_data="cancel"),
                    ]
                ]
            ),
        )
        return
    note = await message.reply_text("⏳ Baca wallet…")
    try:
        text = await wallet_status_text()
    except Exception as error:  # noqa: BLE001
        await note.edit_text(f"❌ Gagal baca wallet: `{esc(error)}`")
        return
    await note.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔑 Ganti wallet", callback_data="wset")]]
        ),
    )


async def on_wallet_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a pasted key: delete the message, never echo or log it."""
    message = update.effective_message
    raw = (message.text or "").strip()
    # Remove the key from the chat immediately, before anything else.
    try:
        await message.delete()
    except (Forbidden, BadRequest):
        pass
    ctx.user_data.pop("awaiting_key", None)
    await apply_wallet_key(message, ctx, raw)


async def apply_wallet_key(message: Any, ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    """Validate, persist, and report one private key. Never echoes the key."""
    key = raw[2:] if raw.lower().startswith("0x") else raw
    if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
        await message.reply_text(
            "❌ Bukan private key yang valid. Harus 64 karakter hex. "
            "Pesan lo sudah dihapus dari chat.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        _atomic_write_env(APP_DIR / ".env", "WALLET_KEY", f"0x{key}")
    except Exception as error:  # noqa: BLE001
        await message.reply_text(
            f"❌ Gagal menulis .env: `{esc(error)}`", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        loaded = load()
        address = loaded.signer.identity.address
    except ConfigError as error:
        await message.reply_text(
            f"❌ Key tersimpan tapi config nggak bisa dibaca: `{esc(error)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    await message.reply_text(
        "✅ *Wallet diganti*\n"
        f"Address: `{address}`\n"
        f"Key: `{APP_DIR / '.env'}` (600, tidak pernah ditampilkan)\n\n"
        "💰 Cek saldo dengan `/wallet`. Key lo sudah dihapus dari chat ini.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_wallet_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirmed wipe of WALLET_KEY — single use, and never on an empty key.

    The callback token is a one-shot nonce, so an old confirmation button that
    is tapped again (or after a new key was set) does nothing. Wiping an
    already-empty key is reported, not re-reported as a new wipe.
    """
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1] if ":" in query.data else ""
    if not token or ctx.user_data.get("clear_nonce") != token:
        await query.edit_message_text(
            "Tombol ini sudah dipakai atau kedaluwarsa. "
            "Ketik `/wallet clear` lagi kalau mau.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ctx.user_data.pop("clear_nonce", None)
    drop_session(ctx)
    try:
        current = load().signer
    except ConfigError as error:
        await query.edit_message_text(
            f"Key sudah kosong. ({esc(error)})", parse_mode=ParseMode.MARKDOWN
        )
        return
    _atomic_write_env(APP_DIR / ".env", "WALLET_KEY", "")
    await query.edit_message_text(
        "🗑 `WALLET_KEY` dikosongkan. Bot nggak bisa mint sampai diisi lagi.\n\n"
        "Isi ulang dengan `/wallet set`.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_wallet_set_button(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    ctx.user_data["awaiting_key"] = True
    await query.edit_message_text(
        "Kirim private key (64 hex, `0x` opsional).\n\n"
        "⚠️ Key lewat server Telegram. Bot akan HAPUS pesan lo "
        "begitu diterima. Ketik /cancel untuk batal.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    drop_session(ctx)
    was_awaiting = ctx.user_data.pop("awaiting_key", None)
    await update.effective_message.reply_text(
        "Sesi dibatalkan." if not was_awaiting else "Batal. Wallet tidak diubah."
    )


async def cmd_doctor(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if CHAT_LOCK.locked():
        await message.reply_text("⏳ Ada mint yang jalan. Tunggu selesai dulu.")
        return
    async with CHAT_LOCK:
        try:
            loaded = load()
        except ConfigError as error:
            await message.reply_text(f"❌ Config: {esc(error)}")
            return
        note = await message.reply_text("⏳ Mengecek config, wallet, RPC…")
        try:
            await run_diagnostics(loaded.app, loaded.signer)
        except Exception as error:  # noqa: BLE001 - surface anything to the operator
            await note.edit_text(f"❌ Doctor gagal: `{esc(error)}`")
            return
        await note.edit_text(
            "✅ *Doctor OK*\n"
            f"Wallet: `{esc(loaded.signer.identity.address)}`\n"
            f"RPC: `{esc(loaded.app.rpc_url)}`\n"
            f"Gas limit: `{loaded.app.gas_limit}`\n\n"
            "Now broadcast tx: `/mint <link>`",
            parse_mode=ParseMode.MARKDOWN,
        )


def looks_like_locator(text: str) -> bool:
    """True when a free-text message is worth trying to parse as a collection.

    Cheap pre-filter so ordinary chat never triggers an OpenSea round-trip. The
    library's parse_collection_locator stays the real authority.
    """
    value = text.strip()
    if not value:
        return False
    if value.startswith(("http://", "https://")):
        # Only a real /collection/<slug> path, matching the library, which
        # rejects /, /item/<id>, and any other host.
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        if parsed.scheme != "https" or parsed.hostname != "opensea.io":
            return False
        segments = [s for s in parsed.path.split("/") if s]
        return len(segments) >= 2 and segments[0] == "collection" and bool(
            SLUG_SHAPE.fullmatch(segments[1])
        )
    if value.startswith("0x"):
        return len(value) == 42 and bool(ADDRESS_SHAPE.fullmatch(value))
    if not SLUG_SHAPE.fullmatch(value):
        return False
    if len(value) < BARE_SLUG_MIN_LENGTH:
        return False
    return any(ch.isdigit() or ch in "-_" for ch in value) or len(value) >= 8


async def cmd_mint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_session(update, ctx, " ".join(ctx.args).strip())


async def on_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Any message that parses as an OpenSea collection starts a session."""
    # A pending wallet key must never be mistaken for a collection slug.
    if ctx.user_data.get("awaiting_key"):
        await on_wallet_key(update, ctx)
        return
    text = (update.effective_message.text or "").strip()
    if not looks_like_locator(text):
        return
    await begin_session(update, ctx, text)


async def begin_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> None:
    message = update.effective_message
    if CHAT_LOCK.locked():
        await message.reply_text("⏳ Ada mint yang jalan. Tunggu selesai dulu.")
        return

    if not raw:
        drop_session(ctx)
        await message.reply_text(
            "Kirim link OpenSea / slug / contract address.\n\n"
            "Contoh: `https://opensea.io/collection/tadaaaaaa/overview`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        locator = parse_collection_locator(raw)
    except Exception as error:  # noqa: BLE001
        await message.reply_text(f"❌ Link tidak valid: `{esc(error)}`")
        return

    try:
        loaded = load()
    except ConfigError as error:
        await message.reply_text(f"❌ Config: `{esc(error)}`")
        return

    # Take the lock for the whole conversation, not just this handler.
    await CHAT_LOCK.acquire()
    session = Session(locator=raw, loaded=loaded)
    ctx.user_data["session"] = session
    note = await message.reply_text("⏳ Resolve collection + SIWE sign-in…")

    # Try each RPC until one whose chain the collection actually lives on.
    # A chain mismatch is a retry-with-another-endpoint signal, not a failure.
    tried: list[int] = []
    last_error: Exception | None = None
    try:
        session.gateway = await session.stack.enter_async_context(
            ChainGateway(
                loaded.app.rpc_request_timeout_ms / 1000, max_connections=4
            )
        )
        session.client = await session.stack.enter_async_context(
            WalletOpenSeaClient(loaded.app.opensea)
        )
        for url in candidate_rpcs(loaded.app.rpc_url):
            try:
                chain = await session.gateway.prepare_chain(url)
            except Exception:  # noqa: BLE001 - dead endpoint, try the next
                continue
            tried.append(int(chain.chain_id))
            try:
                session.chain = chain
                session.metadata = await session.client.resolve_collection(
                    locator, chain.chain_id
                )
                validate_stage_windows(session.metadata)
                break
            except ProtocolError as error:
                session.chain = None
                last_error = error
                if error.kind is not ErrorKind.COLLECTION_CHAIN_MISMATCH:
                    raise
                continue
        if session.metadata is None:
            names = ", ".join(
                f"{CHAIN_NAMES.get(c, c)}({c})" for c in dict.fromkeys(tried)
            )
            raise MintError(
                f"Collection ini nggak ada di chain yang gue punya RPC-nya. "
                f"Sudah coba: {names or 'tidak ada'}. "
                f"Tambah RPC-nya lewat OSNM_EXTRA_RPCS."
            ) from last_error
    except Exception as error:  # noqa: BLE001
        await session.close()
        await note.edit_text(f"❌ Gagal load collection:\n`{esc(error)}`")
        return

    try:
        await session.client.authenticate(
            loaded.signer,
            loaded.signer.identity.address,
            session.chain.chain_id,
            session.metadata.slug,
        )
        session.eligibility = await session.client.eligibility(
            session.metadata.slug, loaded.signer.identity.address
        )
        validate_snapshot_shape(session.metadata, session.eligibility)
    except (ProtocolError, MintError) as error:
        # Upstream falls back to public-only rather than failing the run.
        session.sign_in_failed = True
        session.eligibility = public_only_eligibility(session.metadata)
        await note.edit_text(
            "⚠️ Sign-in/eligibility gagal — hanya phase public yang bisa dipilih.\n"
            f"`{esc(error)}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    try:
        session.options = build_phase_options(
            session.metadata, session.eligibility, SystemUtcClock().now_seconds()
        )
    except Exception as error:  # noqa: BLE001
        await session.close()
        await note.edit_text(f"❌ Gagal membaca phase:\n`{esc(error)}`")
        return

    if not any(option.is_selectable for option in session.options):
        await session.close()
        await note.edit_text("❌ OpenSea nggak nunjukin phase yang bisa dipilih.")
        return

    # Balance + gas are cheap on-chain reads and make the price screen honest.
    await enrich_financials(session)

    picks = selectable_indices(session)
    if len(picks) == 1:
        # One mintable stage: skip the phase screen entirely.
        session.selected_index = picks[0]
        await note.edit_text("✅ Ketemu 1 stage yang bisa di-mint.")
        await show_quantity_buttons(
            update, ctx, note.message_id, picks[0], fast=True
        )
        return
    await note.edit_text("✅ Sesi siap. Lanjut pilih phase 👇")
    await render_phases(update, ctx)


def selectable_indices(session: Session) -> list[int]:
    return [i for i, o in enumerate(session.options) if o.is_selectable]


# ERC721.totalSupply() — live mint count, unlike OpenSea's cached availability.
# contract_reads() takes raw calldata bytes, not a hex string.
TOTAL_SUPPLY_SELECTOR = bytes.fromhex("18160ddd")
# Stage-level: SeaDrop's public drop has no per-stage supply reader that is
# safe to assume, so the collection total is the honest signal we can show.


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


def supply_status(session: Session) -> str | None:
    """One line describing live mint progress, or None when unknown."""
    minted = session.total_minted
    cap = session.max_supply
    if minted is None:
        return None
    if not cap:
        return f"\U0001f522 *Minted:* `{minted:,}`"
    pct = min(100.0, 100.0 * minted / cap)
    line = f"\U0001f522 *Supply:* `{minted:,}` / `{cap:,}` ({pct:.1f}%)"
    if pct >= 100.0:
        return f"\U0001f6ab *SOLD OUT* — {line}"
    if pct >= 90.0:
        return f"\U0001f525 *Hampir habis* — {line}"
    return line


async def enrich_financials(session: Session) -> None:
    """Read the wallet balance and a gas estimate for the price screen.

    Best effort: a failure here must not block the mint, it only means the
    screen shows price without the sufficiency line.
    """
    if session.gateway is None or session.chain is None or session.loaded is None:
        return
    address = session.loaded.signer.identity.address
    try:
        balance = await session.gateway.balance(session.chain, address)
        session.balance_wei = int(balance)
    except Exception:  # noqa: BLE001
        pylogging.warning("balance read failed", exc_info=True)
    try:
        inputs = await session.gateway.submission_inputs(
            session.chain, address, None
        )
        gas_limit = session.gas_limit or 250_000
        session.gas_estimate_wei = int(
            inputs.fee_estimate.max_fee_per_gas
        ) * gas_limit
    except Exception:  # noqa: BLE001
        pylogging.warning("gas estimate failed", exc_info=True)
    # Live mint progress: OpenSea's availability flag can be stale, totalSupply
    # is read straight from the contract.
    try:
        session.total_minted = await read_total_supply(
            session.gateway, session.chain, session.metadata.address
        )
    except Exception:  # noqa: BLE001
        pylogging.warning("supply read failed", exc_info=True)


def format_native(wei: int | None, chain_id: int, places: int = 6) -> str:
    """Format a native-token amount, trimming trailing zeros."""
    if not wei:
        return "0"
    text = f"{wei / 10**18:.{places}f}".rstrip("0").rstrip(".")
    return f"{text} {CHAIN_NAMES.get(chain_id, 'native')}"


def price_table(session: Session, index: int) -> str:
    """Per-NFT price, gas estimate, and balance sufficiency for one stage."""
    meta = session.metadata
    stage = meta.stages[index]
    chain_id = int(session.chain.chain_id)
    cap = session.options[index].max_quantity or 1
    cap = max(1, min(int(cap), 5))
    unit = int(stage.native_price_wei or 0)
    gas = session.gas_estimate_wei or 0

    lines = [
        f"💰 *Harga*",
        f"• per NFT: `{format_native(unit, chain_id)}`",
        f"• maks {cap} NFT: `{format_native(unit * cap, chain_id)}`",
    ]
    if gas:
        lines.append(f"• gas (≈{session.gas_limit:,}): `{format_native(gas, chain_id)}`")
    total = unit * cap + gas
    if cap > 1:
        lines.append(f"• *total maks*: `{format_native(total, chain_id)}`")

    if session.balance_wei is not None:
        balance = int(session.balance_wei)
        needed = unit * cap + gas
        if balance < needed:
            short = needed - balance
            lines.append(
                f"\n🔴 *Saldo kurang* — butuh `{format_native(short, chain_id)}` lagi"
            )
        else:
            lines.append(
                f"\n🟢 Saldo cukup — sisa `{format_native(balance - needed, chain_id)}`"
            )
    return "\n".join(lines)


def render_quantity_text(session: Session, index: int) -> str:
    """Fast-path screen: one stage is already chosen, only qty is left."""
    meta = session.metadata
    option = session.options[index]
    chain_id = int(session.chain.chain_id)
    cap = option.max_quantity or option.wallet_mint_limit or 1
    cap = max(1, min(int(cap), 5))
    stage = meta.stages[index]
    unit = int(stage.native_price_wei or 0)
    return (
        f"*{esc(meta.slug)}* · {esc(option.name)}\n"
        f"`{esc(meta.address)}`\n"
        f"{esc(CHAIN_NAMES.get(chain_id, 'unknown'))} ({chain_id})"
        f" · {esc(option.eligibility)}\n\n"
        f"{price_table(session, index)}\n"
        + (f"{supply_status(session)}\n" if supply_status(session) else "")
        + f"\n👛 `{esc(session.loaded.signer.identity.address)}`\n\n"
        "⚠️ _Tap jumlah = LANGSUNG KIRIM tx._"
    )


def render_phase_text(session: Session, warn: str | None) -> str:
    """Build the phase-list message. Pure, so tests can render real metadata."""
    warn = warn or warn_exhausted_stages(session.options)
    meta = session.metadata
    chain_id = int(session.chain.chain_id)
    body = [
        f"*{esc(meta.slug)}*",
        f"`{esc(meta.address)}`",
        f"Chain: {esc(CHAIN_NAMES.get(chain_id, 'unknown'))} ({chain_id})"
        f" · {esc(meta.chain_identifier)}",
        "",
    ]
    if meta.is_minted_out:
        body.append("\U0001f6ab Collection sudah minted out.")
    if meta.is_disabled:
        body.append("\U0001f6ab Collection dinonaktifkan OpenSea.")
    if warn:
        body.append(f"⚠️ {esc(warn)}")
    live = supply_status(session)
    if live:
        body.append(live)
    body.extend(fmt_phase(option, i) for i, option in enumerate(session.options))
    body.append("\n_Pilih phase:_")
    return "\n".join(body)


async def show_quantity_buttons(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    edit_id: int,
    index: int,
    fast: bool = False,
) -> None:
    session: Session | None = ctx.user_data.get("session")
    if session is None:
        return
    option = session.options[index]
    cap = option.max_quantity or option.wallet_mint_limit or 1
    cap = max(1, min(int(cap), 5))
    # fast=True means the qty tap broadcasts directly, so a nonce is pre-issued.
    if fast:
        session.nonce = secrets.token_urlsafe(8)
        session.nonce_born = time.monotonic()
    # fast: "go:q:<qty>" is self-contained and skips the confirm screen.
    # slow: "qt:<qty>" then "go:<nonce>" after an explicit confirm screen.
    def payload(n: int) -> str:
        return f"go:q:{n}" if fast else f"qt:{n}"

    rows = [
        [InlineKeyboardButton(f"{n} NFT" if n > 1 else "1 NFT", callback_data=payload(n))]
        for n in range(1, cap + 1)
    ]
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])
    text = (
        render_quantity_text(session, index)
        if fast
        else f"*{esc(option.name)}*\n{esc(option.eligibility)}\n\n"
             f"_Pilih jumlah (maks {cap}):_"
    )
    try:
        await ctx.bot.edit_message_text(
            text,
            chat_id=update.effective_chat.id,
            message_id=edit_id,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except BadRequest as error:
        if "message is not modified" not in str(error).lower():
            await update.effective_message.reply_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(rows),
            )


async def render_phases(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session: Session | None = ctx.user_data.get("session")
    if session is None:
        return
    warn = warn_exhausted_stages(session.options)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if o.is_selectable else '⛔'} {esc(o.name)[:40]}",
                callback_data=f"ph:{i}",
            )
        ]
        for i, o in enumerate(session.options)
        if o.is_selectable
    ]
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="cancel")])

    await update.effective_message.reply_text(
        render_phase_text(session, warn),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ── callbacks ─────────────────────────────────────────────────────────


async def on_phase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    session: Session | None = ctx.user_data.get("session")
    if session is None:
        await query.edit_message_text("Sesi habis. Kirim /mint lagi.")
        return
    index = int(query.data.split(":")[1])
    if not 0 <= index < len(session.options) or not session.options[index].is_selectable:
        await query.edit_message_text("Phase itu nggak bisa dipilih.")
        return
    session.selected_index = index
    await show_quantity_buttons(update, ctx, query.message.message_id, index, fast=False)


async def on_quantity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    session: Session | None = ctx.user_data.get("session")
    if session is None or session.selected_index is None:
        await query.edit_message_text("Sesi habis. Kirim link lagi.")
        return
    session.quantity = int(query.data.split(":")[1])
    option = session.options[session.selected_index]
    session.nonce = secrets.token_urlsafe(8)
    session.nonce_born = time.monotonic()
    rows = [[
        InlineKeyboardButton(
            f"✅ MINT {session.quantity} NFT", callback_data=f"go:{session.nonce}"
        ),
        InlineKeyboardButton("❌ Batal", callback_data="cancel"),
    ]]
    await query.edit_message_text(
        f"*{esc(option.name)}*\n"
        f"Qty: *{session.quantity}*\n"
        f"Wallet: `{esc(session.loaded.signer.identity.address)}`\n\n"
        "⚠️ *Ini mengirim transaksi sungguhan.* "
        f"Konfirmasi dalam {CONFIRM_TTL_SECONDS} detik.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    drop_session(ctx)
    await query.edit_message_text("Sesi dibatalkan.")


async def on_no(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    drop_session(ctx)
    await query.edit_message_text("Dibatalkan, tidak ada tx yang dikirim.")


async def on_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    session: Session | None = ctx.user_data.get("session")
    parts = query.data.split(":")
    # Fast path (single stage): "go:<qty>" with a nonce pre-issued by the screen.
    # Slow path (multi stage): "go:<nonce>" after an explicit confirm screen.
    if session is None:
        await query.edit_message_text("Sesi habis. Kirim link lagi.")
        return
    if len(parts) == 3 and parts[1] == "q":
        session.quantity = int(parts[2])
    elif len(parts) == 2 and session.nonce != parts[1]:
        await query.edit_message_text("Sesi atau konfirmasi kedaluwarsa. Kirim link lagi.")
        return
    is_fast = len(parts) == 3 and parts[1] == "q"
    if not is_fast and time.monotonic() - session.nonce_born > CONFIRM_TTL_SECONDS:
        await session.close()
        await query.edit_message_text("⏱ Konfirmasi kedaluwarsa. Kirim link lagi.")
        return

    index, quantity = session.selected_index, session.quantity
    if index is None or quantity is None:
        await session.close()
        await query.edit_message_text("Sesi tidak lengkap. /mint lagi.")
        return

    stage = session.metadata.stages[index]
    option = session.options[index]
    chain, gateway, client, loaded = (
        session.chain, session.gateway, session.client, session.loaded
    )

    status = await query.edit_message_text(
        f"🚀 *Mengirim mint*\n"
        f"Phase: {esc(option.name)}\n"
        f"Qty: {quantity}\n"
        f"Chain: {chain.chain_id}\n\n"
        f"_{esc(option.state)}_",
        parse_mode=ParseMode.MARKDOWN,
    )
    ctx.user_data["chat_id"] = query.message.chat_id
    clock = SystemUtcClock()

    try:
        validate_mint_limits(session.metadata, stage, session.eligibility, quantity)
        phase = stage_window(stage)
        if stage.stage_type != "PUBLIC_SALE":
            ensure_mint_not_expired(None, phase.ends_at)
        target = None
        if stage.stage_type == "PUBLIC_SALE":
            target = await discover_seadrop_address(
                client, gateway, chain, session.metadata
            )
        outcome = await execute_mint_phase(
            MintExecutionContext(
                config=loaded.app,
                chain=chain,
                gateway=gateway,
                client=client,
                signer=loaded.signer,
                metadata=session.metadata,
                eligibility=session.eligibility,
                selected_stage=stage,
                phase=phase,
                is_scheduled=is_future_phase(phase.starts_at, clock),
                quantity=quantity,
                gas_limit=loaded.app.gas_limit,
                public_mint_target=target,
                system_clock=clock,
            )
        )
    except Exception as error:  # noqa: BLE001
        await session.close()
        await status.edit_text(
            f"❌ *Mint gagal*\n`{esc(error)}`", parse_mode=ParseMode.MARKDOWN
        )
        return

    await session.close()

    tx_hash = outcome.submission.transaction_hash
    base = EXPLORER.get(int(chain.chain_id), "")
    link = f"{base}{tx_hash}" if base else tx_hash
    short = f"{tx_hash[:18]}…{tx_hash[-6:]}"
    if outcome.state is ExecutionState.COMPLETED:
        text = (
            f"✅ *Mint sukses*\n"
            f"Phase: {esc(option.name)}\nQty: {quantity}\n"
            f"TX: [{esc(short)}]({link})"
        )
    elif outcome.state is ExecutionState.MINED_REVERTED:
        text = (
            f"❌ *Tx revert*\n"
            f"Phase: {esc(option.name)}\nQty: {quantity}\n"
            f"TX: [{esc(short)}]({link})\n\n_Cek receipt-nya._"
        )
    else:
        text = (
            f"⚠️ *Belum terkonfirmasi*\n"
            f"State: `{esc(outcome.state.value)}`\n"
            f"TX: [{esc(short)}]({link})\n\n"
            f"_Cek manual, jangan kirim ulang._"
        )
    await status.edit_text(text, parse_mode=ParseMode.MARKDOWN)


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pylogging.exception("unhandled", exc_info=ctx.error)
    session: Session | None = ctx.user_data.get("session")
    if session is not None:
        await session.close()
    chat_id = ctx.user_data.get("chat_id") or getattr(
        getattr(update, "effective_chat", None), "id", None
    )
    if chat_id:
        try:
            await ctx.bot.send_message(
                chat_id, f"❌ Error: `{esc(ctx.error)}`", parse_mode=ParseMode.MARKDOWN
            )
        except (Forbidden, BadRequest):
            pass


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only. Runs in group -1 so it sees every update first."""
    chat = update.effective_chat
    if chat is not None and chat.id != OWNER_ID:
        try:
            await chat.send_message("Unauthorized.")
        except (Forbidden, BadRequest):
            pass
        raise ApplicationHandlerStop


def _configure_logging() -> None:
    """Send logs to stderr and to a rotating file next to the app.

    On Linux systemd already captures stderr into the journal, but a scheduled
    task on Windows runs hidden with no console, so stdout would be discarded
    and a failure would be undiagnosable. The file handler is what makes
    Get-ScheduledTaskInfo and the log tail useful there.
    """
    pylogging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        from logging.handlers import RotatingFileHandler

        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            pylogging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        pylogging.getLogger().addHandler(handler)
    except OSError:
        # A read-only checkout is not a reason to refuse to start; stderr still works.
        pylogging.getLogger().debug("file logging unavailable", exc_info=True)


def main() -> None:
    _configure_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not OWNER_ID:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is not set")

    app = ApplicationBuilder().token(token).concurrent_updates(False).build()
    app.add_handler(TypeHandler(object, guard), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("mint", cmd_mint))
    # Bare links/slugs/addresses auto-start a session, no command needed.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link), group=1)
    app.add_handler(CallbackQueryHandler(on_phase, pattern=r"^ph:"))
    app.add_handler(CallbackQueryHandler(on_quantity, pattern=r"^qt:"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(on_no, pattern=r"^no:"))
    app.add_handler(CallbackQueryHandler(on_wallet_set_button, pattern=r"^wset$"))
    app.add_handler(CallbackQueryHandler(on_wallet_clear, pattern=r"^wclear:[A-Za-z0-9_-]+"))
    app.add_handler(CallbackQueryHandler(on_go, pattern=r"^go:"))  # go:q:N and go:<nonce>
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
