"""OpenSea response models, decoding, and protocol error classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import (
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import orjson
from eth_utils.address import is_address, is_same_address, to_checksum_address

from .domain import UINT256_MAX, ZERO_ADDRESS
from .scheduling import NANOSECONDS_PER_SECOND, parse_rfc3339_ns

SIWE_STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy)."
)
_SLUG = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_TRANSACTION_HASH = re.compile(r"0x[0-9A-Fa-f]{64}\Z")


class ErrorKind(Enum):
    CLIENT = "client"
    INVALID_COLLECTION_LOCATOR = "invalid_collection_locator"
    AMBIGUOUS_COLLECTION_LOCATOR = "ambiguous_collection_locator"
    INVALID_NONCE_RESPONSE = "invalid_nonce_response"
    AUTHENTICATION = "authentication"
    AUTHENTICATION_SESSION_MISMATCH = "authentication_session_mismatch"
    TRANSPORT = "transport"
    HTTP = "http"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_CANNOT_TRADE = "account_cannot_trade"
    AUTHENTICATION_REQUIRED = "authentication_required"
    COMPATIBILITY = "compatibility"
    COLLECTION_NOT_FOUND = "collection_not_found"
    COLLECTION_CHAIN_MISMATCH = "collection_chain_mismatch"
    DROP_NOT_FOUND = "drop_not_found"
    INVALID_PROTOCOL_VALUE = "invalid_protocol_value"
    SESSION_REQUIRED = "session_required"
    MINT_STAGE_NOT_OPEN = "mint_stage_not_open"
    MINT_WALLET_INELIGIBLE = "mint_wallet_ineligible"
    MINT_LIMIT_EXCEEDED = "mint_limit_exceeded"
    MINT_INSUFFICIENT_FUNDS = "mint_insufficient_funds"
    MINT_ACTION_REJECTED = "mint_action_rejected"
    SIGNING = "signing"


_ERROR_MESSAGES = {
    ErrorKind.CLIENT: "could not initialize the OpenSea client",
    ErrorKind.INVALID_COLLECTION_LOCATOR: "enter a valid OpenSea slug, URL, or contract address",
    ErrorKind.AMBIGUOUS_COLLECTION_LOCATOR: (
        "the contract address matches more than one OpenSea collection on the RPC chain"
    ),
    ErrorKind.INVALID_NONCE_RESPONSE: "OpenSea returned an invalid sign-in challenge",
    ErrorKind.AUTHENTICATION_SESSION_MISMATCH: (
        "OpenSea sign-in did not match the configured wallet"
    ),
    ErrorKind.TRANSPORT: "OpenSea request failed or timed out",
    ErrorKind.RATE_LIMITED: "OpenSea is rate limiting requests",
    ErrorKind.ACCOUNT_CANNOT_TRADE: ("OpenSea rejected this account for trading operations"),
    ErrorKind.AUTHENTICATION_REQUIRED: (
        "OpenSea did not recognize the authenticated wallet session"
    ),
    ErrorKind.COMPATIBILITY: "OpenSea returned a response this tool could not read",
    ErrorKind.COLLECTION_NOT_FOUND: "OpenSea collection was not found",
    ErrorKind.COLLECTION_CHAIN_MISMATCH: (
        "the OpenSea collection and RPC endpoint are connected to different chains"
    ),
    ErrorKind.DROP_NOT_FOUND: "the collection is not an OpenSea-hosted SeaDrop",
    ErrorKind.INVALID_PROTOCOL_VALUE: (
        "OpenSea returned an invalid address, integer, or calldata field"
    ),
    ErrorKind.SESSION_REQUIRED: "sign in to OpenSea before requesting wallet-specific mint data",
    ErrorKind.MINT_STAGE_NOT_OPEN: (
        "OpenSea reported that the drop is not currently accepting mints"
    ),
    ErrorKind.MINT_WALLET_INELIGIBLE: (
        "OpenSea reported that the wallet is ineligible for the active mint stage"
    ),
    ErrorKind.MINT_LIMIT_EXCEEDED: (
        "OpenSea reported that the requested mint exceeds an allocation or supply limit"
    ),
    ErrorKind.MINT_INSUFFICIENT_FUNDS: (
        "OpenSea reported insufficient wallet funds for the mint action"
    ),
    ErrorKind.MINT_ACTION_REJECTED: "OpenSea rejected the mint action",
    ErrorKind.SIGNING: "wallet signing failed",
}


class ProtocolError(RuntimeError):
    """An OpenSea request, authentication, or response-decoding operation failed."""

    __slots__ = ("kind", "status")

    def __init__(self, kind: ErrorKind, status: int | None = None) -> None:
        self.kind = kind
        self.status = status
        if kind is ErrorKind.AUTHENTICATION:
            message = f"OpenSea sign-in failed with HTTP status {status}"
        elif kind is ErrorKind.HTTP:
            message = f"OpenSea returned HTTP status {status}"
        else:
            message = _ERROR_MESSAGES[kind]
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StageMetadata:
    kind: str
    label: str | None
    stage_type: str
    stage_index: int
    start_time: str | None
    end_time: str | None
    max_total_mintable_by_wallet: int | None
    native_price_wei: int | None = None
    price_chain_identifier: str | None = None
    uuid: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionMetadata:
    slug: str
    address: str
    chain_identifier: str
    network_id: int
    drop_kind: str
    drop_address: str
    stages: tuple[StageMetadata, ...]
    is_disabled: bool = False
    is_minted_out: bool = False


class EligibleMinterRelation(Enum):
    ACTIVE_WALLET = "active_wallet"
    LINKED_WALLET = "linked_wallet"


@dataclass(frozen=True, slots=True)
class StageEligibility:
    kind: str
    stage_type: str
    stage_index: int
    is_eligible: bool | None
    eligible_minter_relation: EligibleMinterRelation | None
    max_total_mintable_by_wallet: int | None
    eligible_max_total_mintable_by_wallet: int | None
    eligible_native_price_wei: int | None = None
    eligible_price_chain_identifier: str | None = None
    uuid: str | None = None


@dataclass(frozen=True, slots=True)
class EligibilitySnapshot:
    drop_kind: str
    minter_quantity_minted: int | None
    stages: tuple[StageEligibility, ...]


@dataclass(frozen=True, slots=True)
class MintTransactionAction:
    target: str
    value: int
    calldata: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class CollectionLocator:
    slug: str | None = None
    contract: str | None = None


def effective_native_mint_price(
    stage: StageMetadata, eligibility: StageEligibility
) -> tuple[int, str | None]:
    """Apply OpenSea's eligible-price override, then stage-price, then free-mint fallback."""
    if eligibility.eligible_native_price_wei is not None:
        return (
            eligibility.eligible_native_price_wei,
            eligibility.eligible_price_chain_identifier,
        )
    if stage.native_price_wei is not None:
        return stage.native_price_wei, stage.price_chain_identifier
    return 0, None


