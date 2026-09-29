"""Entry point: logging setup and handler registration.
"""

from __future__ import annotations

import logging as pylogging
import os
import sys
from pathlib import Path

from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    MessageHandler, TypeHandler, filters)

from .config import OWNER_ID
from .handlers import (cmd_cancel, cmd_doctor, cmd_help, cmd_mint, cmd_start,
    cmd_status, cmd_wallet, error_handler, guard, on_cancel, on_go, on_link,
    on_no, on_phase, on_quantity, on_simulate, on_wallet_clear,
    on_wallet_set_button)

def _log_dir() -> Path:
    """Where the rotating log lives. Overridable via OSNM_Z_LOG_DIR.

    Split out of _configure_logging so a test can redirect the log, and so an
    operator can move it off a read-only checkout.
    """
    override = os.environ.get("OSNM_Z_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # One level up from osnmzbot/ so the log lands in bot/logs/, beside the
    # launcher scripts and where the README says to look. Keeping it inside
    # the package would also ship a runtime directory into the install tree.
    return Path(__file__).resolve().parent.parent / "logs"


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

        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        # The log records request and response detail, so keep it owner-only on
        # platforms that have POSIX modes. No-op on Windows, where setup.ps1
        # applies NTFS ACLs instead.
        try:
            os.chmod(log_dir, 0o700)
            os.chmod(log_dir / "bot.log", 0o600)
        except OSError:
            pass
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
    app.add_handler(CommandHandler("status", cmd_status))
    # Bare links/slugs/addresses auto-start a session, no command needed.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link), group=1)
    app.add_handler(CallbackQueryHandler(on_phase, pattern=r"^ph:"))
    app.add_handler(CallbackQueryHandler(on_quantity, pattern=r"^qt:"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(on_no, pattern=r"^no:"))
    app.add_handler(CallbackQueryHandler(on_wallet_set_button, pattern=r"^wset$"))
    app.add_handler(CallbackQueryHandler(on_wallet_clear, pattern=r"^wclear:[A-Za-z0-9_-]+"))
    app.add_handler(CallbackQueryHandler(on_simulate, pattern=r"^sim:"))
    app.add_handler(CallbackQueryHandler(on_go, pattern=r"^go:"))  # go:q:N and go:<nonce>
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)

