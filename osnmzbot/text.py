"""User-facing copy that is not formatting logic.

Kept apart from render.py so wording changes do not collide with the price
and supply formatting beside it.
"""

from __future__ import annotations


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