def parse_collection_locator(locator: str) -> CollectionLocator:
    value = locator.strip()
    if value.startswith("0x") and len(value) == 42:
        if not is_address(value):
            raise ProtocolError(ErrorKind.INVALID_COLLECTION_LOCATOR)
        return CollectionLocator(contract=to_checksum_address(value))
    try:
        validate_slug(value)
    except ProtocolError:
        pass
    else:
        return CollectionLocator(slug=value)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "opensea.io":
        raise ProtocolError(ErrorKind.INVALID_COLLECTION_LOCATOR)
    segments = parsed.path.lstrip("/").split("/")
    if len(segments) < 2 or segments[0] != "collection":
        raise ProtocolError(ErrorKind.INVALID_COLLECTION_LOCATOR)
    validate_slug(segments[1])
    return CollectionLocator(slug=segments[1])


def validate_slug(slug: str) -> None:
    if _SLUG.fullmatch(slug) is None:
        raise ProtocolError(ErrorKind.INVALID_COLLECTION_LOCATOR)


def create_siwe_message(
    domain: str,
    address: str,
    uri: str,
    chain_id: int,
    nonce: str,
    issued_at: str,
) -> str:
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
        f"{SIWE_STATEMENT}\n\nURI: {uri}\nVersion: 1\nChain ID: {chain_id}\n"
        f"Nonce: {nonce}\nIssued At: {issued_at}"
    )


