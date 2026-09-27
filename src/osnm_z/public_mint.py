"""On-chain public SeaDrop discovery, calldata encoding, and timed broadcasts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from eth_abi import encode  # type: ignore[attr-defined]
from eth_utils.address import is_address, is_same_address, to_checksum_address
from eth_utils.crypto import keccak

from . import logging
from .chain import ChainGateway
from .config import ChainConfig
from .domain import UINT256_MAX, ZERO_ADDRESS
from .opensea import WalletOpenSeaClient
from .opensea_protocol import (
    CollectionMetadata,
    MintTransactionAction,
    ProtocolError,
    StageMetadata,
)
from .rpc_protocol import BatchReadError, ChainError
from .scheduling import NANOSECONDS_PER_SECOND
from .seadrop import ERC721_PUBLIC_MINT_SELECTOR, OPENSEA_SEADROP_ADDRESS

GET_PUBLIC_DROP_SELECTOR = bytes.fromhex("bc6a629c")
GET_ALLOWED_FEE_RECIPIENTS_SELECTOR = bytes.fromhex("68632274")
GET_MINT_STATS_SELECTOR = bytes.fromhex("840e15d4")
OPENSEA_FEE_RECIPIENT = to_checksum_address("0x0000a26b00c1F0DF003000390027140000fAa719")
PUBLIC_DROP_UPDATED_TOPIC = keccak(
    text="PublicDropUpdated(address,(uint80,uint48,uint48,uint16,uint16,bool))"
)
SEADROP_DISCOVERY_ATTEMPTS = 2
SEADROP_DISCOVERY_TIMEOUT_SECONDS = 2.0


class PublicMintError(ValueError):
    """A public mint cannot be discovered, encoded, or scheduled safely."""


@dataclass(frozen=True, slots=True)
class PublicMintBroadcastPlan:
    starts_at_ns: int
    offset_ms: int


@dataclass(frozen=True, slots=True)
class PublicDropConfig:
    mint_price: int
    start_time: int
    end_time: int
    max_total_mintable_by_wallet: int
    restrict_fee_recipients: bool


@dataclass(frozen=True, slots=True)
class PublicMintContext:
    target: str
    fee_recipient: str
    config: PublicDropConfig


@dataclass(frozen=True, slots=True)
class PublicMintStats:
    minter_quantity_minted: int
    total_supply: int
    max_supply: int


def public_mint_broadcast_plan(
    config: PublicDropConfig,
    offset_ms: int,
    now_ns: int,
) -> PublicMintBroadcastPlan | None:
    """Return a timed plan only while the authoritative public drop is upcoming."""
    if now_ns < 0 or not 0 <= offset_ms <= 60_000:
        raise PublicMintError("public mint broadcast timing is invalid")
    if now_ns >= config.end_time * NANOSECONDS_PER_SECOND:
        raise PublicMintError("the on-chain public drop has ended")
    if now_ns >= config.start_time * NANOSECONDS_PER_SECOND:
        return None
    return PublicMintBroadcastPlan(config.start_time * NANOSECONDS_PER_SECOND, offset_ms)


async def resolve_public_mint_context_and_stats(
    gateway: ChainGateway,
    chain: ChainConfig,
    collection: CollectionMetadata,
    wallet: str,
    seadrop_address: str = OPENSEA_SEADROP_ADDRESS,
    now_ns: int | None = None,
) -> tuple[PublicMintContext, PublicMintStats]:
    """Resolve public configuration and one wallet's stats in one contract-read batch."""
    if not is_address(wallet) or is_same_address(wallet, ZERO_ADDRESS):
        raise PublicMintError("public mint wallet is invalid")
    stats_calldata = GET_MINT_STATS_SELECTOR + encode(["address"], [to_checksum_address(wallet)])
    context, additional = await _resolve_public_mint_data(
        gateway,
        chain,
        collection,
        seadrop_address,
        ((collection.address, stats_calldata),),
        now_ns,
    )
    stats_result = additional[0]
    if isinstance(stats_result, ChainError):
        raise stats_result
    return context, decode_public_mint_stats(stats_result)


