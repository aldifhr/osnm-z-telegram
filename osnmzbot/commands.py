"""Slash commands: /start /help /wallet /cancel /doctor /mint /status."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from osnm_z.config import ConfigError
from osnm_z.command import run_diagnostics

from .locks import CHAT_LOCK
from .text import HELP
from .wallet import apply_wallet_key
from .models import TrackedMint
from .flows import begin_session
from .status import build_gateway
from .status import describe
from .flows import drop_session
from .render import esc
from .config import load
from .status import track_status
from .wallet import wallet_status_text


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP, parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP, parse_mode=ParseMode.MARKDOWN
    )

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

async def cmd_mint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await begin_session(update, ctx, " ".join(ctx.args).strip())

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — where did my last mint go?

    Reads the receipt instead of linking an explorer and hoping. A mined
    transaction with status 0 is reported as reverted (gas spent, nothing
    received), which is a different problem from a transaction that never
    landed (no gas spent, safe to retry).
    """
    tracked: TrackedMint | None = ctx.user_data.get("tracked")
    if tracked is None:
        await update.effective_message.reply_text(
            "Belum ada tx yang dilacak. Bot belum pernah broadcast mint buat lo."
        )
        return
    try:
        loaded = load()
    except ConfigError as error:
        await update.effective_message.reply_text(f"❌ Config error: `{esc(error)}`")
        return

    gateway = build_gateway(loaded)
    try:
        await gateway.__aenter__()
        result = await track_status(gateway, tracked)
    finally:
        await gateway.__aexit__(None, None, None)
    await update.effective_message.reply_text(
        describe(tracked, result), parse_mode=ParseMode.MARKDOWN
    )