def classify_graphql_errors(errors: list[object]) -> ProtocolError:
    for value in errors:
        if not isinstance(value, dict) or not isinstance(value.get("message"), str):
            continue
        message = value["message"].lower()
        extensions = value.get("extensions")
        codes: set[str] = set()
        if isinstance(extensions, dict):
            for key in ("code", "errorCode", "errorType", "classification"):
                code = extensions.get(key)
                if isinstance(code, str):
                    codes.add(code.upper())
        if codes & {"RATE_LIMITED", "TOO_MANY_REQUESTS"}:
            return ProtocolError(ErrorKind.RATE_LIMITED)
        if codes & {"UNAUTHENTICATED", "AUTHENTICATION_REQUIRED", "NOT_AUTHENTICATED"}:
            return ProtocolError(ErrorKind.AUTHENTICATION_REQUIRED)
        if "can not perform trading operations" in message or (
            "cannot perform trading operations" in message
        ):
            return ProtocolError(ErrorKind.ACCOUNT_CANNOT_TRADE)
        if "not authenticated" in message or "authentication" in message:
            return ProtocolError(ErrorKind.AUTHENTICATION_REQUIRED)
        if "rate limit" in message or "too many requests" in message:
            return ProtocolError(ErrorKind.RATE_LIMITED)
    return ProtocolError(ErrorKind.COMPATIBILITY)


def should_retry_without_persisted_query(envelope: dict[str, Any]) -> bool:
    errors = envelope.get("errors", [])
    if not isinstance(errors, list):
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    for value in errors:
        if not isinstance(value, dict):
            continue
        message = value.get("message")
        if message in {
            "PersistedQueryNotFound",
            "PersistedQueryIdInvalid",
            "PersistedQueryNotSupported",
        }:
            return True
        extensions = value.get("extensions")
        if isinstance(extensions, dict):
            if extensions.get("code") in {
                "PERSISTED_QUERY_NOT_FOUND",
                "PERSISTED_QUERY_NOT_SUPPORTED",
            }:
                return True
    return False


def graphql_data(envelope: dict[str, Any]) -> Any:
    _validate_graphql_auth_extension(envelope)
    errors = envelope.get("errors", [])
    if not isinstance(errors, list):
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    if errors:
        raise classify_graphql_errors(errors)
    if "data" not in envelope or envelope["data"] is None:
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    return envelope["data"]


def _validate_graphql_auth_extension(envelope: dict[str, Any]) -> None:
    extensions = envelope.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, dict):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        auth = extensions.get("auth")
        if auth is not None:
            if not isinstance(auth, dict):
                raise ProtocolError(ErrorKind.COMPATIBILITY)
            auth_error = auth.get("error")
            if auth_error is not None:
                raise ProtocolError(ErrorKind.AUTHENTICATION_REQUIRED)