async def discover_seadrop_address(
    client: WalletOpenSeaClient,
    gateway: ChainGateway,
    chain: ChainConfig,
    collection: CollectionMetadata,
) -> str:
    """Discover SeaDrop through indexed config transactions, or use the canonical fallback."""
    last_error: ProtocolError | ChainError | PublicMintError | None = None
    for attempt in range(1, SEADROP_DISCOVERY_ATTEMPTS + 1):
        try:
            async with asyncio.timeout(SEADROP_DISCOVERY_TIMEOUT_SECONDS):
                return await _discover_seadrop_address_once(client, gateway, chain, collection)
        except TimeoutError:
            error = PublicMintError(
                f"SeaDrop discovery did not respond within "
                f"{SEADROP_DISCOVERY_TIMEOUT_SECONDS:g} seconds"
            )
            last_error = error
        except (ProtocolError, ChainError, PublicMintError) as error:
            last_error = error
        if attempt < SEADROP_DISCOVERY_ATTEMPTS:
            logging.warn(f"SeaDrop discovery attempt {attempt} failed: {last_error}")
    assert last_error is not None
    logging.warn(f"Using the default SeaDrop contract; discovery failed: {last_error}")
    return OPENSEA_SEADROP_ADDRESS


def decode_address_array(encoded: bytes, label: str) -> tuple[str, ...]:
    if len(encoded) < 64 or len(encoded) % 32 != 0:
        raise PublicMintError(f"contract returned invalid {label} data")
    offset = int.from_bytes(encoded[:32], "big")
    count = int.from_bytes(encoded[32:64], "big")
    if offset != 32 or len(encoded) != 64 + count * 32:
        raise PublicMintError(f"contract returned invalid {label} data")
    addresses: list[str] = []
    seen: set[str] = set()
    for index in range(count):
        word = encoded[64 + index * 32 : 96 + index * 32]
        if word[:12] != bytes(12):
            raise PublicMintError(f"contract returned invalid {label} data")
        address = to_checksum_address(word[12:])
        if is_same_address(address, ZERO_ADDRESS) or address in seen:
            raise PublicMintError(f"contract returned invalid {label} data")
        seen.add(address)
        addresses.append(address)
    return tuple(addresses)


def decode_public_drop(encoded: bytes) -> PublicDropConfig:
    if len(encoded) != 6 * 32:
        raise PublicMintError("SeaDrop returned invalid public drop data")
    values = (
        _decode_uint_word(encoded, 0, 80),
        _decode_uint_word(encoded, 1, 48),
        _decode_uint_word(encoded, 2, 48),
        _decode_uint_word(encoded, 3, 16),
        _decode_uint_word(encoded, 4, 16),
        _decode_uint_word(encoded, 5, 1),
    )
    mint_price, start_time, end_time, maximum, fee_bps, restricted = values
    if end_time <= start_time or maximum == 0 or fee_bps > 10_000:
        raise PublicMintError("SeaDrop returned an inactive or invalid public drop config")
    return PublicDropConfig(
        mint_price,
        start_time,
        end_time,
        maximum,
        bool(restricted),
    )


def decode_public_mint_stats(encoded: bytes) -> PublicMintStats:
    if len(encoded) != 3 * 32:
        raise PublicMintError("NFT contract returned invalid mint stats")
    return PublicMintStats(
        _decode_uint_word(encoded, 0, 256),
        _decode_uint_word(encoded, 1, 256),
        _decode_uint_word(encoded, 2, 256),
    )


def build_public_mint_action(
    collection: CollectionMetadata,
    stage: StageMetadata,
    context: PublicMintContext,
    stats: PublicMintStats,
    wallet: str,
    quantity: int,
) -> MintTransactionAction:
    if stage.stage_type != "PUBLIC_SALE":
        raise PublicMintError("local calldata is available only for public mint stages")
    if (
        not is_address(context.target)
        or is_same_address(context.target, ZERO_ADDRESS)
        or not is_address(context.fee_recipient)
        or is_same_address(context.fee_recipient, ZERO_ADDRESS)
        or not is_address(wallet)
        or is_same_address(wallet, ZERO_ADDRESS)
        or type(quantity) is not int
        or not 0 < quantity <= UINT256_MAX
    ):
        raise PublicMintError("public mint action inputs are invalid")
    if collection.drop_kind != "Erc721SeaDropV1" or stage.stage_index != 0:
        raise PublicMintError("ERC-721 public mint stage is incompatible")
    available = context.config.max_total_mintable_by_wallet - stats.minter_quantity_minted
    if available < quantity or stats.total_supply + quantity > stats.max_supply:
        raise PublicMintError("public mint quantity exceeds current on-chain availability")
    mint_value = context.config.mint_price * quantity
    if mint_value > UINT256_MAX:
        raise PublicMintError("public mint value overflows uint256")
    values = [
        to_checksum_address(collection.address),
        to_checksum_address(context.fee_recipient),
        to_checksum_address(wallet),
        quantity,
    ]
    calldata = ERC721_PUBLIC_MINT_SELECTOR + encode(
        ["address", "address", "address", "uint256"], values
    )
    return MintTransactionAction(
        context.target,
        mint_value,
        calldata,
    )


