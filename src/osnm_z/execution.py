"""Prepare and schedule one selected public or private mint phase."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace

from . import logging
from .action_retry import (
    PrivateActionRetryDisposition,
    classify_private_action_retry,
    private_action_retry_message,
    retry_committed_operation,
)
from .chain import SINGLE_RPC_WARMUP_TIMEOUT_SECONDS, ChainGateway
from .concurrency import gather_fail_fast
from .config import AppConfig, ChainConfig
from .domain import PhaseWindow
from .errors import MintError
from .fee import configured_fee_estimate, initial_transaction_fees
from .launch_timing import (
    PRIVATE_CALLDATA_LEAD_MS,
    PRIVATE_CONNECTION_PREPARATION_LEAD_SECONDS,
    PRIVATE_CONNECTION_PREPARATION_TIMEOUT_SECONDS,
    PRIVATE_PREPARATION_LEAD_SECONDS,
    PRIVATE_REAUTHENTICATION_LEAD_SECONDS,
    deadline_before,
    ensure_mint_not_expired,
    funding_check_deadline,
    nonce_refresh_window,
    preparation_deadline,
    rewarm_submission_endpoint,
    warm_submission_endpoint,
)
from .opensea import WalletOpenSeaClient
from .opensea_protocol import (
    CollectionMetadata,
    EligibilitySnapshot,
    MintTransactionAction,
    ProtocolError,
    StageMetadata,
    effective_native_mint_price,
)
from .outcomes import TransactionExecutionResult
from .phase_selection import (
    matching_eligibility_stage,
    stage_name,
    stage_start_nanoseconds,
    validate_mint_limits,
)
from .public_mint import (
    PublicMintBroadcastPlan,
    PublicMintContext,
    build_public_mint_action,
    public_mint_broadcast_plan,
    resolve_public_mint_context_and_stats,
)
from .rpc_protocol import SubmissionInputs
from .scheduling import (
    NANOSECONDS_PER_MILLISECOND,
    NANOSECONDS_PER_SECOND,
    LaunchClock,
    SystemUtcClock,
    validate_utc_timestamp_ns,
)
from .seadrop import mint_action_stage_index
from .signing import WalletSigner
from .submission import (
    SubmissionContext,
    prepare_mint_submission,
    refresh_prepared_public_mint,
    submit_prepared_mint,
)
from .wallet import (
    ensure_single_wallet_funding,
    retry_wallet_balance,
    retry_wallet_launch_snapshot,
)


@dataclass(slots=True)
class MintExecutionContext:
    config: AppConfig
    chain: ChainConfig
    gateway: ChainGateway
    client: WalletOpenSeaClient
    signer: WalletSigner
    metadata: CollectionMetadata
    eligibility: EligibilitySnapshot
    selected_stage: StageMetadata
    phase: PhaseWindow
    is_scheduled: bool
    quantity: int
    gas_limit: int
    public_mint_target: str | None
    authentication_task: asyncio.Task[None] | None = None
    system_clock: SystemUtcClock = field(default_factory=SystemUtcClock)
    mint_data_preparation_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class PrivateMintLaunchSnapshot:
    inputs: SubmissionInputs
    balance: int
    expected_mint_value: int


@dataclass(frozen=True, slots=True)
class PublicMintLaunchSnapshot:
    action: MintTransactionAction
    inputs: SubmissionInputs
    balance: int
    public_context: PublicMintContext
    broadcast_plan: PublicMintBroadcastPlan | None


def _require_public_mint_target(context: MintExecutionContext) -> str:
    target = context.public_mint_target
    if target is None:
        raise MintError("public mint contract was not prepared")
    return target


async def execute_mint_phase(context: MintExecutionContext) -> TransactionExecutionResult:
    selected_stage_name = stage_name(context.selected_stage)
    if context.is_scheduled and context.selected_stage.stage_type != "PUBLIC_SALE":
        preparation_utc_ns = deadline_before(
            validate_utc_timestamp_ns(stage_start_nanoseconds(context.selected_stage)),
            PRIVATE_PREPARATION_LEAD_SECONDS * NANOSECONDS_PER_SECOND,
        )
        preparation_clock = LaunchClock(preparation_utc_ns)
        await logging.animate(
            f"Waiting for {selected_stage_name} preparation window",
            preparation_clock.wait_until_lead_ns(),
            countdown_utc_ns=stage_start_nanoseconds(context.selected_stage),
        )
    try:
        return await _run_mint_launch(context)
    finally:
        preparation_tasks = tuple(
            task
            for task in (context.authentication_task, context.mint_data_preparation_task)
            if task is not None
        )
        for task in preparation_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*preparation_tasks, return_exceptions=True)


async def _run_mint_launch(context: MintExecutionContext) -> TransactionExecutionResult:
    wallet = context.signer.identity.address
    is_public = context.selected_stage.stage_type == "PUBLIC_SALE"
    selected_stage_name = stage_name(context.selected_stage)
    stage_launch_clock = LaunchClock(stage_start_nanoseconds(context.selected_stage))
    preparation_deadline_ns = (
        preparation_deadline(stage_launch_clock.target_utc_ns)
        if context.is_scheduled and not is_public
        else None
    )
    if (
        preparation_deadline_ns is not None
        and preparation_deadline_ns <= context.system_clock.now_ns()
    ):
        preparation_deadline_ns = None
    logging.info(f"Preparing {selected_stage_name}.")
    if is_public:
        public_mint_target = _require_public_mint_target(context)
        public_snapshot, _ = await gather_fail_fast(
            prepare_single_public_launch(context, wallet, public_mint_target),
            warm_submission_endpoint(context.gateway, context.chain),
        )
        public_context_result = public_snapshot.public_context
        action = public_snapshot.action
        inputs = public_snapshot.inputs
        balance = public_snapshot.balance
        public_broadcast_plan = public_snapshot.broadcast_plan
    else:
        public_broadcast_plan = None
        snapshot_operation = retry_committed_operation(
            f"Preparing {selected_stage_name}",
            lambda: prepare_single_wallet_launch(context, wallet),
            context.phase.ends_at,
            context.config.opensea.attempts,
            context.config.opensea.retry_interval_ms,
            recovery_deadline_ns=preparation_deadline_ns,
            attempt_timeout_seconds=SINGLE_RPC_WARMUP_TIMEOUT_SECONDS,
        )
        if context.is_scheduled:
            context.mint_data_preparation_task = asyncio.create_task(
                prepare_private_mint_connection(context, stage_launch_clock)
            )
            context.authentication_task = asyncio.create_task(
                reauthenticate_single_until_ready(
                    context,
                    wallet,
                    stage_launch_clock,
                )
            )
        snapshot, _ = await gather_fail_fast(
            snapshot_operation,
            warm_submission_endpoint(
                context.gateway, context.chain, deadline_ns=preparation_deadline_ns
            ),
        )
        inputs = snapshot.inputs
        balance = snapshot.balance
    scheduled_launch = public_broadcast_plan is not None if is_public else context.is_scheduled
    preparation = "public mint" if is_public else "wallet and RPC"
    logging.success(f"Initial {preparation} preparation complete for {selected_stage_name}.")
    submission_context = SubmissionContext(
        config=context.config,
        chain=context.chain,
        gateway=context.gateway,
        signer=context.signer,
        gas_limit=context.gas_limit,
        public_broadcast_plan=public_broadcast_plan,
        expected_nft_contract=context.metadata.address,
        expected_nft_recipient=wallet,
        expected_nft_quantity=context.quantity,
        is_scheduled=scheduled_launch,
        phase_ends_at=public_context_result.config.end_time if is_public else context.phase.ends_at,
    )
    prepared = prepare_mint_submission(submission_context, action, inputs) if is_public else None
    canonical_launch_utc_ns = (
        public_context_result.config.start_time * NANOSECONDS_PER_SECOND
        if is_public
        else stage_launch_clock.target_utc_ns
    )
    launch_utc_ns = validate_utc_timestamp_ns(canonical_launch_utc_ns)
    launch_clock = LaunchClock(launch_utc_ns)
    broadcast_utc_ns = launch_utc_ns
    if public_broadcast_plan is not None:
        broadcast_utc_ns = deadline_before(
            launch_utc_ns,
            public_broadcast_plan.offset_ms * NANOSECONDS_PER_MILLISECOND,
        )
    nonce_window = nonce_refresh_window(
        launch_utc_ns,
        broadcast_utc_ns,
    )
    nonce_refresh_utc_ns = nonce_window.refresh_utc_ns
    nonce_cutoff_ns = nonce_window.cutoff_utc_ns
    funding_check_utc_ns = funding_check_deadline(launch_utc_ns, broadcast_utc_ns)
    has_refresh_window = scheduled_launch and launch_clock.now_ns() < nonce_cutoff_ns
    if has_refresh_window:
        await logging.animate(
            "Waiting for the scheduled funding check",
            launch_clock.wait_until_lead_ns(max(0, launch_utc_ns - funding_check_utc_ns)),
            countdown_utc_ns=launch_utc_ns,
        )
        balance = await retry_wallet_balance(
            context.gateway,
            context.chain,
            wallet,
            nonce_cutoff_ns,
        )
    mint_value = action.value if is_public else snapshot.expected_mint_value
    ensure_single_wallet_funding(
        context.config,
        context.gas_limit,
        mint_value,
        inputs.fee_estimate,
        balance,
        scheduled=scheduled_launch,
    )
    if has_refresh_window:
        await logging.animate(
            "Funding checked; waiting for the final wallet refresh",
            launch_clock.wait_until_lead_ns(max(0, launch_utc_ns - nonce_refresh_utc_ns)),
            countdown_utc_ns=launch_utc_ns,
        )
        wallet_refresh = retry_wallet_launch_snapshot(
            context.gateway,
            context.chain,
            wallet,
            context.config.fees,
            deadline_ns=nonce_cutoff_ns,
        )
        if is_public:
            refreshed_wallet, refreshed_public_data, _ = await gather_fail_fast(
                wallet_refresh,
                retry_committed_operation(
                    "Public mint settings refresh",
                    lambda: resolve_public_mint_context_and_stats(
                        context.gateway,
                        context.chain,
                        context.metadata,
                        wallet,
                        _require_public_mint_target(context),
                        context.system_clock.now_ns(),
                    ),
                    public_context_result.config.end_time,
                    context.config.opensea.attempts,
                    context.config.opensea.retry_interval_ms,
                    recovery_deadline_ns=nonce_cutoff_ns,
                    attempt_timeout_seconds=SINGLE_RPC_WARMUP_TIMEOUT_SECONDS,
                ),
                rewarm_submission_endpoint(
                    context.gateway, context.chain, deadline_ns=nonce_cutoff_ns
                ),
            )
        else:
            refreshed_wallet, _ = await gather_fail_fast(
                wallet_refresh,
                rewarm_submission_endpoint(
                    context.gateway, context.chain, deadline_ns=nonce_cutoff_ns
                ),
            )
            refreshed_public_data = None
        balance = refreshed_wallet.balance
        inputs = refreshed_wallet.submission_inputs
        if refreshed_public_data is not None:
            refreshed_public_context, refreshed_stats = refreshed_public_data
            _validate_public_t10_window(public_context_result, refreshed_public_context)
            action = build_public_mint_action(
                context.metadata,
                context.selected_stage,
                refreshed_public_context,
                refreshed_stats,
                wallet,
                context.quantity,
            )
        if prepared is not None:
            refreshed_fees = initial_transaction_fees(
                context.config.fees, inputs.fee_estimate, scheduled=True
            )
            prepared = refresh_prepared_public_mint(
                submission_context,
                prepared,
                action,
                inputs.pending_nonce,
                refreshed_fees,
            )
        refreshed_mint_value = action.value if is_public else snapshot.expected_mint_value
        ensure_single_wallet_funding(
            context.config,
            context.gas_limit,
            refreshed_mint_value,
            inputs.fee_estimate,
            balance,
            scheduled=True,
        )
        logging.success("Final wallet checks complete.")
    submission_context = replace(submission_context, launch_clock=launch_clock)
    logging.section("Mint execution")
    if is_public:
        logging.success("Public mint transaction built from on-chain settings.")
    else:
        if context.is_scheduled:
            await logging.animate(
                f"Waiting to request mint data for {selected_stage_name}",
                launch_clock.wait_until_lead_ns(
                    PRIVATE_CALLDATA_LEAD_MS * NANOSECONDS_PER_MILLISECOND
                ),
                countdown_utc_ns=launch_utc_ns,
            )
        else:
            logging.info(f"Requesting mint data for {selected_stage_name}.")
        authentication_task = context.authentication_task
        connection_task = context.mint_data_preparation_task
        if connection_task is not None and not connection_task.done():
            if not connection_task.cancelling():
                connection_task.cancel()
        if authentication_task is not None:
            if not authentication_task.done():
                if not authentication_task.cancelling():
                    authentication_task.cancel()
            elif not authentication_task.cancelled() and authentication_task.exception() is None:
                context.authentication_task = None
            # Join background preparation cleanup on exit, not before the T-2 request.
            if context.authentication_task is not None:
                logging.warn("Early sign-in unavailable; the mint request can reauthenticate.")
        action = await request_single_wallet_action_hot(context, wallet, launch_clock)
        ensure_single_wallet_funding(
            context.config,
            context.gas_limit,
            action.value,
            inputs.fee_estimate,
            balance,
            scheduled=context.is_scheduled,
        )
        prepared = prepare_mint_submission(submission_context, action, inputs)
    assert prepared is not None
    return await submit_prepared_mint(submission_context, prepared)


async def prepare_single_wallet_launch(
    context: MintExecutionContext, wallet: str
) -> PrivateMintLaunchSnapshot:
    validate_mint_limits(
        context.metadata,
        context.selected_stage,
        context.eligibility,
        context.quantity,
    )
    stage_eligibility = matching_eligibility_stage(context.eligibility, context.selected_stage)
    native_price, price_chain = effective_native_mint_price(
        context.selected_stage, stage_eligibility
    )
    if price_chain is not None and price_chain != context.metadata.chain_identifier:
        raise MintError("OpenSea mint price is for a different chain")
    expected_mint_value = native_price * context.quantity
    if expected_mint_value > (1 << 256) - 1:
        raise MintError("selected mint quantity overflows its native value")
    wallet_snapshot = await context.gateway.wallet_snapshot(
        context.chain,
        wallet,
        configured_fee_estimate(context.config.fees),
    )
    return PrivateMintLaunchSnapshot(
        wallet_snapshot.submission_inputs,
        wallet_snapshot.balance,
        expected_mint_value,
    )


async def prepare_single_public_launch(
    context: MintExecutionContext,
    wallet: str,
    public_mint_target: str,
) -> PublicMintLaunchSnapshot:
    """Read the authoritative public window before making any scheduling decision."""
    wallet_snapshot, public_data = await gather_fail_fast(
        retry_committed_operation(
            "Public mint wallet preparation",
            lambda: context.gateway.wallet_snapshot(
                context.chain,
                wallet,
                configured_fee_estimate(context.config.fees),
            ),
            None,
            context.config.opensea.attempts,
            context.config.opensea.retry_interval_ms,
            attempt_timeout_seconds=SINGLE_RPC_WARMUP_TIMEOUT_SECONDS,
        ),
        retry_committed_operation(
            "Public mint settings",
            lambda: resolve_public_mint_context_and_stats(
                context.gateway,
                context.chain,
                context.metadata,
                wallet,
                public_mint_target,
                context.system_clock.now_ns(),
            ),
            None,
            context.config.opensea.attempts,
            context.config.opensea.retry_interval_ms,
            attempt_timeout_seconds=SINGLE_RPC_WARMUP_TIMEOUT_SECONDS,
        ),
    )
    public_context, stats = public_data
    action = build_public_mint_action(
        context.metadata,
        context.selected_stage,
        public_context,
        stats,
        wallet,
        context.quantity,
    )
    broadcast_plan = public_mint_broadcast_plan(
        public_context.config,
        context.config.scheduling.public_mint_broadcast_offset_ms,
        context.system_clock.now_ns(),
    )
    return PublicMintLaunchSnapshot(
        action,
        wallet_snapshot.submission_inputs,
        wallet_snapshot.balance,
        public_context,
        broadcast_plan,
    )


def _validate_public_t10_window(
    original: PublicMintContext,
    refreshed: PublicMintContext,
) -> None:
    if original.target.lower() != refreshed.target.lower():
        raise MintError("T-10 public mint target changed; nothing was broadcast")
    if (
        original.config.start_time != refreshed.config.start_time
        or original.config.end_time != refreshed.config.end_time
    ):
        raise MintError("T-10 public mint window changed; nothing was broadcast")


async def request_single_wallet_action_hot(
    context: MintExecutionContext,
    wallet: str,
    launch_clock: LaunchClock | None = None,
) -> MintTransactionAction:
    attempt_limit = context.config.opensea.calldata_attempts
    now_ns = launch_clock.now_ns if launch_clock is not None else time.time_ns
    starts_at_ns = stage_start_nanoseconds(context.selected_stage)
    ensure_mint_not_expired(launch_clock, context.phase.ends_at)
    remaining = (
        None
        if context.phase.ends_at is None
        else max(0.0, context.phase.ends_at - now_ns() / NANOSECONDS_PER_SECOND)
    )
    try:
        async with asyncio.timeout(remaining):
            for attempt in range(1, attempt_limit + 1):
                if attempt > 1:
                    await asyncio.sleep(context.config.opensea.retry_interval_ms / 1_000)
                ensure_mint_not_expired(launch_clock, context.phase.ends_at)
                stage_started_when_requested = now_ns() >= starts_at_ns
                try:
                    action = await context.client.mint_transaction_action(
                        context.metadata, wallet, context.quantity
                    )
                except ProtocolError as error:
                    classification = classify_private_action_retry(
                        error, stage_started_when_requested
                    )
                    if (
                        classification is PrivateActionRetryDisposition.TERMINAL
                        or attempt == attempt_limit
                    ):
                        raise
                    if classification is PrivateActionRetryDisposition.REAUTHENTICATE:
                        try:
                            await context.client.authenticate(
                                context.signer,
                                wallet,
                                context.chain.chain_id,
                                context.metadata.slug,
                            )
                        except ProtocolError:
                            pass
                    logging.warn(private_action_retry_message(classification))
                else:
                    ensure_mint_not_expired(launch_clock, context.phase.ends_at)
                    if mint_action_stage_index(action) == context.selected_stage.stage_index:
                        return action
                    selected_name = stage_name(context.selected_stage)
                    if attempt == attempt_limit:
                        raise MintError(f"OpenSea did not return mint data for {selected_name}")
                    logging.warn(f"OpenSea returned another stage; retrying for {selected_name}.")
    except TimeoutError as error:
        raise MintError("selected stage ended while requesting OpenSea mint data") from error
    raise AssertionError("bounded mint-data retry loop did not return")


async def prepare_private_mint_connection(
    context: MintExecutionContext,
    launch_clock: LaunchClock,
) -> None:
    """Warm the separate GraphQL origin once; failure must not prevent minting."""
    await launch_clock.wait_until_lead_ns(
        PRIVATE_CONNECTION_PREPARATION_LEAD_SECONDS * NANOSECONDS_PER_SECOND
    )
    remaining_seconds = (
        preparation_deadline(launch_clock.target_utc_ns) - launch_clock.now_ns()
    ) / NANOSECONDS_PER_SECOND
    if remaining_seconds <= 0:
        return
    try:
        async with asyncio.timeout(
            min(PRIVATE_CONNECTION_PREPARATION_TIMEOUT_SECONDS, remaining_seconds)
        ):
            await context.client.warm_mint_data_connection(context.metadata.slug)
    except (ProtocolError, TimeoutError):
        # The mint request can establish its own connection if this optional check fails.
        return


async def reauthenticate_single_until_ready(
    context: MintExecutionContext,
    wallet: str,
    launch_clock: LaunchClock,
) -> None:
    """Refresh the private session from T-20 until the T-2 mint-data window."""
    await launch_clock.wait_until_lead_ns(
        PRIVATE_REAUTHENTICATION_LEAD_SECONDS * NANOSECONDS_PER_SECOND
    )
    deadline_ns = deadline_before(
        launch_clock.target_utc_ns,
        PRIVATE_CALLDATA_LEAD_MS * NANOSECONDS_PER_MILLISECOND,
    )
    last_error: ProtocolError | None = None
    while launch_clock.now_ns() < deadline_ns:
        remaining_seconds = (deadline_ns - launch_clock.now_ns()) / NANOSECONDS_PER_SECOND
        try:
            async with asyncio.timeout(remaining_seconds):
                await context.client.authenticate(
                    context.signer,
                    wallet,
                    context.chain.chain_id,
                    context.metadata.slug,
                )
            return
        except ProtocolError as error:
            last_error = error
            logging.warn(f"Early wallet sign-in failed. Retrying: {error}")
        except TimeoutError:
            break
        remaining_seconds = (deadline_ns - launch_clock.now_ns()) / NANOSECONDS_PER_SECOND
        if remaining_seconds <= 0:
            break
        await asyncio.sleep(
            min(context.config.opensea.retry_interval_ms / 1_000, remaining_seconds)
        )
    raise MintError("wallet sign-in did not recover before the mint-data window") from last_error
