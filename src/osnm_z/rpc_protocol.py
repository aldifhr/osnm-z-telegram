"""Ethereum JSON-RPC models, encoding, response validation, and error classification."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from eth_utils.address import is_address, to_checksum_address

from .config import RetryConfig
from .domain import UINT256_MAX
from .transaction import SignedTransaction

RPC_ERROR_DETAIL_LIMIT = 2 * 1024
_RPC_ERROR_HEX_DATA = re.compile(r"\b(?:0[xX])?[0-9a-fA-F]{65,}\b")
_RPC_ERROR_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


class ChainError(RuntimeError):
    """An RPC transport or protocol boundary failed."""


class BatchReadError(ChainError):
    """An individual or grouped RPC read failed outside a method-specific error response."""


class RateLimitError(BatchReadError):
    """An RPC endpoint reported rate limiting without proving transaction admission."""


class RpcResponseError(ChainError):
    """A valid JSON-RPC response reported an error for one requested method call."""

    __slots__ = ("code", "provider_message")

    def __init__(self, message: str, code: int, provider_message: str | None = None) -> None:
        self.code = code
        self.provider_message = provider_message
        super().__init__(message)


class SubmissionDisposition(Enum):
    """Whether submission was acknowledged, uncertain, or definitively rejected."""

    ACKNOWLEDGED = "acknowledged"
    POSSIBLY_SUBMITTED = "possibly_submitted"
    REJECTED = "rejected"


class TransactionSubmissionError(ChainError):
    """RPC_URL did not acknowledge the signed transaction."""

    __slots__ = ("disposition",)

    def __init__(
        self,
        message: str,
        disposition: SubmissionDisposition,
    ) -> None:
        self.disposition = disposition
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RawTransactionSubmission:
    """The locally known hash and delivery disposition of one transaction."""

    transaction_hash: str
    disposition: SubmissionDisposition


@dataclass(frozen=True, slots=True)
class FeeEstimate:
    max_priority_fee_per_gas: int
    max_fee_per_gas: int


@dataclass(frozen=True, slots=True)
class SubmissionInputs:
    pending_nonce: int
    fee_estimate: FeeEstimate


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    balance: int
    submission_inputs: SubmissionInputs


@dataclass(frozen=True, slots=True)
class ReceiptPollingPolicy:
    timeout_seconds: float
    interval_ms: int

    @classmethod
    def from_retry_config(cls, config: RetryConfig) -> ReceiptPollingPolicy:
        return cls(config.pending_timeout_seconds, config.poll_interval_ms)


@dataclass(frozen=True, slots=True)
class TransactionLog:
    address: str
    topics: tuple[bytes, ...]
    data: bytes


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    transaction_hash: str
    block_number: int
    is_success: bool
    logs: tuple[TransactionLog, ...] = ()


def extract_json_rpc_result(envelope: object, expected_id: int) -> Any:
    if not isinstance(envelope, dict):
        raise invalid_rpc_response()
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != expected_id
        or ("result" in envelope and "error" in envelope)
    ):
        raise invalid_rpc_response()
    if "error" in envelope:
        error = envelope["error"]
        if not isinstance(error, dict) or type(error.get("code")) is not int:
            raise invalid_rpc_response()
        code = error["code"]
        message = error.get("message", "")
        if _is_rate_limit_error(code, message):
            raise RateLimitError("RPC endpoint rate limited the request")
        detail = _format_rpc_error_detail(error)
        suffix = f": {detail}" if detail else ""
        provider_message = message if isinstance(message, str) else None
        raise RpcResponseError(
            f"RPC endpoint returned error code {code}{suffix}",
            code,
            provider_message,
        )
    if "result" not in envelope:
        raise invalid_rpc_response()
    return envelope["result"]


def parse_json_rpc_quantity(value: object, bits: int) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid JSON-RPC quantity")
    digits = value[2:]
    if (
        not digits
        or bits <= 0
        or len(digits) > (bits + 3) // 4
        or any(digit not in "0123456789abcdefABCDEF" for digit in digits)
        or (len(digits) > 1 and digits.startswith("0"))
    ):
        raise ValueError("invalid JSON-RPC quantity")
    result = int(digits, 16)
    if result >= 1 << bits:
        raise ValueError("JSON-RPC quantity overflow")
    return result


def validate_returned_transaction_hash(
    returned_hash: object,
    signed: SignedTransaction,
) -> None:
    if not is_transaction_hash(returned_hash):
        raise invalid_rpc_response()
    assert isinstance(returned_hash, str)
    if returned_hash.lower() != signed.hash_hex.lower():
        raise ChainError("RPC endpoint returned a transaction hash that differs from local signing")


def failed_submission_disposition(error: ChainError) -> SubmissionDisposition:
    if isinstance(error, RpcResponseError):
        message = (error.provider_message or "").lower()
        # A low nonce may mean this transaction has already been mined.
        if "nonce too low" in message:
            return SubmissionDisposition.POSSIBLY_SUBMITTED
        if error.code in {-32_600, -32_601, -32_602, -32_003} or any(
            reason in message
            for reason in (
                "insufficient funds",
                "nonce too high",
                "intrinsic gas too low",
                "exceeds block gas limit",
                "invalid sender",
                "transaction underpriced",
                "replacement transaction underpriced",
                "max fee per gas less than block base fee",
                "tip above fee cap",
            )
        ):
            return SubmissionDisposition.REJECTED
    return SubmissionDisposition.POSSIBLY_SUBMITTED


def _format_rpc_error_detail(error: dict[str, Any]) -> str:
    details: list[str] = []
    message = error.get("message")
    if isinstance(message, str) and (normalized := _normalize_rpc_error_text(message)):
        details.append(normalized)
    data = error.get("data")
    revert = _decode_solidity_revert(data)
    if revert is not None and revert not in details:
        details.append(revert)
    elif data is not None and revert is None:
        # Providers can echo signed transactions or credentials in arbitrary error data.
        details.append("provider error data omitted")
    detail = "; ".join(details)
    return (
        detail
        if len(detail) <= RPC_ERROR_DETAIL_LIMIT
        else detail[: RPC_ERROR_DETAIL_LIMIT - 3] + "..."
    )


def _decode_solidity_revert(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        return None
    try:
        encoded = bytes.fromhex(value[2:])
    except ValueError:
        return None
    if encoded[:4] == bytes.fromhex("08c379a0") and len(encoded) >= 68:
        offset = int.from_bytes(encoded[4:36], "big")
        length_index = 4 + offset
        if offset % 32 == 0 and length_index + 32 <= len(encoded):
            length = int.from_bytes(encoded[length_index : length_index + 32], "big")
            start = length_index + 32
            end = start + length
            if end <= len(encoded):
                try:
                    reason = encoded[start:end].decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    normalized = _normalize_rpc_error_text(reason)
                    if normalized:
                        return f"revert reason={normalized}"
    if encoded[:4] == bytes.fromhex("4e487b71") and len(encoded) == 36:
        return f"Solidity panic=0x{int.from_bytes(encoded[4:], 'big'):x}"
    return None


def _normalize_rpc_error_text(value: str) -> str:
    redacted = _RPC_ERROR_URL.sub("[redacted URL]", value)
    redacted = _RPC_ERROR_HEX_DATA.sub("[redacted hex data]", redacted)
    return " ".join(redacted.split())


def decode_fee_inputs(block: object, priority: object) -> FeeEstimate:
    if not isinstance(block, dict) or block.get("baseFeePerGas") is None:
        if isinstance(block, dict):
            raise ChainError("RPC endpoint does not expose an EIP-1559 base fee")
        raise invalid_rpc_response()
    try:
        parse_json_rpc_quantity(block["timestamp"], 64)
        base_fee = parse_json_rpc_quantity(block["baseFeePerGas"], 256)
        priority_fee = parse_json_rpc_quantity(priority, 256)
    except (KeyError, TypeError, ValueError) as error:
        raise invalid_rpc_response() from error
    maximum = base_fee * 2 + priority_fee
    if maximum > UINT256_MAX:
        raise ChainError("RPC quantity overflowed the supported transaction fields")
    return FeeEstimate(priority_fee, maximum)


def validate_contract_calls(calls: Sequence[tuple[str, bytes]]) -> tuple[tuple[str, bytes], ...]:
    if any(not is_address(target) or not calldata for target, calldata in calls):
        raise invalid_rpc_response()
    return tuple((to_checksum_address(target), bytes(calldata)) for target, calldata in calls)


def contract_call_request(target: str, calldata: bytes) -> tuple[str, list[object]]:
    return (
        "eth_call",
        [{"to": target, "input": f"0x{calldata.hex()}"}, "latest"],
    )


def decode_transaction_receipt(
    receipt: object,
    transaction_hash: str,
) -> TransactionReceipt | None:
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        raise invalid_rpc_response()
    try:
        returned_hash = receipt["transactionHash"]
        block_hash = receipt["blockHash"]
        if block_hash is not None:
            block_hash = _parse_hash(block_hash)
    except (KeyError, TypeError, ValueError) as error:
        raise invalid_rpc_response() from error
    if not is_transaction_hash(returned_hash) or returned_hash.lower() != transaction_hash.lower():
        raise ChainError(
            "RPC returned a different transaction hash than the locally signed transaction"
        )
    # A pending or preconfirmed receipt does not yet prove inclusion in a block.
    if block_hash is None or block_hash == bytes(32):
        return None
    try:
        block_number = parse_json_rpc_quantity(receipt["blockNumber"], 64)
        status = parse_json_rpc_quantity(receipt["status"], 64)
        raw_logs = receipt["logs"]
    except (KeyError, TypeError, ValueError) as error:
        raise invalid_rpc_response() from error
    if status > 1 or not isinstance(raw_logs, list):
        raise invalid_rpc_response()
    logs: list[TransactionLog] = []
    try:
        for raw_log in raw_logs:
            if not isinstance(raw_log, dict) or not is_address(raw_log.get("address")):
                raise ValueError
            raw_topics = raw_log.get("topics")
            if not isinstance(raw_topics, list):
                raise ValueError
            logs.append(
                TransactionLog(
                    to_checksum_address(raw_log["address"]),
                    tuple(_parse_hash(topic) for topic in raw_topics),
                    parse_hex_bytes(raw_log.get("data")),
                )
            )
    except (TypeError, ValueError) as error:
        raise invalid_rpc_response() from error
    return TransactionReceipt(
        returned_hash,
        block_number,
        status == 1,
        tuple(logs),
    )


def rpc_request(request_id: int, method: str, params: list[object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def is_transaction_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", value) is not None


def is_already_known(error: ChainError) -> bool:
    if not isinstance(error, RpcResponseError):
        return False
    if error.provider_message is None:
        return False
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", error.provider_message)
    canonical_message = " ".join(re.findall(r"[a-z0-9]+", words.casefold()))
    return canonical_message in (
        "already known",
        "already known transaction",
        "known transaction",
        "transaction already exists",
        "transaction already imported",
        "transaction already in the pool",
        "transaction already known",
    )


def _parse_hash(value: object) -> bytes:
    if not is_transaction_hash(value):
        raise ValueError("invalid hash")
    assert isinstance(value, str)
    return bytes.fromhex(value[2:])


def parse_hex_bytes(value: object) -> bytes:
    if not isinstance(value, str) or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value) is None:
        raise ValueError("invalid hex bytes")
    return bytes.fromhex(value[2:])


def invalid_rpc_response() -> ChainError:
    return ChainError("RPC endpoint returned an invalid response")


def _is_rate_limit_error(code: int, message: object) -> bool:
    normalized = message.lower() if isinstance(message, str) else ""
    return code in (429, -32_005) or any(
        text in normalized for text in ("rate limit", "too many requests", "request limit")
    )