async def _resolve_public_mint_data(
    gateway: ChainGateway,
    chain: ChainConfig,
    collection: CollectionMetadata,
    seadrop_address: str,
    additional_calls: tuple[tuple[str, bytes], ...],
    now_ns: int | None,
) -> tuple[PublicMintContext, tuple[bytes | ChainError, ...]]:
    if collection.drop_kind != "Erc721SeaDropV1":
        raise PublicMintError("collection does not use the supported ERC-721 SeaDrop contract")
    if not is_address(seadrop_address) or is_same_address(seadrop_address, ZERO_ADDRESS):
        raise PublicMintError("discovered SeaDrop contract is invalid")
    nft_argument = encode(["address"], [to_checksum_address(collection.address)])
    target = to_checksum_address(seadrop_address)
    try:
        config, encoded_recipients, additional = await _read_public_drop_configuration(
            gateway, chain, target, nft_argument, additional_calls
        )
    except BatchReadError:
        raise
    except (ChainError, PublicMintError) as error:
        if is_same_address(target, OPENSEA_SEADROP_ADDRESS):
            raise
        logging.warn(f"Trying the default SeaDrop contract; settings could not be read: {error}")
        target = OPENSEA_SEADROP_ADDRESS
        config, encoded_recipients, additional = await _read_public_drop_configuration(
            gateway, chain, target, nft_argument, additional_calls
        )
    current_utc_ns = time.time_ns() if now_ns is None else now_ns
    if current_utc_ns < 0:
        raise PublicMintError("current UTC time is invalid")
    if config.end_time * NANOSECONDS_PER_SECOND <= current_utc_ns:
        raise PublicMintError("the on-chain public drop has ended")
    fee_recipient: str = OPENSEA_FEE_RECIPIENT
    if config.restrict_fee_recipients:
        recipients = decode_address_array(encoded_recipients, "allowed fee recipient")
        if not recipients:
            raise PublicMintError("public drop restricts fee recipients but none are configured")
        fee_recipient = next(
            (
                recipient
                for recipient in recipients
                if is_same_address(recipient, OPENSEA_FEE_RECIPIENT)
            ),
            recipients[0],
        )
    return PublicMintContext(target, fee_recipient, config), additional


async def _read_public_drop_configuration(
    gateway: ChainGateway,
    chain: ChainConfig,
    target: str,
    nft_argument: bytes,
    additional_calls: tuple[tuple[str, bytes], ...],
) -> tuple[PublicDropConfig, bytes, tuple[bytes | ChainError, ...]]:
    results = await gateway.contract_reads(
        chain,
        (
            (target, GET_PUBLIC_DROP_SELECTOR + nft_argument),
            (target, GET_ALLOWED_FEE_RECIPIENTS_SELECTOR + nft_argument),
            *additional_calls,
        ),
    )
    public_drop = results[0]
    if isinstance(public_drop, ChainError):
        raise public_drop
    config = decode_public_drop(public_drop)
    recipients = results[1]
    if isinstance(recipients, ChainError):
        if config.restrict_fee_recipients:
            raise recipients
        recipients = b""
    return config, recipients, results[2:]


async def _discover_seadrop_address_once(
    client: WalletOpenSeaClient,
    gateway: ChainGateway,
    chain: ChainConfig,
    collection: CollectionMetadata,
) -> str:
    transaction_hashes = await client.config_change_transaction_hashes(collection.slug)
    if not transaction_hashes:
        raise PublicMintError("OpenSea configuration history contains no transaction hashes")
    nft_topic = bytes.fromhex(collection.address[2:]).rjust(32, b"\0")
    receipts = await gateway.transaction_receipts(chain, transaction_hashes)
    first_error: ChainError | None = None
    for receipt in receipts:
        if isinstance(receipt, ChainError):
            first_error = first_error or receipt
            continue
        if receipt is None or not receipt.is_success:
            continue
        matching_logs = [
            log
            for log in receipt.logs
            if len(log.topics) >= 2
            and log.topics[0] == PUBLIC_DROP_UPDATED_TOPIC
            and log.topics[1] == nft_topic
        ]
        if matching_logs:
            target = matching_logs[-1].address
            logging.success(f"Public mint contract resolved: {target}.")
            return target
    if first_error is not None:
        raise first_error
    raise PublicMintError("SeaDrop was absent from OpenSea configuration receipts")


def _decode_uint_word(encoded: bytes, index: int, bits: int) -> int:
    word = encoded[index * 32 : (index + 1) * 32]
    value = int.from_bytes(word, "big")
    if len(word) != 32 or value >= 1 << bits:
        raise PublicMintError("contract returned an out-of-range public mint value")
    return value
