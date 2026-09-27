"""Verify that a receipt proves the requested ERC-721 mint."""

from __future__ import annotations

from eth_utils.crypto import keccak

from .domain import ZERO_ADDRESS
from .rpc_protocol import TransactionReceipt


class NftError(ValueError):
    """A receipt cannot prove the exact requested NFT mint."""


def extract_minted_assets(
    receipt: TransactionReceipt,
    nft_contract: str,
    wallet: str,
    expected_units: int,
) -> list[int]:
    erc721 = keccak(text="Transfer(address,address,uint256)")
    assets: list[int] = []
    for log in receipt.logs:
        if log.address.lower() != nft_contract.lower() or not log.topics:
            continue
        if log.topics[0] == erc721:
            if (
                len(log.topics) != 4
                or _topic_address(log.topics[1]) != ZERO_ADDRESS
                or _topic_address(log.topics[2]).lower() != wallet.lower()
                or log.data
            ):
                continue
            if len(log.topics[3]) != 32:
                raise NftError("mint receipt contained a malformed token ID")
            assets.append(int.from_bytes(log.topics[3], "big"))
        if len(assets) > expected_units:
            raise NftError("mint receipt contained more NFT units than requested")

    if not assets or len(assets) != expected_units or len(set(assets)) != len(assets):
        raise NftError("mint receipt did not contain the exact requested NFT units")
    return assets


def _topic_address(topic: bytes) -> str:
    if len(topic) != 32 or topic[:12] != bytes(12):
        raise NftError("mint receipt contained malformed NFT transfer data")
    return "0x" + topic[12:].hex()
