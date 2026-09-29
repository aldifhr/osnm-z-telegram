"""Tests for the dry-run simulation.

The important one is test_simulate_never_signs: a dry run that signs is worse
than no dry run, because it produces a valid signature the user never asked for
and burns a nonce. The fake signer raises rather than records, so any call at
all fails the test.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
# Resolve from the file, not a fixed path: the suite has to run both in the
# app tree and from a fresh clone or a Windows install.
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from osnmzbot.simulate import SimulationReport, simulate_mint  # noqa: E402


class ExplodingSigner:
    """Any interaction is a failure, not a record."""

    def __getattr__(self, name):  # noqa: ANN001
        def boom(*a, **k):  # noqa: ANN001
            raise AssertionError(f"dry run touched the signer via {name}")

        return boom


class FakeGateway:
    def __init__(self, balance=10**15, call_ok=True, revert=b""):
        self.balance_wei = balance
        self.call_ok = call_ok
        self.revert = revert
        self.calls: list[tuple[str, object]] = []

    async def balance(self, chain, wallet):
        self.calls.append(("balance", wallet))
        return self.balance_wei

    async def _request(self, endpoint, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "eth_call":
            if self.call_ok:
                return "0x"
            raise RuntimeError(f"execution reverted: 0x{self.revert.hex()}")
        raise AssertionError(f"dry run issued {method}")


class FakeAction:
    def __init__(self):
        self.target = "0xf517d3a2c499cce82c0f9e37d9e6c79319f3d9a3"
        self.value = 10**14
        self.calldata = bytes.fromhex("deadbeef")


class ManualFees:
    """Manual fees make the gas cost exact, so the report has real numbers."""

    automatic = False
    max_fee_per_gas = 10**9
    max_priority_fee_per_gas = 10**8


class FakeConfig:
    fees = ManualFees()
    gas_limit = 300_000


class FakeChain:
    rpc_url = "https://example.invalid"
    chain_id = 4663


def report(**over) -> SimulationReport:
    base = dict(
        ok=True, stage_name="Public", quantity=1, target="0xabc", value_wei=10**14,
        calldata=b"\xde\xad", balance_wei=10**15, fee_estimate_wei=10**12, result=None,
    )
    base.update(over)
    return SimulationReport(**base)


def test_report_marks_unaffordable_wallet():
    r = report(balance_wei=10**12)
    assert r.affordable is False
    assert r.missing_wei > 0


def test_report_marks_affordable_wallet():
    r = report(balance_wei=10**16)
    assert r.affordable is True
    assert r.missing_wei == 0


def test_report_claims_nothing_when_balance_unknown():
    r = report(balance_wei=None, fee_estimate_wei=None)
    assert r.affordable is None


def test_simulate_sends_no_broadcast_method():
    """The whole point: eth_call only, never eth_sendRawTransaction."""
    from osnmzbot import simulate as sim

    gateway = FakeGateway()
    sent: list[str] = []

    class FakePublic:
        target = "0xdead"
        value = 10**14
        calldata = b"\x01"

    async def fake_resolve(*a, **k):  # noqa: ANN001
        return object(), object()

    def fake_build(*a, **k):  # noqa: ANN001
        # build_public_mint_action is synchronous upstream.
        return FakePublic()

    sim.resolve_public_mint_context_and_stats = fake_resolve
    sim.build_public_mint_action = fake_build

    stage = type("S", (), {"stage_type": "PUBLIC_SALE", "name": "Public"})()

    async def run():
        return await sim.simulate_mint(
            client=object(), gateway=gateway, chain=FakeChain(), config=FakeConfig(),
            metadata=object(), eligibility=None, stage=stage, phase=object(),
            quantity=1, wallet="0x" + "11" * 20, public_target="0xdead",
        )

    out = asyncio.run(run())
    methods = [m for m, _ in gateway.calls]
    assert "eth_call" in methods
    assert not any("sendRawTransaction" in m for m in methods)
    assert not sent
    assert out.ok is True


def test_simulate_reports_revert_as_failure():
    from osnmzbot import simulate as sim

    gateway = FakeGateway(call_ok=False, revert=bytes.fromhex("deadbeef"))

    class FakePublic:
        target = "0xdead"
        value = 0
        calldata = b"\x01"

    async def fake_resolve(*a, **k):  # noqa: ANN001
        return object(), object()

    def fake_build(*a, **k):  # noqa: ANN001
        # build_public_mint_action is synchronous upstream.
        return FakePublic()

    sim.resolve_public_mint_context_and_stats = fake_resolve
    sim.build_public_mint_action = fake_build
    stage = type("S", (), {"stage_type": "PUBLIC_SALE", "name": "Public"})()

    async def run():
        return await sim.simulate_mint(
            client=object(), gateway=gateway, chain=FakeChain(), config=FakeConfig(),
            metadata=object(), eligibility=None, stage=stage, phase=object(),
            quantity=1, wallet="0x" + "11" * 20, public_target="0xdead",
        )

    out = asyncio.run(run())
    assert out.ok is False
    assert out.result is not None
    assert out.result.revert_selector == "0xdeadbeef"
    methods = [m for m, _ in gateway.calls]
    assert not any("sendRawTransaction" in m for m in methods)


def test_simulate_module_never_references_a_signer():
    """Structural guarantee: no signer is imported or constructed here."""
    from pathlib import Path

    import osnmzbot.simulate as sim

    source = Path(sim.__file__).read_text()
    for forbidden in ("sign_transaction", "broadcast_signed", "sign_mint_transaction",
                      "eth_sendRawTransaction", "prepared.signed"):
        assert forbidden not in source, f"simulate.py references {forbidden}"