def decode_collection(data: object, requested_slug: str) -> CollectionMetadata:
    try:
        root = _object(data, {"collectionBySlug"})
        collection_value = root["collectionBySlug"]
        if collection_value is None:
            raise ProtocolError(ErrorKind.COLLECTION_NOT_FOUND)
        collection = _object(
            collection_value,
            {"__typename", "slug", "address", "chain"},
        )
        if collection["__typename"] != "Collection":
            raise ProtocolError(ErrorKind.COLLECTION_NOT_FOUND)
        slug = _string(collection.get("slug"))
        validate_slug(slug)
        if slug != requested_slug:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        address = _address(collection.get("address"))
        chain = _decode_chain(collection.get("chain"), network_required=True)
        drop_value = root.get("dropBySlug")
        if drop_value is None:
            raise ProtocolError(ErrorKind.DROP_NOT_FOUND)
        drop = _object(
            drop_value,
            {"__typename", "identifier", "stages"},
        )
        drop_kind = _string(drop["__typename"])
        if drop_kind != "Erc721SeaDropV1":
            raise ProtocolError(ErrorKind.DROP_NOT_FOUND)
        identifier = _object(
            drop.get("identifier"),
            {"contractAddress", "chain"},
        )
        identifier_chain = _decode_chain(identifier["chain"], network_required=False)
        if identifier_chain[0] != chain[0]:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        drop_address = _address(identifier["contractAddress"])
        if is_same_address(address, ZERO_ADDRESS) or not is_same_address(drop_address, address):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        stage_values = drop["stages"]
        if not isinstance(stage_values, list):
            raise TypeError
        stages = tuple(_decode_metadata_stage(item, drop_kind) for item in stage_values)
        if len({stage.stage_index for stage in stages}) != len(stages):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        uuids = [stage.uuid for stage in stages if stage.uuid is not None]
        if len(set(uuids)) != len(uuids):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        is_disabled, is_minted_out = _decode_drop_availability(drop)
        return CollectionMetadata(
            slug=slug,
            address=address,
            chain_identifier=chain[0],
            network_id=chain[1],
            drop_kind=drop_kind,
            drop_address=drop_address,
            stages=stages,
            is_disabled=is_disabled,
            is_minted_out=is_minted_out,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error


def _decode_drop_availability(drop: dict[str, Any]) -> tuple[bool, bool]:
    if not {"disabledReason", "maxSupply", "totalSupply"}.issubset(drop):
        raise TypeError("missing drop availability fields")
    disabled_reason = drop["disabledReason"]
    if disabled_reason is not None and not isinstance(disabled_reason, str):
        raise TypeError("invalid disabled reason")
    total_supply = _uint(drop["totalSupply"], 256)
    max_supply = _uint(drop["maxSupply"], 256)
    return bool(disabled_reason), total_supply >= max_supply


def _decode_metadata_stage(value: object, drop_kind: str) -> StageMetadata:
    required = {
        "__typename",
        "label",
        "stageType",
        "stageIndex",
        "startTime",
        "endTime",
        "maxTotalMintableByWallet",
    }
    stage = _object(value, required)
    kind = _string(stage["__typename"])
    label = _optional_string(stage["label"])
    stage_type = _string(stage["stageType"])
    validate_stage_identity(drop_kind, kind, stage_type)
    stage_index = _uint(stage["stageIndex"], 32)
    start_time = _optional_string(stage.get("startTime"))
    end_time = _optional_string(stage.get("endTime"))
    validate_stage_time_window(start_time, end_time)
    max_total = _optional_uint(stage.get("maxTotalMintableByWallet"), 64)
    native_price, price_chain = _decode_native_price(stage.get("price"))
    return StageMetadata(
        kind,
        label,
        stage_type,
        stage_index,
        start_time,
        end_time,
        max_total,
        native_price,
        price_chain,
        _optional_stage_uuid(stage.get("uuid")),
    )


def decode_config_change_transaction_hashes(data: object) -> tuple[str, ...]:
    """Decode and order transaction hashes from OpenSea's drop configuration history."""
    try:
        root = _object(data, {"dropBySlug"})
        drop_value = root["dropBySlug"]
        if drop_value is None:
            raise ProtocolError(ErrorKind.DROP_NOT_FOUND)
        drop = _object(drop_value, {"configChangeHistory"})
        history_value = drop["configChangeHistory"]
        if history_value is None:
            return ()
        history = _object(history_value, {"items"})
        items = history["items"]
        if not isinstance(items, list):
            raise TypeError
        changes: list[tuple[int, int, str]] = []
        for position, value in enumerate(items):
            item = _object(value, {"timestamp", "transactionHash"})
            timestamp = parse_stage_time(_string(item["timestamp"]))
            transaction_hash = _optional_string(item["transactionHash"])
            if transaction_hash is None:
                continue
            if _TRANSACTION_HASH.fullmatch(transaction_hash) is None:
                raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
            changes.append((timestamp, -position, transaction_hash.lower()))
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error
    changes.sort(reverse=True)
    unique: list[str] = []
    seen: set[str] = set()
    for _, _, transaction_hash in changes:
        if transaction_hash not in seen:
            seen.add(transaction_hash)
            unique.append(transaction_hash)
    return tuple(unique)


def decode_eligibility(data: object, wallet: str) -> EligibilitySnapshot:
    try:
        root = _object(data, {"dropBySlug"})
        drop_value = root["dropBySlug"]
        if drop_value is None:
            raise ProtocolError(ErrorKind.DROP_NOT_FOUND)
        drop = _object(drop_value, {"__typename", "stages"})
        drop_kind = _string(drop["__typename"])
        if drop_kind != "Erc721SeaDropV1":
            raise ProtocolError(ErrorKind.DROP_NOT_FOUND)
        if "minterQuantityMinted" not in drop:
            raise TypeError("missing ERC-721 minted quantity")
        stage_values = drop["stages"]
        if not isinstance(stage_values, list):
            raise TypeError
        account_wallet = _decode_account_wallet(drop.get("accountStageEligibility"), wallet)
        minted = _optional_uint(drop["minterQuantityMinted"], 64)
        wallet_stages = None
        if account_wallet is not None:
            account_minted, wallet_stages = account_wallet
            if account_minted is not None:
                minted = account_minted
        stages = tuple(
            _decode_eligibility_stage(item, drop_kind, wallet, wallet_stages)
            for item in stage_values
        )
        if len({stage.stage_index for stage in stages}) != len(stages):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        uuids = [stage.uuid for stage in stages if stage.uuid is not None]
        if len(set(uuids)) != len(uuids):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        return EligibilitySnapshot(drop_kind, minted, stages)
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error


def _decode_account_wallet(
    value: object, wallet: str
) -> tuple[int | None, dict[str, dict[str, Any]]] | None:
    """Select this wallet's counters and UUID-keyed stages, never an account-wide sum."""
    if value is None:
        return None
    wallets = _object(value, {"wallets"})["wallets"]
    if not isinstance(wallets, list):
        raise TypeError("expected account wallets")
    matching = [
        item
        for item in wallets
        if is_same_address(_address(_object(item, {"address"})["address"]), wallet)
    ]
    if len(matching) != 1:
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    selected = _object(matching[0], {"quantityMinted", "stages"})
    raw_stages = selected["stages"]
    if not isinstance(raw_stages, list):
        raise TypeError("expected wallet stages")
    stages: dict[str, dict[str, Any]] = {}
    for item in raw_stages:
        stage = _object(item, {"dropStageUuid", "isEligible", "maxTotalMintableByWallet"})
        uuid = _optional_stage_uuid(stage["dropStageUuid"])
        if uuid is None or uuid in stages:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        stages[uuid] = stage
    return _optional_uint(selected["quantityMinted"], 64), stages


def _decode_eligibility_stage(
    value: object,
    drop_kind: str,
    wallet: str,
    wallet_stages: dict[str, dict[str, Any]] | None = None,
) -> StageEligibility:
    required = {
        "__typename",
        "stageType",
        "stageIndex",
        "isEligible",
        "eligibleMinterAddress",
        "maxTotalMintableByWallet",
        "eligibleMaxTotalMintableByWallet",
    }
    stage = _object(value, required)
    uuid = _optional_stage_uuid(stage.get("uuid"))
    if wallet_stages is not None:
        if uuid is None or uuid not in wallet_stages:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        wallet_stage = wallet_stages[uuid]
        stage = {
            **stage,
            "isEligible": wallet_stage["isEligible"],
            "eligibleMinterAddress": wallet,
            "eligibleMaxTotalMintableByWallet": wallet_stage["maxTotalMintableByWallet"],
            "eligiblePrice": wallet_stage.get("price"),
        }
    kind = _string(stage["__typename"])
    stage_type = _string(stage["stageType"])
    validate_stage_identity(drop_kind, kind, stage_type)
    eligible_address = stage.get("eligibleMinterAddress")
    relation = None
    if eligible_address is not None:
        parsed_address = _address(eligible_address)
        relation = (
            EligibleMinterRelation.ACTIVE_WALLET
            if is_same_address(parsed_address, wallet)
            else EligibleMinterRelation.LINKED_WALLET
        )
    is_eligible_value = stage.get("isEligible")
    if is_eligible_value is not None and type(is_eligible_value) is not bool:
        raise TypeError
    native_price, price_chain = _decode_native_price(stage.get("eligiblePrice"))
    return StageEligibility(
        kind=kind,
        stage_type=stage_type,
        stage_index=_uint(stage["stageIndex"], 32),
        is_eligible=is_eligible_value,
        eligible_minter_relation=relation,
        max_total_mintable_by_wallet=_optional_uint(stage.get("maxTotalMintableByWallet"), 64),
        eligible_max_total_mintable_by_wallet=_optional_uint(
            stage.get("eligibleMaxTotalMintableByWallet"), 64
        ),
        eligible_native_price_wei=native_price,
        eligible_price_chain_identifier=price_chain,
        uuid=uuid,
    )


def _optional_stage_uuid(value: object) -> str | None:
    uuid = _optional_string(value)
    if uuid is not None and (not uuid or uuid != uuid.strip()):
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    return uuid


def _decode_native_price(value: object) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict) or "token" not in value:
        return None, None
    price = _object(value, {"token"})
    token = _object(price["token"], {"unit", "symbol", "contractAddress", "chain"})
    if (
        not isinstance(token["symbol"], str)
        or not token["symbol"].strip()
        or not is_same_address(_address(token["contractAddress"]), ZERO_ADDRESS)
    ):
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    chain = _object(token["chain"], {"identifier"})
    identifier = _string(chain["identifier"])
    if not identifier.strip():
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    unit = token["unit"]
    if type(unit) not in {Decimal, int, float}:
        raise ProtocolError(ErrorKind.COMPATIBILITY)
    try:
        decimal = unit if isinstance(unit, Decimal) else Decimal(str(unit))
        if not decimal.is_finite() or decimal < 0:
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
        # A uint256 needs up to 78 digits; fractional wei must never round into validity.
        with localcontext(Context(prec=78, traps=[InvalidOperation, Inexact, Overflow])):
            wei = (decimal * Decimal(10**18)).to_integral_exact()
        if wei > UINT256_MAX:
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
        amount = int(wei)
    except (DecimalException, ValueError) as error:
        raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE) from error
    return amount, identifier


