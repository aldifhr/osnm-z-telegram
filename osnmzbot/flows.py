"""Session lifecycle: open, price, render keyboards, tear down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import asyncio
import logging as pylogging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from osnm_z.chain import ChainGateway
from osnm_z.config import ConfigError
from osnm_z.errors import MintError
from osnm_z.opensea import WalletOpenSeaClient
from osnm_z.opensea_protocol import ErrorKind, ProtocolError, parse_collection_locator
from osnm_z.phase_selection import (build_phase_options, public_only_eligibility,
    validate_snapshot_shape, validate_stage_windows, warn_exhausted_stages)
from osnm_z.scheduling import SystemUtcClock

from .config import CHAIN_NAMES, candidate_rpcs, load
from .locks import CHAT_LOCK
from .models import Session
from .onchain import read_total_supply
from .render import esc, render_phase_text, render_quantity_text

def drop_session(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Pop and synchronously schedule teardown of any open session."""
    session: Session | None = ctx.user_data.pop("session", None)
    if session is not None:
        asyncio.get_event_loop().create_task(session.close())


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

