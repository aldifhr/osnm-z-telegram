"""Reading the active wallet and persisting a new private key.

The key file holds a secret, so every write is atomic and the replacement
file is created restricted from the start.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import os
import re
from contextlib import AsyncExitStack
from pathlib import Path

from osnm_z import chain as _chain

# Resolved through the module so a test can substitute the gateway class.
ChainGateway = _chain.ChainGateway
from osnm_z.config import ConfigError

from .config import app_dir, CHAIN_NAMES, candidate_rpcs, load
from .render import esc

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
            f"{app_dir() / '.env'}`"
        )
    address = loaded.signer.identity.address
    lines.append(f"`{address}`")
    lines.append(f"key: `{app_dir() / '.env'}` (600, never displayed)")
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

