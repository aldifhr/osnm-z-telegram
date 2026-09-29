"""Tests for the /status tracking and the dry-run simulation."""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from dataclasses import dataclass

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
# Resolve from the file, not a fixed path: the suite has to run both in the
# app tree and from a fresh clone or a Windows install.
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from osnmzbot.models import TrackedMint  # noqa: E402
from osnmzbot.onchain import SimulationResult  # noqa: E402
from osnmzbot.status import TxStatus, describe, track_status  # noqa: E402


@dataclass
class FakeReceipt:
    block_number: int
    is_success: bool


class FakeGateway:
    """Minimal stand-in. Records what it was asked, so tests can assert the
    transaction was only read and never broadcast."""

    def __init__(self, receipt=None, head=100, error=None):
        self.receipt = receipt
        self.head = head
        self.error = error
        self.calls: list[tuple[str, list]] = []

    async def transaction_receipts(self, chain, hashes):
        self.calls.append(("eth_getTransactionReceipt", list(hashes)))
        if self.error:
            raise self.error
        return (self.receipt,) if self.receipt is not None else (None,)

    async def _request(self, endpoint, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        raise AssertionError(f"unexpected method {method}")


def make_tracked(**overrides) -> TrackedMint:
    base = dict(
        transaction_hash="0x" + "ab" * 32,
        chain_id=4663,
        rpc_url="https://example.invalid",
        stage_name="Public",
        quantity=1,
        sent_at=0.0,
    )
    base.update(overrides)
    return TrackedMint(**base)


def test_pending_receipt_is_pending():
    gateway = FakeGateway(receipt=None)
    import time

    tracked = make_tracked(sent_at=time.monotonic())
    status = asyncio.run(track_status(gateway, tracked))
    assert status.state == "pending"
    assert "TransactionReceipt" in gateway.calls[0][0]


def test_succeeded_receipt_reports_confirmations():
    gateway = FakeGateway(receipt=FakeReceipt(block_number=90, is_success=True), head=100)
    tracked = make_tracked(sent_at=0.0)
    status = asyncio.run(track_status(gateway, tracked))
    assert status.state == "succeeded"
    assert status.confirmations == 11  # head 100, block 90 -> 100-90+1


def test_reverted_receipt_is_not_succeeded():
    gateway = FakeGateway(receipt=FakeReceipt(block_number=90, is_success=False), head=100)
    tracked = make_tracked(sent_at=0.0)
    status = asyncio.run(track_status(gateway, tracked))
    assert status.state == "reverted"
    assert status.confirmations == 11


def test_old_unmined_tx_is_dropped_not_pending():
    import time

    gateway = FakeGateway(receipt=None)
    tracked = make_tracked(sent_at=time.monotonic() - 400)
    status = asyncio.run(track_status(gateway, tracked))
    assert status.state == "dropped"


def test_status_read_never_broadcasts():
    gateway = FakeGateway(receipt=FakeReceipt(block_number=90, is_success=True), head=100)
    asyncio.run(track_status(gateway, make_tracked()))
    for method, _ in gateway.calls:
        assert "sendRawTransaction" not in method
        assert method in ("eth_getTransactionReceipt", "eth_blockNumber")


def test_describe_includes_hash_and_state():
    gateway = FakeGateway(receipt=FakeReceipt(block_number=90, is_success=True), head=100)
    tracked = make_tracked()
    status = asyncio.run(track_status(gateway, tracked))
    text = describe(tracked, status)
    assert tracked.transaction_hash in text
    assert "Sukses" in text
    assert "Confirmations" in text


def test_describe_omits_stage_for_simulated():
    tracked = make_tracked(simulated=True)
    text = describe(tracked, TxStatus(state="pending"))
    assert "Stage:" not in text


def test_invalid_hash_is_rejected_by_receipt_read():
    from osnm_z.errors import MintError
    from osnmzbot.onchain import read_receipt

    class Gw:
        async def _request(self, *a, **k):
            raise AssertionError("must not reach rpc for an invalid hash")

    with pytest.raises(MintError):
        asyncio.run(read_receipt(Gw(), None, "not-a-hash"))


def test_simulation_revert_selector_extracted():
    result = SimulationResult(ok=False, revert_data=bytes.fromhex("deadbeef"))
    assert result.revert_selector == "0xdeadbeef"


def test_simulation_no_revert_data_returns_none_selector():
    assert SimulationResult(ok=False).revert_selector is None


def test_simulation_ok_has_no_revert():
    assert SimulationResult(ok=True).revert_selector is None
