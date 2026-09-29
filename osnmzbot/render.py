"""Message text and price formatting.

Pure functions over a Session: no I/O, no Telegram, testable without a bot.
"""

from __future__ import annotations

import html
from typing import Any

from osnm_z.phase_selection import warn_exhausted_stages

from .config import CHAIN_NAMES
from .models import Session

def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_phase(option: Any, index: int) -> str:
    mark = "✅" if option.is_selectable else "⛔"
    kind = "Public" if option.stage_type == "PUBLIC_SALE" else "Allowlist"
    return (
        f"{mark} *{index}.* {esc(option.name)[:60]}\n"
        f"    `{kind}` · {esc(option.state)}\n"
        f"    {esc(option.eligibility)}\n"
        f"    max: {option.max_quantity if option.max_quantity is not None else '—'}"
    )


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



def render_simulation(report: Any, stage_name: str, chain_id: int) -> str:
    """Format a dry-run result.

    The verdict line is deliberately explicit about what was and was not done:
    a green "lolos" here means the eth_call reverted nothing, not that a
    transaction was sent, and the message has to say so.
    """
    if report.error:
        return (
            "🧪 *Simulasi gagal*\n"
            f"Phase: {esc(stage_name)}\n\n"
            f"`{esc(report.error)}`\n\n"
            "_Bukan karena tx-nya gagal — simulasinya sendiri yang nggak jalan._"
        )
    lines = [
        "🧪 *Hasil simulasi* — tidak ada tx yang dikirim",
        f"Phase: {esc(stage_name)}",
        f"Qty: {report.quantity}",
    ]
    if report.calldata:
        lines.append(
            f"Calldata: `{report.calldata[:8].hex()}…` ({len(report.calldata)} byte)"
        )
    if report.value_wei:
        lines.append(f"Nilai: `{format_native(report.value_wei, chain_id)}`")
    if report.fee_estimate_wei is not None:
        lines.append(f"Gas (maks): `{format_native(report.fee_estimate_wei, chain_id)}`")
    if report.balance_wei is not None:
        lines.append(f"Saldo: `{format_native(report.balance_wei, chain_id)}`")

    lines.append("")
    if report.ok:
        lines.append("🟢 *Lolos* — eth_call nggak revert.")
    else:
        lines.append("🔴 *Gagal* — eth_call revert.")
        if report.result and report.result.revert_selector:
            lines.append(f"Error selector: `{report.result.revert_selector}`")
    if report.affordable is False:
        lines.append(
            f"🔴 *Saldo kurang* — kurang `{format_native(report.missing_wei, chain_id)}`"
        )
    elif report.affordable is True:
        lines.append("🟢 Saldo cukup.")
    lines.append(
        "\n_Simulasi cuma eth_call: allowlist, quota, dan saldo dicek. "
        "Gas tetap dibayar kalau lo tekan MINT._"
    )
    return "\n".join(lines)
