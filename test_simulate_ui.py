"""Tests for the simulate button wiring in callbacks.on_simulate."""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
# Resolve from the file, not a fixed path: the suite has to run both in the
# app tree and from a fresh clone or a Windows install.
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from osnmzbot import callbacks  # noqa: E402


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.answered = False
        self.edits: list[tuple[str, object]] = []

    async def answer(self, *a, **k):  # noqa: ANN001
        self.answered = True

    async def edit_message_text(self, text, **kwargs):  # noqa: ANN001
        self.edits.append((text, kwargs.get("reply_markup")))
        return self


class FakeUpdate:
    def __init__(self, data: str):
        self.callback_query = FakeQuery(data)


class FakeCtx:
    def __init__(self, user_data):
        self.user_data = user_data
        self.bot = None


def make_session(nonce="abc123", qty=2, chain_id=4663):  # noqa: ANN001
    stage = types.SimpleNamespace(stage_type="PRIVATE_SALE", name="GTD")
    metadata = types.SimpleNamespace(stages=[stage, stage], slug="x")

    class Signer:
        identity = types.SimpleNamespace(address="0x" + "22" * 20)

    loaded = types.SimpleNamespace(app=object(), signer=Signer())
    return {
        "session": types.SimpleNamespace(
            locator="x", selected_index=0, quantity=qty, nonce=nonce,
            nonce_born=0.0, metadata=metadata, client=None, gateway=None,
            chain=types.SimpleNamespace(chain_id=chain_id, rpc_url="u"),
            loaded=loaded, options=[types.SimpleNamespace(name="GTD")],
            eligibility=None,
        )
    }


def test_simulate_rejects_a_mismatched_nonce(monkeypatch):
    """A stale confirm button must not run against a newer session."""
    data = make_session(nonce="current")
    update = FakeUpdate("sim:someone-elses")
    ctx = FakeCtx(data)
    asyncio.run(callbacks.on_simulate(update, ctx))
    assert "kedaluwarsa" in update.callback_query.edits[-1][0].lower()


def test_simulate_rejects_a_missing_session():
    update = FakeUpdate("sim:abc")
    ctx = FakeCtx({})
    asyncio.run(callbacks.on_simulate(update, ctx))
    assert "Sesi habis" in update.callback_query.edits[-1][0]


def test_simulate_answers_the_callback_first():
    data = make_session(nonce="current")
    update = FakeUpdate("sim:other")
    ctx = FakeCtx(data)
    asyncio.run(callbacks.on_simulate(update, ctx))
    assert update.callback_query.answered is True


def test_simulate_expired_nonce_is_refused(monkeypatch):
    import time

    data = make_session(nonce="current")
    data["session"].nonce_born = time.monotonic() - 10_000
    update = FakeUpdate("sim:current")
    ctx = FakeCtx(data)
    asyncio.run(callbacks.on_simulate(update, ctx))
    assert "kedaluwarsa" in update.callback_query.edits[-1][0].lower()