def _reject_nonstandard_json_number(value: str) -> object:
    raise ValueError(f"non-standard JSON number {value}")


def decode_mint_action(data: object) -> MintTransactionAction:
    """Extract the sole transaction without applying local mint policy to its payload."""
    try:
        root = _object(data, {"swap"})
        swap_value = root["swap"]
        if swap_value is None:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        swap = _object(swap_value, {"actions", "errors"})
        actions = swap["actions"]
        errors = swap["errors"]
        if not isinstance(actions, list) or not isinstance(errors, list):
            raise TypeError
        transactions: list[MintTransactionAction] = []
        for value in actions:
            action = _object(value, {"__typename"})
            _string(action["__typename"])
            transaction = action.get("transactionSubmissionData")
            if transaction is not None:
                transactions.append(decode_transaction(transaction))
        error_types: list[str] = []
        for value in errors:
            error = _object(value, {"__typename"})
            error_types.append(_string(error["__typename"]))
        if error_types:
            raise classify_mint_action_errors(tuple(error_types))
        if len(transactions) != 1:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        return transactions[0]
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error


def classify_mint_action_errors(error_types: tuple[str, ...]) -> ProtocolError:
    """Classify OpenSea's reported mint failures for the launch retry policy."""
    values = set(error_types)
    if "MinterNotEligibleForActiveDropStageError" in values:
        return ProtocolError(ErrorKind.MINT_WALLET_INELIGIBLE)
    if values & {
        "MintQuantityMoreThanAllocatedForMinterError",
        "InsufficientMintsRemainingError",
        "UnableToFulfillQuantityError",
    }:
        return ProtocolError(ErrorKind.MINT_LIMIT_EXCEEDED)
    if "InsufficientFundError" in values:
        return ProtocolError(ErrorKind.MINT_INSUFFICIENT_FUNDS)
    if "TradingDisabledError" in values:
        return ProtocolError(ErrorKind.ACCOUNT_CANNOT_TRADE)
    if "DropNotFoundError" in values:
        return ProtocolError(ErrorKind.DROP_NOT_FOUND)
    if "DropNotMintingError" in values:
        return ProtocolError(ErrorKind.MINT_STAGE_NOT_OPEN)
    return ProtocolError(ErrorKind.MINT_ACTION_REJECTED)


