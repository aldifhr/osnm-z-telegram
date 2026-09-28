"""Process-wide state shared across modules.

Isolated so models, handlers, and app can all reach it without importing
each other.
"""

from __future__ import annotations

import asyncio


CHAT_LOCK = asyncio.Lock()
