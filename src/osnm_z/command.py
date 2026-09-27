"""Load configuration and run interactive mint setup or diagnostics."""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

from . import logging, terminal
from .chain import ChainGateway
from .config import AppConfig, LoadedConfig
from .errors import MintError
from .execution import MintExecutionContext, execute_mint_phase
from .fee import configured_fee_estimate
from .launch_timing import (
    ensure_mint_not_expired,
    is_future_phase,
)
from .opensea import WalletOpenSeaClient
from .opensea_protocol import EligibilitySnapshot, ProtocolError, parse_collection_locator
from .outcomes import ExecutionState
from .phase_selection import (
    build_phase_options,
    public_only_eligibility,
    stage_name,
    stage_window,
    validate_mint_limits,
    validate_snapshot_shape,
    validate_stage_windows,
    warn_exhausted_stages,
)
from .public_mint import (
    discover_seadrop_address,
)
from .scheduling import SystemUtcClock
from .signing import WalletSigner


async def execute_command(command: str) -> None:
    if command not in {"mint", "doctor"}:
        raise MintError(f"unknown command {command}")
    logging.section("Configuration")
    loaded = LoadedConfig.load()
    signer = loaded.signer
    try:
        source_label = loaded.source_path.relative_to(Path.cwd().resolve())
    except ValueError:
        source_label = Path(loaded.source_path.name)
    logging.success(f"Configuration loaded from {source_label}.")
    logging.success(f"Wallet signer loaded: {signer.identity.address}.")
    if command == "doctor":
        await run_diagnostics(loaded.app, signer)
    else:
        await run_mint(loaded.app, signer)


async def run_diagnostics(config: AppConfig, signer: WalletSigner) -> None:
    logging.section("RPC checks")
    async with ChainGateway(
        config.rpc_request_timeout_ms / 1000,
        max_connections=4,
    ) as gateway:
        chain = await logging.animate(
            "Preparing RPC endpoint",
            gateway.prepare_chain(config.rpc_url),
        )
        await logging.animate(
            "Loading RPC fee and nonce data",
            gateway.submission_inputs(
                chain,
                signer.identity.address,
                configured_fee_estimate(config.fees),
            ),
        )
        logging.success(f"RPC is ready on chain {chain.chain_id}.")
    logging.section("OpenSea client check")
    async with WalletOpenSeaClient(config.opensea):
        logging.success("OpenSea client configured; sign-in is checked when minting.")


async def run_mint(config: AppConfig, signer: WalletSigner) -> None:
    locator = parse_collection_locator(terminal.prompt_collection_locator())
    logging.section("Collection lookup")
    async with AsyncExitStack() as stack:
        gateway = await stack.enter_async_context(
            ChainGateway(
                config.rpc_request_timeout_ms / 1000,
                max_connections=4,
            )
        )
        client = await stack.enter_async_context(WalletOpenSeaClient(config.opensea))
        chain = await logging.animate(
            "Preparing RPC endpoint",
            gateway.prepare_chain(config.rpc_url),
        )
        logging.success(f"Connected to RPC chain {chain.chain_id}.")
        metadata = await logging.animate(
            "Loading the OpenSea collection",
            client.resolve_collection(locator, chain.chain_id),
        )
        validate_stage_windows(metadata)
        logging.success(f"Loaded OpenSea collection {metadata.slug}.")
        logging.section("Wallet eligibility")
        eligibility: EligibilitySnapshot
        try:
            await logging.animate(
                "Signing in the wallet",
                client.authenticate(
                    signer,
                    signer.identity.address,
                    chain.chain_id,
                    metadata.slug,
                ),
            )
            logging.success("Wallet signed in.")
        except ProtocolError as error:
            logging.warn(f"Sign-in failed; only public stages can be selected: {error}")
            eligibility = public_only_eligibility(metadata)
        else:
            try:
                eligibility = await logging.animate(
                    "Loading this wallet's mint eligibility",
                    client.eligibility(metadata.slug, signer.identity.address),
                )
                validate_snapshot_shape(metadata, eligibility)
                logging.success("Wallet eligibility loaded.")
            except (ProtocolError, MintError) as error:
                logging.warn(
                    f"Eligibility unavailable; only public stages can be selected: {error}"
                )
                eligibility = public_only_eligibility(metadata)
        system_clock = SystemUtcClock()
        options = build_phase_options(metadata, eligibility, system_clock.now_seconds())
        logging.section("Phase selection")
        warn_exhausted_stages(options)
        selected = terminal.select_phase(options)
        quantity = terminal.prompt_quantity(options[selected])
        stage = metadata.stages[selected]
        validate_mint_limits(metadata, stage, eligibility, quantity)
        phase = stage_window(stage)
        if stage.stage_type != "PUBLIC_SALE":
            ensure_mint_not_expired(None, phase.ends_at)
        selected_stage_name = stage_name(stage)
        logging.section("Mint preparation")
        logging.info(f"Selected {selected_stage_name}; quantity: {quantity}.")
        public_mint_target = None
        if stage.stage_type == "PUBLIC_SALE":
            public_mint_target = await logging.animate(
                "Resolving the public mint contract",
                discover_seadrop_address(
                    client,
                    gateway,
                    chain,
                    metadata,
                ),
            )
        outcome = await execute_mint_phase(
            MintExecutionContext(
                config=config,
                chain=chain,
                gateway=gateway,
                client=client,
                signer=signer,
                metadata=metadata,
                eligibility=eligibility,
                selected_stage=stage,
                phase=phase,
                is_scheduled=is_future_phase(phase.starts_at, system_clock),
                quantity=quantity,
                gas_limit=config.gas_limit,
                public_mint_target=public_mint_target,
                system_clock=system_clock,
            )
        )
        if outcome.state is not ExecutionState.COMPLETED:
            raise MintError(f"Mint not confirmed; check {outcome.submission.transaction_hash}")