def decode_transaction(value: object) -> MintTransactionAction:
    """Parse fields required by the signer without checking their mint semantics."""
    try:
        transaction = _object(value, {"to", "data", "value"})
        target = _address(transaction["to"])
        data = _string(transaction["data"])
        if re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", data) is None:
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
        try:
            calldata = bytes.fromhex(data[2:])
        except ValueError as error:
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE) from error
        scalar_value = transaction["value"]
        if scalar_value is None:
            scalar_value = "0"
        value_number = parse_u256(_scalar_string(scalar_value))
        return MintTransactionAction(target, value_number, calldata)
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE) from error


def validate_authentication_response(response_body: bytes, wallet: str) -> None:
    try:
        response = orjson.loads(response_body)
        if not isinstance(response, dict):
            raise TypeError
        user = response["user"]
        if not isinstance(user, dict):
            raise TypeError
        authenticated_address = _address(user["address"])
    except (orjson.JSONDecodeError, KeyError, TypeError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error
    if not is_same_address(authenticated_address, wallet):
        raise ProtocolError(ErrorKind.AUTHENTICATION_SESSION_MISMATCH)


def matching_collection_slug(data: object, address: str, expected_chain_id: int) -> str:
    try:
        root = _object(data, {"collectionsByQuery"})
        results = root["collectionsByQuery"]
        if not isinstance(results, list):
            raise TypeError
        matching: set[str] = set()
        for value in results:
            result = _object(value, {"__typename"})
            result_address = result.get("address")
            if result_address is None:
                continue
            if result["__typename"] != "Collection" or not is_same_address(
                _address(result_address), address
            ):
                continue
            chain = _decode_chain(result.get("chain"), network_required=True)
            if chain[1] != expected_chain_id:
                continue
            slug = _string(result.get("slug"))
            validate_slug(slug)
            matching.add(slug)
        if len(matching) > 1:
            raise ProtocolError(ErrorKind.AMBIGUOUS_COLLECTION_LOCATOR)
        if not matching:
            raise ProtocolError(ErrorKind.COLLECTION_NOT_FOUND)
        return matching.pop()
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error


def parse_u256(value: str) -> int:
    try:
        if value.startswith("0x"):
            if re.fullmatch(r"[0-9a-fA-F]+", value[2:]) is None:
                raise ValueError
            result = int(value[2:], 16)
        else:
            if not value or not value.isascii() or not value.isdigit():
                raise ValueError
            result = int(value, 10)
    except ValueError as error:
        raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE) from error
    if result > UINT256_MAX:
        raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
    return result


