"""Inline-keyboard callbacks and the mint broadcast path."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from osnm_z.outcomes import ExecutionState
from osnm_z.execution import MintExecutionContext
from osnm_z.scheduling import SystemUtcClock
from osnm_z.public_mint import discover_seadrop_address
from osnm_z.launch_timing import ensure_mint_not_expired
from osnm_z.execution import execute_mint_phase
from osnm_z.launch_timing import is_future_phase
from osnm_z.phase_selection import stage_window
from osnm_z.phase_selection import validate_mint_limits

from .config import CONFIRM_TTL_SECONDS, EXPLORER
from .flows import begin_session, drop_session, show_quantity_buttons
from .locator import looks_like_locator
from .models import Session, TrackedMint
from .render import esc, render_simulation
from .wallet import on_wallet_key


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
        InlineKeyboardButton("🧪 Simulasi dulu", callback_data=f"sim:{session.nonce}"),
    ], [
        InlineKeyboardButton("❌ Batal", callback_data="cancel"),
    ]]
    await query.edit_message_text(
        f"*{esc(option.name)}*\n"
        f"Qty: *{session.quantity}*\n"
        f"Wallet: `{esc(session.loaded.signer.identity.address)}`\n\n"
        "🧪 *Simulasi* jalanin mint sebagai eth_call — cek quota, allowlist, "
        "dan saldo tanpa kirim apa pun.\n\n"
        "⚠️ *MINT mengirim transaksi sungguhan.* "
        f"Konfirmasi dalam {CONFIRM_TTL_SECONDS} detik.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Dry-run the mint. Builds the calldata, eth_calls it, signs nothing."""
    query = update.callback_query
    await query.answer()
    session: Session | None = ctx.user_data.get("session")
    if session is None or session.selected_index is None or session.quantity is None:
        await query.edit_message_text("Sesi habis. Kirim link lagi.")
        return
    if session.nonce != query.data.split(":")[1]:
        await query.edit_message_text("Simulasi kedaluwarsa. Kirim link lagi.")
        return
    if time.monotonic() - session.nonce_born > CONFIRM_TTL_SECONDS:
        await query.edit_message_text("⏱ Konfirmasi kedaluwarsa. Kirim link lagi.")
        return

    from .simulate import simulate_mint

    index, quantity = session.selected_index, session.quantity
    stage = session.metadata.stages[index]
    option = session.options[index]
    phase = stage_window(stage)
    target = None
    if stage.stage_type == "PUBLIC_SALE":
        try:
            target = await discover_seadrop_address(
                session.client, session.gateway, session.chain, session.metadata
            )
        except Exception:  # noqa: BLE001
            target = None
    await query.edit_message_text(
        "🧪 *Simulasi berjalan*\nMembangun calldata, tidak menandatangani apa pun…",
        parse_mode=ParseMode.MARKDOWN,
    )
    report = await simulate_mint(
        client=session.client,
        gateway=session.gateway,
        chain=session.chain,
        config=session.loaded.app,
        metadata=session.metadata,
        eligibility=session.eligibility,
        stage=stage,
        phase=phase,
        quantity=quantity,
        wallet=session.loaded.signer.identity.address,
        public_target=target,
    )
    await query.edit_message_text(
        render_simulation(report, option.name, int(session.chain.chain_id)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ MINT {quantity} NFT", callback_data=f"go:{session.nonce}"
            ),
            InlineKeyboardButton("❌ Batal", callback_data="cancel"),
        ]]),
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

    # Remember it so /status can answer once the transaction is in flight.
    # The RPC that broadcast it is the one most likely to know the receipt, so
    # it is recorded rather than looked up again later.
    ctx.user_data["tracked"] = TrackedMint(
        transaction_hash=tx_hash,
        chain_id=int(chain.chain_id),
        rpc_url=chain.rpc_url,
        stage_name=option.name,
        quantity=quantity,
        sent_at=time.monotonic(),
    )
