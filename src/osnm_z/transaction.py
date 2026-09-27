"""Validation and local signing for EIP-1559 type-two transactions."""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_utils.address import to_checksum_address

from .domain import UINT256_MAX
from .signing import WalletSigner, WalletSignerError


class TransactionError(ValueError):
    """The transaction cannot be signed safely."""


@dataclass(frozen=True, slots=True)
class Eip1559Transaction:
    chain_id: int
    nonce: int
    max_priority_fee_per_gas: int
    max_fee_per_gas: int
    gas_limit: int
    target: str
    value: int
    calldata: bytes = field(repr=False)


class SignedTransaction:
    __slots__ = ("_hash", "_raw")

    def __init__(self, raw: bytes, transaction_hash: bytes) -> None:
        self._raw = raw
        self._hash = transaction_hash

    @property
    def raw(self) -> bytes:
        return self._raw

    @property
    def hash_hex(self) -> str:
        return f"0x{self._hash.hex()}"

    def __repr__(self) -> str:
        return f"SignedTransaction(hash={self.hash_hex}, encoded_bytes={len(self._raw)}, ...)"


def sign_eip1559_transaction(
    transaction: Eip1559Transaction, signer: WalletSigner
) -> SignedTransaction:
    _validate_transaction(transaction)
    payload: dict[str, object] = {
        "type": 2,
        "chainId": transaction.chain_id,
        "nonce": transaction.nonce,
        "maxPriorityFeePerGas": transaction.max_priority_fee_per_gas,
        "maxFeePerGas": transaction.max_fee_per_gas,
        "gas": transaction.gas_limit,
        "to": to_checksum_address(transaction.target),
        "value": transaction.value,
        "data": transaction.calldata,
        "accessList": [],
    }
    try:
        raw, transaction_hash = signer.sign_transaction(payload)
    except WalletSignerError as error:
        raise TransactionError(str(error)) from error
    if not raw or raw[0] != 2:
        raise TransactionError("wallet signing failed")
    return SignedTransaction(raw, transaction_hash)


def _validate_transaction(transaction: Eip1559Transaction) -> None:
    if transaction.chain_id <= 0 or not 0 <= transaction.nonce < (1 << 64) - 1:
        raise TransactionError("transaction chain ID or account nonce is invalid")
    if transaction.max_fee_per_gas < transaction.max_priority_fee_per_gas:
        raise TransactionError("EIP-1559 fee cap must cover the priority fee")
    if not 0 < transaction.gas_limit < 1 << 64:
        raise TransactionError("transaction gas limit must be a positive uint64")
    values = (
        transaction.chain_id,
        transaction.nonce,
        transaction.max_fee_per_gas,
        transaction.max_priority_fee_per_gas,
    )
    if any(value < 0 or value > UINT256_MAX for value in values):
        raise TransactionError("transaction contains an invalid unsigned integer")
