"""One local Ethereum wallet with redacted private material."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress


class WalletSignerError(ValueError):
    """Private-key construction or signing failed."""


@dataclass(frozen=True, slots=True)
class WalletIdentity:
    address: ChecksumAddress


class WalletSigner:
    """A single signer that never exposes its private key through repr."""

    __slots__ = ("_account", "_identity")

    def __init__(self, account: LocalAccount) -> None:
        self._account = account
        self._identity = WalletIdentity(account.address)

    @classmethod
    def from_private_key(cls, value: str) -> WalletSigner:
        key_text = value[2:] if value.startswith("0x") else value
        try:
            if len(key_text) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key_text):
                raise ValueError
            key_bytes = bytes.fromhex(key_text)
            return cls(Account.from_key(key_bytes))
        except (TypeError, ValueError, IndexError) as error:
            raise WalletSignerError("private key is invalid") from error

    @property
    def identity(self) -> WalletIdentity:
        return self._identity

    def sign_personal_message(self, message: bytes) -> str:
        try:
            signed = self._account.sign_message(encode_defunct(primitive=message))
        except (TypeError, ValueError) as error:
            raise WalletSignerError("wallet signing failed") from error
        return f"0x{signed.signature.hex()}"

    def sign_transaction(self, transaction: dict[str, Any]) -> tuple[bytes, bytes]:
        try:
            signed = self._account.sign_transaction(transaction)
        except (TypeError, ValueError) as error:
            raise WalletSignerError("wallet signing failed") from error
        return bytes(signed.raw_transaction), bytes(signed.hash)

    def __repr__(self) -> str:
        return f"WalletSigner(identity={self._identity!r}, ...)"