def validate_stage_identity(drop_kind: str, stage_kind: str, stage_type: str) -> None:
    expected = "Erc721SeaDropV1Stage" if drop_kind == "Erc721SeaDropV1" else None
    if (
        expected is None
        or stage_kind != expected
        or stage_type
        not in (
            "PUBLIC_SALE",
            "SIGNED_PRESALE",
            "MERKLE_PRESALE",
        )
    ):
        raise ProtocolError(ErrorKind.COMPATIBILITY)


def validate_stage_time_window(start_time: str | None, end_time: str | None) -> None:
    starts_at = parse_stage_time(start_time) if start_time is not None else 0
    ends_at = parse_stage_time(end_time) if end_time is not None else None
    if ends_at is not None and ends_at <= starts_at:
        raise ProtocolError(ErrorKind.COMPATIBILITY)


def parse_stage_time(value: str) -> int:
    try:
        return parse_rfc3339_ns(value) // NANOSECONDS_PER_SECOND
    except (OverflowError, ValueError) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error


def is_retryable_request_error(error: ProtocolError) -> bool:
    """Return whether a complete OpenSea request may be retried safely."""
    if error.kind in {ErrorKind.TRANSPORT, ErrorKind.RATE_LIMITED}:
        return True
    return (
        error.kind in {ErrorKind.HTTP, ErrorKind.AUTHENTICATION}
        and error.status is not None
        and is_transient_http_status(error.status)
    )


