"""Sign, submit, and verify one prepared mint transaction."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from . import logging
from .chain import ChainGateway
from .config import AppConfig, ChainConfig
from .domain import Eip1559Fees
from .errors import MintError
from .fee import initial_transaction_fees
from .launch_timing import (
    deadline_before,
    ensure_mint_not_expired,
    wait_for_public_broadcast,
)
from .nft import NftError, extract_minted_assets
from .opensea_protocol import MintTransactionAction
from .outcomes import TransactionExecutionResult
from .public_mint import PublicMintBroadcastPlan
from .rpc_protocol import (
    ChainError,
    RawTransactionSubmission,
    ReceiptPollingPolicy,
    SubmissionDisposition,
    SubmissionInputs,
    TransactionReceipt,
    TransactionSubmissionError,
)
from .scheduling import (
    NANOSECONDS_PER_MILLISECOND,
    LaunchClock,
    format_launch_delta_ms,
)
from .signing import WalletSigner
from .transaction import (
    Eip1559Transaction,
    SignedTransaction,
    sign_eip1559_transaction,
)


@dataclass(frozen=True, slots=True)
class SubmissionContext:
    config: AppConfig
    chain: ChainConfig
    gateway: ChainGateway
    signer: WalletSigner
    gas_limit: int
    expected_nft_contract: str
    expected_nft_recipient: str
    expected_nft_quantity: int
    public_broadcast_plan: PublicMintBroadcastPlan | None = None
    launch_clock: LaunchClock | None = None
    is_scheduled: bool = False
    phase_ends_at: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedMintSubmission:
    action: MintTransactionAction
    nonce: int
    fees: Eip1559Fees
    signed: SignedTransaction


def refresh_prepared_public_mint(
    context: SubmissionContext,
    prepared: PreparedMintSubmission,
    action: MintTransactionAction,
    nonce: int,
    fees: Eip1559Fees,
) -> PreparedMintSubmission:
    """Preserve signed bytes unless refreshed transaction fields changed."""
    if prepared.action == action and prepared.nonce == nonce and prepared.fees == fees:
        return prepared
    return PreparedMintSubmission(
        action,
        nonce,
        fees,
        sign_mint_transaction(context, action, nonce, fees),
    )


def prepare_mint_submission(
    context: SubmissionContext,
    action: MintTransactionAction,
    inputs: SubmissionInputs,
) -> PreparedMintSubmission:
    fees = initial_transaction_fees(
        context.config.fees, inputs.fee_estimate, scheduled=context.is_scheduled
    )
    signed = sign_mint_transaction(context, action, inputs.pending_nonce, fees)
    return PreparedMintSubmission(action, inputs.pending_nonce, fees, signed)


async def submit_prepared_mint(
    context: SubmissionContext,
    prepared: PreparedMintSubmission,
) -> TransactionExecutionResult:
    if context.public_broadcast_plan is not None:
        launch_clock = await logging.animate(
            "Waiting for the configured public mint broadcast time",
            wait_for_public_broadcast(context.public_broadcast_plan, context.launch_clock),
            countdown_utc_ns=context.public_broadcast_plan.starts_at_ns,
        )
        context = replace(context, launch_clock=launch_clock)
    ensure_mint_not_expired(context.launch_clock, context.phase_ends_at)
    submission = await broadcast_signed_transaction(context, prepared.signed, prepared.nonce)
    transaction_hash = submission.transaction_hash
    if submission.disposition is SubmissionDisposition.ACKNOWLEDGED:
        logging.info(f"Transaction accepted by RPC: {transaction_hash}.")
    logging.section("Receipt tracking")
    try:
        receipt = await logging.animate(
            "Waiting for transaction receipt",
            context.gateway.wait_for_transaction_receipt(
                context.chain,
                transaction_hash,
                ReceiptPollingPolicy.from_retry_config(context.config.retry),
            ),
        )
    except ChainError as error:
        logging.section("Mint result")
        logging.warn(f"Could not check confirmation for {transaction_hash}: {error}")
        return TransactionExecutionResult(submission, None)
    logging.section("Mint result")
    if receipt is None:
        logging.warn(f"Unconfirmed; not resent: {transaction_hash}")
        return TransactionExecutionResult(submission, None)
    finalize_mined_receipt(receipt, context)
    return TransactionExecutionResult(submission, receipt)


def sign_mint_transaction(
    context: SubmissionContext,
    action: MintTransactionAction,
    nonce: int,
    fees: Eip1559Fees,
) -> SignedTransaction:
    return sign_eip1559_transaction(
        Eip1559Transaction(
            chain_id=context.chain.chain_id,
            nonce=nonce,
            max_priority_fee_per_gas=fees.max_priority_fee_per_gas,
            max_fee_per_gas=fees.max_fee_per_gas,
            gas_limit=context.gas_limit,
            target=action.target,
            value=action.value,
            calldata=action.calldata,
        ),
        context.signer,
    )


async def broadcast_signed_transaction(
    context: SubmissionContext,
    signed_transaction: SignedTransaction,
    nonce: int,
) -> RawTransactionSubmission:
    local_hash = signed_transaction.hash_hex
    public_plan = context.public_broadcast_plan
    submission_started_monotonic_ns = time.perf_counter_ns()
    submission_started_ns = time.time_ns()
    try:
        try:
            transaction_hash = await context.gateway.send_raw_transaction(
                context.chain, signed_transaction
            )
        finally:
            if public_plan is not None:
                configured_target_ns = deadline_before(
                    public_plan.starts_at_ns,
                    public_plan.offset_ms * NANOSECONDS_PER_MILLISECOND,
                )
                deadline_delta_ms = (
                    submission_started_ns - configured_target_ns
                ) / NANOSECONDS_PER_MILLISECOND
                measured_delta = format_launch_delta_ms(
                    public_plan.starts_at_ns,
                    submission_started_ns,
                )
                timing_summary = (
                    f"configured=T-{public_plan.offset_ms} ms, measured={measured_delta}, "
                    f"deadline_delta={deadline_delta_ms:+.3f} ms"
                )
                message = f"Public mint: {timing_summary}, nonce={nonce}, hash={local_hash}."
                logging.info_at_unix_ns(submission_started_ns, message)
    except TransactionSubmissionError as error:
        if error.disposition is SubmissionDisposition.REJECTED:
            raise
        logging.warn(f"Submission uncertain; not resent: {local_hash}: {error}")
        return RawTransactionSubmission(local_hash, error.disposition)
    if public_plan is not None:
        transport_ms = (
            time.perf_counter_ns() - submission_started_monotonic_ns
        ) / NANOSECONDS_PER_MILLISECOND
        logging.info(f"Public mint submission acknowledged in {transport_ms:.3f} ms.")
    return RawTransactionSubmission(
        transaction_hash,
        SubmissionDisposition.ACKNOWLEDGED,
    )


def finalize_mined_receipt(receipt: TransactionReceipt, context: SubmissionContext) -> None:
    if receipt.is_success:
        try:
            extract_minted_assets(
                receipt,
                context.expected_nft_contract,
                context.expected_nft_recipient,
                context.expected_nft_quantity,
            )
        except NftError as error:
            raise MintError(
                "successful transaction receipt did not prove the requested ERC-721 mint: "
                f"{receipt.transaction_hash}",
            ) from error
        logging.success(f"Minted {receipt.transaction_hash}; block {receipt.block_number}.")
        return
    raise MintError(
        f"mint transaction reverted in block {receipt.block_number}: {receipt.transaction_hash}",
    )
