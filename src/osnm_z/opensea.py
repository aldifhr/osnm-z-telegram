"""OpenSea HTTP sessions, authentication, and bounded GraphQL requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import orjson
from eth_utils.address import is_address, is_same_address, to_checksum_address

from . import __version__, logging
from .config import (
    APP_ID,
    DEFAULT_OPENSEA_ATTEMPTS,
    GRAPHQL_URL,
    HTTP_KEEPALIVE_EXPIRY_SECONDS,
    SITE_URL,
    OpenSeaConfig,
)
from .domain import ZERO_ADDRESS
from .opensea_protocol import (
    SIWE_STATEMENT,
    CollectionLocator,
    CollectionMetadata,
    EligibilitySnapshot,
    ErrorKind,
    MintTransactionAction,
    ProtocolError,
    create_siwe_message,
    decode_collection,
    decode_config_change_transaction_hashes,
    decode_eligibility,
    decode_graphql_envelope,
    decode_mint_action,
    decode_nonce_response,
    graphql_data,
    is_retryable_request_error,
    matching_collection_slug,
    should_retry_without_persisted_query,
    validate_authentication_response,
    validate_slug,
)
from .signing import WalletSigner, WalletSignerError

CONNECTED_ACCOUNT_HINT_COOKIE = "connected-account-server-hint"
ELIGIBILITY_PERSISTED_QUERY_HASH = (
    "b847627a1d94d694a9e69fd97f94ddccff0d4fb0f4936d5037fe608e000614f6"
)
NONCE_RESPONSE_LIMIT = 4 * 1024
AUTH_RESPONSE_LIMIT = 64 * 1024
GRAPHQL_RESPONSE_LIMIT = 2 * 1024 * 1024
COLLECTION_QUERY = """
query MintCollectionMetadata($slug: String!) {
  collectionBySlug(slug: $slug) {
    __typename
    ... on Collection {
      slug
      address
      chain { identifier networkId }
    }
  }
  dropBySlug(slug: $slug) {
    __typename
    disabledReason
    identifier { contractAddress chain { identifier } }
    ... on Erc721SeaDropV1 {
      maxSupply
      totalSupply
    }
    stages {
      __typename
      uuid
      label
      stageType
      stageIndex
      startTime
      endTime
      maxTotalMintableByWallet
      price {
        token { unit symbol contractAddress chain { identifier } }
      }
    }
  }
}
"""
COLLECTION_SEARCH_QUERY = """
query MintCollectionSearch($query: String!) {
  collectionsByQuery(query: $query, limit: 50) {
    __typename
    slug
    address
    chain { identifier networkId }
  }
}
"""
ELIGIBILITY_QUERY = """
query DropEligibilityQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    __typename
    ... on Erc721SeaDropV1 { minterQuantityMinted(minter: $address) }
    accountStageEligibility {
      wallets {
        address
        quantityMinted
        stages {
          dropStageUuid
          isEligible
          maxTotalMintableByWallet
          price {
            token { unit symbol contractAddress chain { identifier } }
          }
        }
      }
    }
    stages {
      __typename
      uuid
      stageType
      stageIndex
      isEligible
      eligibleMinterAddress
      maxTotalMintableByWallet
      eligibleMaxTotalMintableByWallet
      eligiblePrice {
        token { unit symbol contractAddress chain { identifier } }
      }
    }
  }
}
"""
MINT_ACTION_QUERY = """
query MintActionTimelineQuery(
  $address: Address!
  $fromAssets: [AssetQuantityInput!]!
  $toAssets: [AssetQuantityInput!]!
  $recipient: Address
) {
  swap(
    address: $address
    fromAssets: $fromAssets
    toAssets: $toAssets
    recipient: $recipient
    action: MINT
  ) {
    actions {
      __typename
      ... on TransactionAction {
        transactionSubmissionData {
          to
          data
          value
        }
      }
    }
    errors { __typename }
  }
}
"""
DROP_CONFIG_CHANGES_QUERY = """
query DropConfigChangesQuery(
  $collectionSlug: String!
  $limit: Int!
  $after: Cursor
) {
  dropBySlug(slug: $collectionSlug) {
    configChangeHistory(limit: $limit, after: $after) {
      items {
        timestamp
        transactionHash
      }
    }
  }
}
"""
DROP_CONFIG_CHANGE_LIMIT = 20


class WalletOpenSeaClient:
    __slots__ = (
        "_action_timeout",
        "_app_id",
        "_attempts",
        "_authenticated_wallet",
        "_authentication_lock",
        "_client",
        "_eligibility_timeout",
        "_graphql_url",
        "_retry_interval_ms",
        "_site_url",
    )

    def __init__(self, config: OpenSeaConfig) -> None:
        try:
            self._client = httpx.AsyncClient(
                timeout=config.request_timeout_ms / 1000,
                limits=httpx.Limits(
                    max_connections=4,
                    max_keepalive_connections=4,
                    keepalive_expiry=HTTP_KEEPALIVE_EXPIRY_SECONDS,
                ),
                follow_redirects=False,
                http2=True,
                headers={"user-agent": f"osnm-z/{__version__}"},
            )
        except (TypeError, ValueError) as error:
            raise ProtocolError(ErrorKind.CLIENT) from error
        self._site_url = SITE_URL
        self._graphql_url = GRAPHQL_URL
        self._app_id = APP_ID
        self._action_timeout = config.action_request_timeout_ms / 1000
        self._eligibility_timeout = config.eligibility_request_timeout_ms / 1000
        self._attempts = config.attempts
        self._retry_interval_ms = config.retry_interval_ms
        self._authenticated_wallet: str | None = None
        self._authentication_lock = asyncio.Lock()

    async def __aenter__(self) -> WalletOpenSeaClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return (
            f"WalletOpenSeaClient(site_url={self._site_url!r}, "
            f"graphql_url={self._graphql_url!r}, "
            f"is_authenticated={self._authenticated_wallet is not None}, ...)"
        )

    async def collection_metadata(self, slug: str) -> CollectionMetadata:
        return await self._retry_request(
            "collection metadata", lambda: self._collection_metadata_once(slug)
        )

    async def config_change_transaction_hashes(self, slug: str) -> tuple[str, ...]:
        """Return newest-first transaction hashes from OpenSea's drop history."""
        validate_slug(slug)
        data = await self._graphql(
            "DropConfigChangesQuery",
            DROP_CONFIG_CHANGES_QUERY,
            {"collectionSlug": slug, "limit": DROP_CONFIG_CHANGE_LIMIT, "after": None},
            slug,
        )
        return decode_config_change_transaction_hashes(data)

    async def _collection_metadata_once(self, slug: str) -> CollectionMetadata:
        validate_slug(slug)
        data = await self._graphql("MintCollectionMetadata", COLLECTION_QUERY, {"slug": slug}, slug)
        return decode_collection(data, slug)

    async def resolve_collection(
        self, locator: CollectionLocator, expected_chain_id: int
    ) -> CollectionMetadata:
        if locator.slug is not None:
            metadata = await self.collection_metadata(locator.slug)
            if metadata.network_id != expected_chain_id:
                raise ProtocolError(ErrorKind.COLLECTION_CHAIN_MISMATCH)
            return metadata
        if locator.contract is None:
            raise ProtocolError(ErrorKind.INVALID_COLLECTION_LOCATOR)
        contract = locator.contract
        data = await self._retry_request(
            "collection search",
            lambda: self._graphql(
                "MintCollectionSearch",
                COLLECTION_SEARCH_QUERY,
                {"query": to_checksum_address(contract)},
                "search",
            ),
        )
        slug = matching_collection_slug(data, contract, expected_chain_id)
        metadata = await self.collection_metadata(slug)
        if not is_same_address(metadata.address, contract) or (
            metadata.network_id != expected_chain_id
        ):
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        return metadata

    async def authenticate(
        self,
        signer: WalletSigner,
        wallet: str,
        chain_id: int,
        slug: str,
    ) -> None:
        async with self._authentication_lock:
            await self._retry_request(
                "wallet authentication",
                lambda: self._authenticate_once(signer, wallet, chain_id, slug),
            )

    async def _authenticate_once(
        self,
        signer: WalletSigner,
        wallet: str,
        chain_id: int,
        slug: str,
    ) -> None:
        if chain_id <= 0 or not is_address(wallet):
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
        validate_slug(slug)
        self._set_connected_account_hint(wallet)
        collection_url = self._collection_url(slug)
        nonce = await self._request_nonce(collection_url)
        issued_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        address = to_checksum_address(wallet)
        domain = urlsplit(self._site_url).hostname
        if domain is None:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        message = create_siwe_message(domain, address, collection_url, chain_id, nonce, issued_at)
        if not is_same_address(signer.identity.address, wallet):
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
        try:
            signature = signer.sign_personal_message(message.encode())
        except WalletSignerError as error:
            raise ProtocolError(ErrorKind.SIGNING) from error
        body: dict[str, object] = {
            "message": {
                "domain": domain,
                "address": address,
                "statement": SIWE_STATEMENT,
                "uri": collection_url,
                "version": "1",
                "chainId": str(chain_id),
                "nonce": nonce,
                "issuedAt": issued_at,
                "accountType": "Ethereum",
            },
            "signature": signature,
            "chainArch": "EVM",
        }
        url = urljoin(self._site_url, "/__api/auth/siwe/verify")
        response = await self._send(
            url,
            headers={"origin": self._origin(), "referer": collection_url},
            body=body,
        )
        try:
            if not response.is_success:
                raise ProtocolError(ErrorKind.AUTHENTICATION, response.status_code)
            try:
                response_body = await _read_limited_body(response, AUTH_RESPONSE_LIMIT)
            except _BodyReadError as error:
                raise ProtocolError(ErrorKind.TRANSPORT) from error
        finally:
            await response.aclose()
        validate_authentication_response(response_body, wallet)
        self._authenticated_wallet = to_checksum_address(wallet)

    async def eligibility(self, slug: str, wallet: str) -> EligibilitySnapshot:
        snapshot = await self._retry_request(
            "wallet eligibility", lambda: self._eligibility_once(slug, wallet)
        )
        if snapshot.minter_quantity_minted is not None:
            return snapshot
        # Recheck an unknown count once during setup, without discarding usable eligibility.
        try:
            async with asyncio.timeout(self._eligibility_timeout):
                await asyncio.sleep(self._retry_interval_ms / 1000)
                return await self._eligibility_once(slug, wallet)
        except (ProtocolError, TimeoutError):
            return snapshot

    async def _eligibility_once(self, slug: str, wallet: str) -> EligibilitySnapshot:
        self._require_session(wallet)
        validate_slug(slug)
        data = await self._graphql(
            "DropEligibilityQuery",
            ELIGIBILITY_QUERY,
            {"address": wallet.lower(), "collectionSlug": slug},
            slug,
        )
        return decode_eligibility(data, wallet)

    async def mint_transaction_action(
        self,
        collection: CollectionMetadata,
        wallet: str,
        quantity: int,
    ) -> MintTransactionAction:
        try:
            async with asyncio.timeout(self._action_timeout):
                return await self._mint_action(collection, wallet, quantity)
        except TimeoutError as error:
            raise ProtocolError(ErrorKind.TRANSPORT) from error

    async def warm_mint_data_connection(self, slug: str) -> None:
        """Contact the GraphQL origin once without requesting wallet or mint data."""
        await self._graphql(
            "MintConnectionPreparation",
            "query MintConnectionPreparation { __typename }",
            {},
            slug,
        )

    async def _mint_action(
        self,
        collection: CollectionMetadata,
        wallet: str,
        quantity: int,
    ) -> MintTransactionAction:
        self._require_session(wallet)
        if quantity <= 0:
            raise ProtocolError(ErrorKind.INVALID_PROTOCOL_VALUE)
        variables: dict[str, object] = {
            "address": to_checksum_address(wallet),
            "fromAssets": [
                {
                    "asset": {
                        "contractAddress": to_checksum_address(ZERO_ADDRESS),
                        "chain": collection.chain_identifier,
                    }
                }
            ],
            "toAssets": [
                {
                    "asset": {
                        "contractAddress": to_checksum_address(collection.drop_address),
                        "chain": collection.chain_identifier,
                        "tokenId": "0",
                    },
                    "quantity": str(quantity),
                }
            ],
            "recipient": None,
        }
        data = await self._graphql(
            "MintActionTimelineQuery",
            MINT_ACTION_QUERY,
            variables,
            collection.slug,
        )
        return decode_mint_action(data)

    async def _request_nonce(self, referer: str) -> str:
        response = await self._send(
            urljoin(self._site_url, "/__api/auth/siwe/nonce"),
            headers={"origin": self._origin(), "referer": referer},
        )
        try:
            if not response.is_success:
                raise ProtocolError(ErrorKind.AUTHENTICATION, response.status_code)
            try:
                body = await _read_limited_body(response, NONCE_RESPONSE_LIMIT)
            except _BodyReadError as error:
                if isinstance(error.__cause__, httpx.HTTPError):
                    raise ProtocolError(ErrorKind.TRANSPORT) from error
                raise ProtocolError(ErrorKind.INVALID_NONCE_RESPONSE) from error
        finally:
            await response.aclose()
        return decode_nonce_response(body)

    async def _graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, object],
        slug: str,
    ) -> Any:
        referer = self._collection_url(slug)
        headers = {
            "accept": "application/json",
            "origin": self._origin(),
            "referer": referer,
        }
        request_timeout = (
            self._eligibility_timeout
            if operation_name == "DropEligibilityQuery"
            else self._action_timeout
            if operation_name == "MintActionTimelineQuery"
            else None
        )
        if operation_name == "DropEligibilityQuery":
            response = await self._send(
                self._graphql_url,
                method="GET",
                headers=headers,
                params={
                    "app_id": self._app_id,
                    "operationName": operation_name,
                    "variables": orjson.dumps(variables).decode(),
                    "extensions": orjson.dumps(
                        {
                            "persistedQuery": {
                                "sha256Hash": ELIGIBILITY_PERSISTED_QUERY_HASH,
                                "version": 1,
                            }
                        },
                    ).decode(),
                },
                request_timeout=request_timeout,
            )
            envelope = await self._read_graphql_envelope(response)
            if should_retry_without_persisted_query(envelope):
                response = await self._send_graphql_post(
                    operation_name,
                    query,
                    variables,
                    headers,
                    request_timeout,
                )
                envelope = await self._read_graphql_envelope(response)
        else:
            response = await self._send_graphql_post(
                operation_name, query, variables, headers, request_timeout
            )
            envelope = await self._read_graphql_envelope(response)
        return graphql_data(envelope)

    async def _send_graphql_post(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, object],
        headers: dict[str, str],
        request_timeout: float | None,
    ) -> httpx.Response:
        post_headers = {**headers, "x-app-id": self._app_id}
        body: dict[str, object] = {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }
        return await self._send(
            self._graphql_url,
            headers=post_headers,
            body=body,
            request_timeout=request_timeout,
        )

    async def _read_graphql_envelope(self, response: httpx.Response) -> dict[str, Any]:
        try:
            _validate_http_response(response)
            try:
                body = await _read_limited_body(response, GRAPHQL_RESPONSE_LIMIT)
            except _BodyReadError as error:
                if isinstance(error.__cause__, httpx.HTTPError):
                    raise ProtocolError(ErrorKind.TRANSPORT) from error
                raise ProtocolError(ErrorKind.COMPATIBILITY) from error
            return decode_graphql_envelope(body)
        finally:
            await response.aclose()

    async def _send(
        self,
        url: str,
        *,
        headers: dict[str, str],
        method: str = "POST",
        body: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> httpx.Response:
        request_headers = (
            headers if body is None else {**headers, "content-type": "application/json"}
        )
        try:
            request = self._client.build_request(
                method,
                url,
                headers=request_headers,
                params=params,
                content=None if body is None else orjson.dumps(body),
                timeout=self._client.timeout if request_timeout is None else request_timeout,
            )
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise ProtocolError(ErrorKind.TRANSPORT) from error

    async def _retry_request[T](
        self, operation_name: str, operation: Callable[[], Awaitable[T]]
    ) -> T:
        attempt_limit = max(self._attempts, DEFAULT_OPENSEA_ATTEMPTS)
        for attempt in range(1, attempt_limit + 1):
            try:
                return await operation()
            except ProtocolError as error:
                if not is_retryable_request_error(error) or attempt == attempt_limit:
                    raise
                logging.warn(f"OpenSea {operation_name} attempt {attempt} failed: {error}")
                await asyncio.sleep(self._retry_interval_ms / 1000)
        raise AssertionError("bounded OpenSea retry loop did not return")

    def _collection_url(self, slug: str) -> str:
        validate_slug(slug)
        return urljoin(self._site_url, f"/collection/{slug}/overview")

    def _origin(self) -> str:
        parsed = urlsplit(self._site_url)
        if not parsed.scheme or not parsed.hostname:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    def _set_connected_account_hint(self, wallet: str) -> None:
        hostname = urlsplit(self._site_url).hostname
        if hostname is None:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
        self._client.cookies.set(
            CONNECTED_ACCOUNT_HINT_COOKIE,
            to_checksum_address(wallet),
            domain=f".{hostname}",
            path="/",
        )

    def _require_session(self, wallet: str) -> None:
        if self._authenticated_wallet is None:
            raise ProtocolError(ErrorKind.SESSION_REQUIRED)
        if not is_same_address(self._authenticated_wallet, wallet):
            raise ProtocolError(ErrorKind.AUTHENTICATION_SESSION_MISMATCH)


class _BodyReadError(RuntimeError):
    pass


async def _read_limited_body(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(chunk) > limit - len(body):
                raise _BodyReadError
            body.extend(chunk)
    except httpx.HTTPError as error:
        raise _BodyReadError from error
    return bytes(body)


def _validate_http_response(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise ProtocolError(ErrorKind.RATE_LIMITED)
    if response.status_code == 401:
        raise ProtocolError(ErrorKind.AUTHENTICATION_REQUIRED)
    if not response.is_success:
        raise ProtocolError(ErrorKind.HTTP, response.status_code)
    content_length = response.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > GRAPHQL_RESPONSE_LIMIT:
            raise ProtocolError(ErrorKind.COMPATIBILITY)
    except ValueError as error:
        raise ProtocolError(ErrorKind.COMPATIBILITY) from error