def is_transient_http_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def _object(value: object, required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected object")
    if not required.issubset(value):
        raise TypeError("incompatible object shape")
    return value


def _decode_chain(value: object, *, network_required: bool) -> tuple[str, int]:
    required = {"identifier", "networkId"} if network_required else {"identifier"}
    chain = _object(value, required)
    identifier = _string(chain["identifier"])
    if not network_required:
        return identifier, 0
    return identifier, _network_id(chain["networkId"])


def _network_id(value: object) -> int:
    if type(value) is int:
        return _uint(value, 64)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
        if result < 1 << 64:
            return result
    raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)


def _scalar_string(value: object) -> str:
    if isinstance(value, str):
        return value
    if type(value) is int and 0 <= value < 1 << 64:
        return str(value)
    raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)


def _address(value: object) -> str:
    if not isinstance(value, str) or not is_address(value):
        raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
    return to_checksum_address(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _uint(value: object, bits: int) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        raise ValueError("expected unsigned integer")
    if result < 0 or result >= 1 << bits:
        raise ValueError("expected unsigned integer")
    return result


def _optional_uint(value: object, bits: int) -> int | None:
    return None if value is None else _uint(value, bits)


def decode_nonce_response(body: bytes) -> str:
    """Accept only the bounded alphanumeric nonce required by SIWE."""
    try:
        decoded = _object(orjson.loads(body), {"nonce"})
        nonce = _string(decoded["nonce"])
    except (orjson.JSONDecodeError, TypeError, KeyError) as error:
        raise ProtocolError(ErrorKind.INVALID_NONCE_RESPONSE) from error
    if not 8 <= len(nonce) <= 256 or not nonce.isascii() or not nonce.isalnum():
        raise ProtocolError(ErrorKind.INVALID_NONCE_RESPONSE)
    return nonce


def decode_graphql_envelope(body: bytes) -> dict[str, Any]:
    """Retain exact numeric prices when decoding OpenSea responses."""
    try:
        decoded = json.loads(
            body, parse_float=Decimal, parse_constant=_reject_nonstandard_json_number
        )
        return _object(decoded, set())
    except (ValueError, TypeError, DecimalException) as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error
