"""Bounded RPC connections, wallet reads, submission, and receipt polling."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
import orjson
from eth_abi import decode, encode  # type: ignore[attr-defined]
from eth_abi.exceptions import DecodingError
from eth_utils.address import is_address, to_checksum_address

from . import logging
from .config import HTTP_KEEPALIVE_EXPIRY_SECONDS, ChainConfig
from .rpc_protocol import (
    BatchReadError,
    ChainError,
    FeeEstimate,
    RateLimitError,
    ReceiptPollingPolicy,
    RpcResponseError,
    SubmissionInputs,
    TransactionReceipt,
    TransactionSubmissionError,
    WalletSnapshot,
    contract_call_request,
    decode_fee_inputs,
    decode_transaction_receipt,
    extract_json_rpc_result,
    failed_submission_disposition,
    invalid_rpc_response,
    is_already_known,
    is_transaction_hash,
    parse_hex_bytes,
    parse_json_rpc_quantity,
    rpc_request,
    validate_contract_calls,
    validate_returned_transaction_hash,
)
from .transaction import SignedTransaction

RPC_RESPONSE_LIMIT = 1024 * 1024
RPC_BATCH_MAX_CALLS = 250
WALLET_SNAPSHOT_MAX_ATTEMPTS = 2
SUBMISSION_RPC_REWARM_TIMEOUT_SECONDS = 1.0
SINGLE_RPC_WARMUP_TIMEOUT_SECONDS = (3.0, 4.0, 5.0, 6.0, 7.0)
ENDPOINT_PREPARATION_TOTAL_TIMEOUT_SECONDS = 10.0
MULTICALL3_ADDRESS = to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
MULTICALL3_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")


class _JsonRpcBatchUnsupported(ChainError):
    """A read endpoint did not return a usable JSON-RPC batch envelope."""


class _MulticallUnavailable(ChainError):
    """The canonical Multicall3 endpoint did not return a decodable aggregate result."""


class ChainGateway:
    __slots__ = (
        "_client",
        "_multicall_available",
        "_read_limiter",
        "_request_timeout_seconds",
        "_rpc_batch_available",
        "_submission_limiter",
    )

    def __init__(self, timeout_seconds: float, max_connections: int = 100) -> None:
        try:
            if max_connections <= 0 or timeout_seconds <= 0:
                raise ValueError
            self._client = httpx.AsyncClient(
                timeout=timeout_seconds,
                limits=httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_connections,
                    keepalive_expiry=HTTP_KEEPALIVE_EXPIRY_SECONDS,
                ),
                follow_redirects=False,
                http2=True,
            )
            self._multicall_available: dict[tuple[int, str], bool] = {}
            self._rpc_batch_available: dict[str, bool] = {}
            self._read_limiter = asyncio.Semaphore(max_connections)
            self._submission_limiter = asyncio.Semaphore(max_connections)
            self._request_timeout_seconds = timeout_seconds
        except (TypeError, ValueError) as error:
            raise ChainError("cannot construct the RPC HTTP client") from error

    async def __aenter__(self) -> ChainGateway:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return "ChainGateway(...)"

    async def prepare_chain(self, rpc_url: str) -> ChainConfig:
        """Resolve the chain served by RPC_URL before preparing a mint."""
        value = await self._request(rpc_url, "eth_chainId", [])
        try:
            chain_id = parse_json_rpc_quantity(value, 64)
            if chain_id == 0:
                raise ValueError
        except ValueError as error:
            raise ChainError("RPC_URL returned an invalid chain ID") from error
        return ChainConfig(chain_id, rpc_url)

    async def warm_submission_endpoint(
        self, config: ChainConfig, *, timeout_seconds: float | None = None
    ) -> None:
        """Verify the configured chain within the preparation retry budget."""
        budget_seconds = min(
            self._request_timeout_seconds,
            ENDPOINT_PREPARATION_TOTAL_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds,
        )
        if budget_seconds <= 0:
            raise ChainError("RPC endpoint preparation deadline has elapsed")
        last_error: ChainError | None = None
        try:
            async with asyncio.timeout(budget_seconds):
                for request_timeout in SINGLE_RPC_WARMUP_TIMEOUT_SECONDS:
                    try:
                        await self.rewarm_submission_endpoint(
                            config, timeout_seconds=min(request_timeout, budget_seconds)
                        )
                        return
                    except ChainError as error:
                        last_error = error
        except TimeoutError as error:
            raise ChainError(
                f"RPC endpoint preparation exceeded {budget_seconds:g} seconds"
            ) from error
        assert last_error is not None
        raise last_error

    async def rewarm_submission_endpoint(
        self,
        config: ChainConfig,
        *,
        timeout_seconds: float = SUBMISSION_RPC_REWARM_TIMEOUT_SECONDS,
    ) -> None:
        """Refresh the RPC connection and reject a changed chain."""
        try:
            async with asyncio.timeout(timeout_seconds):
                value = await self._request(
                    config.rpc_url,
                    "eth_chainId",
                    [],
                    timeout_seconds=timeout_seconds,
                )
            chain_id = parse_json_rpc_quantity(value, 64)
        except TimeoutError as error:
            raise ChainError(
                f"RPC endpoint did not respond within {timeout_seconds:g} seconds"
            ) from error
        except (TypeError, ValueError) as error:
            raise invalid_rpc_response() from error
        if chain_id != config.chain_id:
            raise ChainError(
                f"RPC endpoint reports chain ID {chain_id}; expected {config.chain_id}"
            )

    async def balance(
        self,
        config: ChainConfig,
        wallet: str,
        timeout_seconds: float | None = None,
    ) -> int:
        if not is_address(wallet):
            raise invalid_rpc_response()
        value = await self._request(
            config.rpc_url,
            "eth_getBalance",
            [to_checksum_address(wallet), "pending"],
            timeout_seconds=timeout_seconds,
        )
        try:
            return parse_json_rpc_quantity(value, 256)
        except (TypeError, ValueError) as error:
            raise invalid_rpc_response() from error

    async def wallet_snapshot(
        self,
        config: ChainConfig,
        wallet: str,
        fee_estimate: FeeEstimate | None = None,
        timeout_seconds: float | None = None,
    ) -> WalletSnapshot:
        """Capture one wallet's pending nonce, balance, and fees; retry a nonce race."""
        if not is_address(wallet):
            raise invalid_rpc_response()
        address = to_checksum_address(wallet)
        for _ in range(WALLET_SNAPSHOT_MAX_ATTEMPTS):
            calls: list[tuple[str, list[object]]] = []
            if fee_estimate is None:
                calls.extend(
                    (
                        ("eth_getBlockByNumber", ["latest", False]),
                        ("eth_maxPriorityFeePerGas", []),
                    )
                )
            wallet_offset = len(calls)
            calls.extend(
                (
                    ("eth_getBalance", [address, "pending"]),
                    ("eth_getTransactionCount", [address, "latest"]),
                    ("eth_getTransactionCount", [address, "pending"]),
                    ("eth_getTransactionCount", [address, "pending"]),
                )
            )
            raw = await self._batch_results(
                config.rpc_url,
                calls,
                timeout_seconds=timeout_seconds,
            )
            error = next((value for value in raw if isinstance(value, ChainError)), None)
            if error is not None:
                raise error
            group = raw[wallet_offset:]
            try:
                latest_nonce = parse_json_rpc_quantity(group[1], 64)
                state_nonce = parse_json_rpc_quantity(group[2], 64)
                input_nonce = parse_json_rpc_quantity(group[3], 64)
                if latest_nonce > state_nonce:
                    raise ValueError
                if state_nonce != input_nonce:
                    continue
                estimate = (
                    decode_fee_inputs(raw[0], raw[1]) if fee_estimate is None else fee_estimate
                )
                return WalletSnapshot(
                    parse_json_rpc_quantity(group[0], 256), SubmissionInputs(input_nonce, estimate)
                )
            except (TypeError, ValueError) as error:
                raise invalid_rpc_response() from error
        raise ChainError("wallet nonce changed while RPC account state and fees were captured")

    async def contract_call(
        self,
        config: ChainConfig,
        target: str,
        calldata: bytes,
    ) -> bytes:
        if not is_address(target) or not calldata:
            raise invalid_rpc_response()
        result = await self._request(
            config.rpc_url,
            *contract_call_request(to_checksum_address(target), calldata),
        )
        try:
            return parse_hex_bytes(result)
        except (TypeError, ValueError) as error:
            raise invalid_rpc_response() from error

    async def contract_reads(
        self,
        config: ChainConfig,
        calls: Sequence[tuple[str, bytes]],
    ) -> tuple[bytes | ChainError, ...]:
        """Execute independent latest-state calls through Multicall3, with RPC batch fallback."""
        validated = validate_contract_calls(calls)
        if not validated:
            return ()
        if len(validated) == 1:
            try:
                return (await self.contract_call(config, *validated[0]),)
            except ChainError as error:
                return (_normalize_read_error(error),)
        cache_key = (config.chain_id, config.rpc_url)
        if self._multicall_available.get(cache_key, True):
            try:
                values = await self._multicall3(config, validated)
                self._multicall_available[cache_key] = True
                failed = [
                    index for index, value in enumerate(values) if isinstance(value, ChainError)
                ]
                if not failed:
                    return values
                detailed = await self._rpc_contract_reads(
                    config, tuple(validated[index] for index in failed)
                )
                merged = list(values)
                for index, value in zip(failed, detailed, strict=True):
                    merged[index] = value
                return tuple(merged)
            except ChainError as error:
                if isinstance(error, (RpcResponseError, _MulticallUnavailable)):
                    self._multicall_available[cache_key] = False
        return await self._rpc_contract_reads(config, validated)

    async def submission_inputs(
        self,
        config: ChainConfig,
        wallet: str,
        fee_estimate: FeeEstimate | None = None,
    ) -> SubmissionInputs:
        endpoint = config.rpc_url
        if not is_address(wallet):
            raise invalid_rpc_response()
        address = to_checksum_address(wallet)
        calls: list[tuple[str, list[object]]]
        if fee_estimate is None:
            calls = [
                ("eth_getBlockByNumber", ["latest", False]),
                ("eth_getTransactionCount", [address, "latest"]),
                ("eth_getTransactionCount", [address, "pending"]),
                ("eth_maxPriorityFeePerGas", []),
            ]
        else:
            calls = [
                ("eth_getTransactionCount", [address, "latest"]),
                ("eth_getTransactionCount", [address, "pending"]),
            ]
        raw = await self._batch_results(endpoint, calls)
        error = next((value for value in raw if isinstance(value, ChainError)), None)
        if error is not None:
            raise error
        try:
            if fee_estimate is None:
                latest_nonce = parse_json_rpc_quantity(raw[1], 64)
                pending_nonce = parse_json_rpc_quantity(raw[2], 64)
                estimate = decode_fee_inputs(raw[0], raw[3])
            else:
                latest_nonce = parse_json_rpc_quantity(raw[0], 64)
                pending_nonce = parse_json_rpc_quantity(raw[1], 64)
                estimate = fee_estimate
            if latest_nonce > pending_nonce:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise invalid_rpc_response() from error
        return SubmissionInputs(pending_nonce, estimate)

    async def send_raw_transaction(self, config: ChainConfig, signed: SignedTransaction) -> str:
        """Send once, preserving the local hash when delivery is uncertain."""
        try:
            returned_hash = await self._request(
                config.rpc_url,
                "eth_sendRawTransaction",
                [f"0x{signed.raw.hex()}"],
            )
            validate_returned_transaction_hash(returned_hash, signed)
        except asyncio.CancelledError:
            logging.warn(f"Submission interrupted; check {signed.hash_hex} before retrying.")
            raise
        except ChainError as error:
            if not is_already_known(error):
                raise TransactionSubmissionError(
                    str(error), failed_submission_disposition(error)
                ) from error
        return signed.hash_hex

    async def transaction_receipts(
        self,
        config: ChainConfig,
        transaction_hashes: Sequence[str],
        timeout_seconds: float | None = None,
    ) -> tuple[TransactionReceipt | ChainError | None, ...]:
        """Read receipts from the same RPC used for submission."""
        hashes = tuple(transaction_hashes)
        if any(not is_transaction_hash(transaction_hash) for transaction_hash in hashes):
            raise invalid_rpc_response()
        raw = await self._batch_results(
            config.rpc_url,
            tuple(("eth_getTransactionReceipt", [transaction_hash]) for transaction_hash in hashes),
            timeout_seconds=timeout_seconds,
        )
        decoded: list[TransactionReceipt | ChainError | None] = []
        for transaction_hash, value in zip(hashes, raw, strict=True):
            if isinstance(value, ChainError):
                decoded.append(value)
                continue
            try:
                decoded.append(decode_transaction_receipt(value, transaction_hash))
            except ChainError as error:
                decoded.append(error)
        return tuple(decoded)

    async def wait_for_transaction_receipt(
        self,
        config: ChainConfig,
        transaction_hash: str,
        policy: ReceiptPollingPolicy,
    ) -> TransactionReceipt | None:
        if not is_transaction_hash(transaction_hash):
            raise invalid_rpc_response()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + policy.timeout_seconds
        interval_seconds = max(policy.interval_ms, 50) / 1000
        received_response = False
        last_error: ChainError | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                if not received_response and last_error is not None:
                    raise last_error
                return None
            request_timeout = min(
                remaining,
                self._request_timeout_seconds,
            )
            try:
                async with asyncio.timeout(request_timeout):
                    receipts = await self.transaction_receipts(
                        config,
                        (transaction_hash,),
                        request_timeout,
                    )
            except TimeoutError:
                last_error = ChainError(
                    "receipt RPC exceeded the remaining receipt-tracking deadline"
                )
                receipts = ()
            for receipt in receipts:
                if isinstance(receipt, ChainError):
                    last_error = receipt
                    continue
                received_response = True
                if receipt is not None:
                    return receipt
            remaining = deadline - loop.time()
            if remaining <= 0:
                if not received_response and last_error is not None:
                    raise last_error
                return None
            await asyncio.sleep(min(interval_seconds, remaining))

    async def _multicall3(
        self,
        config: ChainConfig,
        calls: tuple[tuple[str, bytes], ...],
    ) -> tuple[bytes | ChainError, ...]:
        payload = MULTICALL3_AGGREGATE3_SELECTOR + encode(
            ["(address,bool,bytes)[]"],
            [[(target, True, calldata) for target, calldata in calls]],
        )
        encoded = await self.contract_call(config, MULTICALL3_ADDRESS, payload)
        try:
            decoded = decode(["(bool,bytes)[]"], encoded)[0]
            if len(decoded) != len(calls):
                raise ValueError
            return tuple(
                bytes(return_data) if success else ChainError(f"contract read {index + 1} reverted")
                for index, (success, return_data) in enumerate(decoded)
            )
        except (DecodingError, TypeError, ValueError) as error:
            raise _MulticallUnavailable(
                "RPC endpoint did not return a usable Multicall3 result"
            ) from error

    async def _rpc_contract_reads(
        self,
        config: ChainConfig,
        calls: tuple[tuple[str, bytes], ...],
    ) -> tuple[bytes | ChainError, ...]:
        raw = await self._batch_results(
            config.rpc_url,
            tuple(contract_call_request(target, calldata) for target, calldata in calls),
        )
        decoded: list[bytes | ChainError] = []
        for value in raw:
            if isinstance(value, ChainError):
                decoded.append(value)
                continue
            try:
                decoded.append(parse_hex_bytes(value))
            except (TypeError, ValueError):
                decoded.append(invalid_rpc_response())
        return tuple(decoded)

    async def _batch_results(
        self,
        endpoint: str,
        calls: Sequence[tuple[str, list[object]]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[Any | ChainError, ...]:
        if not calls:
            return ()
        chunks = [
            calls[offset : offset + RPC_BATCH_MAX_CALLS]
            for offset in range(0, len(calls), RPC_BATCH_MAX_CALLS)
        ]
        groups = await asyncio.gather(
            *(
                self._read_chunk(
                    endpoint,
                    chunk,
                    timeout_seconds=timeout_seconds,
                )
                for chunk in chunks
            )
        )
        return tuple(value for group in groups for value in group)

    async def _read_chunk(
        self,
        endpoint: str,
        calls: Sequence[tuple[str, list[object]]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[Any | ChainError, ...]:
        if len(calls) == 1 or not self._rpc_batch_available.get(endpoint, True):
            return await self._individual_results(
                endpoint,
                calls,
                timeout_seconds=timeout_seconds,
            )
        try:
            results = await self._batch_chunk(
                endpoint,
                calls,
                timeout_seconds=timeout_seconds,
            )
        except _JsonRpcBatchUnsupported:
            self._rpc_batch_available[endpoint] = False
            return await self._individual_results(
                endpoint,
                calls,
                timeout_seconds=timeout_seconds,
            )
        except ChainError as error:
            batch_error = _normalize_read_error(error)
            return tuple(batch_error for _ in calls)
        self._rpc_batch_available[endpoint] = True
        return results

    async def _individual_results(
        self,
        endpoint: str,
        calls: Sequence[tuple[str, list[object]]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[Any | ChainError, ...]:
        results = await asyncio.gather(
            *(
                self._request(
                    endpoint,
                    method,
                    params,
                    timeout_seconds=timeout_seconds,
                )
                for method, params in calls
            ),
            return_exceptions=True,
        )
        normalized: list[Any | ChainError] = []
        for result in results:
            if isinstance(result, ChainError):
                normalized.append(_normalize_read_error(result))
            elif isinstance(result, BaseException):
                raise result
            else:
                normalized.append(result)
        return tuple(normalized)

    async def _batch_chunk(
        self,
        endpoint: str,
        calls: Sequence[tuple[str, list[object]]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[Any | ChainError, ...]:
        requests = [
            rpc_request(request_id, method, params)
            for request_id, (method, params) in enumerate(calls, 1)
        ]
        envelope = await self._post_json(
            endpoint,
            requests,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(envelope, list) or len(envelope) != len(requests):
            raise _batch_unsupported()
        results: list[Any | ChainError | None] = [None] * len(requests)
        occupied = [False] * len(requests)
        for response in envelope:
            if not isinstance(response, dict) or type(response.get("id")) is not int:
                raise _batch_unsupported()
            response_id = response["id"]
            if not 1 <= response_id <= len(requests) or occupied[response_id - 1]:
                raise _batch_unsupported()
            occupied[response_id - 1] = True
            try:
                results[response_id - 1] = extract_json_rpc_result(response, response_id)
            except ChainError as error:
                results[response_id - 1] = _normalize_read_error(error)
        if not all(occupied):
            raise _batch_unsupported()
        return tuple(results)

    async def _request(
        self,
        endpoint: str,
        method: str,
        params: list[object],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        envelope = await self._post_json(
            endpoint,
            rpc_request(1, method, params),
            timeout_seconds=timeout_seconds,
        )
        return extract_json_rpc_result(envelope, 1)

    async def _post_json(
        self,
        endpoint: str,
        payload: object,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        try:
            request_budget = (
                self._request_timeout_seconds if timeout_seconds is None else timeout_seconds
            )
            request = self._client.build_request(
                "POST",
                endpoint,
                content=orjson.dumps(payload),
                headers={"content-type": "application/json"},
                timeout=request_budget,
            )
            limiter = (
                self._submission_limiter if _is_submission_payload(payload) else self._read_limiter
            )
            async with asyncio.timeout(request_budget), limiter:
                response = await self._client.send(request, stream=True)
                try:
                    if response.status_code == 429:
                        raise RateLimitError("RPC endpoint rate limited the request")
                    if not response.is_success:
                        raise invalid_rpc_response()
                    content_length = response.headers.get("content-length")
                    if content_length is not None and int(content_length) > RPC_RESPONSE_LIMIT:
                        raise invalid_rpc_response()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunk) > RPC_RESPONSE_LIMIT - len(body):
                            raise invalid_rpc_response()
                        body.extend(chunk)
                    try:
                        return orjson.loads(body)
                    except orjson.JSONDecodeError as error:
                        raise invalid_rpc_response() from error
                except (ValueError, httpx.HTTPError) as error:
                    raise invalid_rpc_response() from error
                finally:
                    await response.aclose()
        except TimeoutError as error:
            raise ChainError("RPC endpoint request exceeded its total timeout") from error
        except httpx.HTTPError as error:
            raise invalid_rpc_response() from error


def _is_submission_payload(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("method") == "eth_sendRawTransaction"


def _batch_unsupported() -> ChainError:
    return _JsonRpcBatchUnsupported("RPC endpoint does not support the required JSON-RPC batch")


def _normalize_read_error(error: ChainError) -> ChainError:
    if isinstance(error, (BatchReadError, RpcResponseError)):
        return error
    return BatchReadError(str(error))
