"""Telegram command and callback handlers.

Thin adapters: read the update, call into flows/render/wallet, send the reply.
Mint mechanics remain upstream in osnm_z.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import logging as pylogging
import secrets
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop, ContextTypes

from osnm_z.command import run_diagnostics
from osnm_z.config import ConfigError
from osnm_z.execution import MintExecutionContext, execute_mint_phase
from osnm_z.launch_timing import ensure_mint_not_expired, is_future_phase
from osnm_z.outcomes import ExecutionState
from osnm_z.phase_selection import stage_window, validate_mint_limits
from osnm_z.public_mint import discover_seadrop_address
from osnm_z.scheduling import SystemUtcClock

from .config import app_dir, CONFIRM_TTL_SECONDS, EXPLORER, OWNER_ID
from .config import load
from .flows import begin_session, drop_session, show_quantity_buttons
from .locks import CHAT_LOCK
from .models import Session
from .locator import looks_like_locator
from .render import esc
from .text import HELP
from .wallet import _atomic_write_env, wallet_status_text

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
        _atomic_write_env(app_dir() / ".env", "WALLET_KEY", f"0x{key}")
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
        f"Key: `{app_dir() / '.env'}` (600, tidak pernah ditampilkan)\n\n"
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
    _atomic_write_env(app_dir() / ".env", "WALLET_KEY", "")
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

