"""Mint phase parsing, eligibility, and selection policy."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from . import logging
from .domain import UINT256_MAX, ExecutionKind, PhaseWindow
from .errors import MintError
from .opensea_protocol import (
    CollectionMetadata,
    EligibilitySnapshot,
    EligibleMinterRelation,
    StageEligibility,
    StageMetadata,
)
from .scheduling import NANOSECONDS_PER_SECOND, parse_rfc3339_ns
from .terminal import PhaseOption

STAGE_NAME_MAX_LENGTH = 100


class EligibilityEvidence(Enum):
    PUBLIC_SALE = "public_sale"
    OPENSEA_RESPONSE = "opensea_response"


@dataclass(frozen=True, slots=True)
class WalletEligibility:
    is_eligible: bool
    evidence: EligibilityEvidence | None = None
    requires_wallet_switch: bool = False


def validate_stage_windows(metadata: CollectionMetadata) -> None:
    for stage in metadata.stages:
        stage_window(stage)


def stage_window(stage: StageMetadata) -> PhaseWindow:
    try:
        starts_at = (
            parse_rfc3339_ns(stage.start_time) // NANOSECONDS_PER_SECOND if stage.start_time else 0
        )
        ends_at = (
            parse_rfc3339_ns(stage.end_time) // NANOSECONDS_PER_SECOND if stage.end_time else None
        )
        return PhaseWindow(starts_at, ends_at)
    except (OverflowError, ValueError) as error:
        raise MintError("selected stage has an invalid or incompatible time window") from error


def stage_start_nanoseconds(stage: StageMetadata) -> int:
    return parse_rfc3339_ns(stage.start_time) if stage.start_time else 0


def current_unix_timestamp() -> int:
    timestamp = int(time.time())
    if timestamp < 0:
        raise MintError("selected stage has an invalid or incompatible time window")
    return timestamp


def stage_name(stage: StageMetadata) -> str:
    """Use OpenSea's label with a readable type-based fallback."""
    label = (
        " ".join(
            "".join(
                character if character.isprintable() else " " for character in stage.label
            ).split()
        )
        if stage.label
        else ""
    )
    if label:
        return (
            label
            if len(label) <= STAGE_NAME_MAX_LENGTH
            else label[: STAGE_NAME_MAX_LENGTH - 1] + "…"
        )
    return "Public sale" if stage.stage_type == "PUBLIC_SALE" else "Allowlist"


def validate_snapshot_shape(metadata: CollectionMetadata, eligibility: EligibilitySnapshot) -> None:
    if eligibility.drop_kind != metadata.drop_kind or len(eligibility.stages) != len(
        metadata.stages
    ):
        raise _collection_changed()
    for metadata_stage in metadata.stages:
        stage = matching_eligibility_stage(eligibility, metadata_stage)
        if stage.stage_type != metadata_stage.stage_type or stage.kind != metadata_stage.kind:
            raise _collection_changed()


def matching_eligibility_stage(
    eligibility: EligibilitySnapshot, metadata_stage: StageMetadata
) -> StageEligibility:
    matching = [
        stage for stage in eligibility.stages if stage.stage_index == metadata_stage.stage_index
    ]
    if not matching:
        raise MintError("selected stage was not found")
    if len(matching) != 1:
        raise _collection_changed()
    if (
        metadata_stage.uuid is not None
        and matching[0].uuid is not None
        and metadata_stage.uuid != matching[0].uuid
    ):
        raise _collection_changed()
    return matching[0]


def assess_wallet_eligibility(stage: StageEligibility) -> WalletEligibility:
    if stage.stage_type == "PUBLIC_SALE":
        return WalletEligibility(True, EligibilityEvidence.PUBLIC_SALE)
    if stage.eligible_minter_relation is EligibleMinterRelation.LINKED_WALLET:
        return WalletEligibility(False, requires_wallet_switch=True)
    if stage.is_eligible:
        return WalletEligibility(True, EligibilityEvidence.OPENSEA_RESPONSE)
    return WalletEligibility(False)


def eligibility_label(assessment: WalletEligibility) -> str:
    if assessment.evidence is EligibilityEvidence.PUBLIC_SALE:
        return "eligible (public)"
    if assessment.requires_wallet_switch:
        return "requires eligible linked wallet"
    if assessment.evidence is EligibilityEvidence.OPENSEA_RESPONSE:
        return "eligible"
    return "ineligible"


def available_quantity(
    stage: StageMetadata,
    eligibility_stage: StageEligibility,
    eligibility: EligibilitySnapshot,
) -> int | None:
    """Calculate remaining allowance from reported counts; None means unknown."""
    if not assess_wallet_eligibility(eligibility_stage).is_eligible:
        return 0
    total_cap = wallet_mint_limit(stage, eligibility_stage)
    if total_cap == 0:
        return 0
    minted = eligibility.minter_quantity_minted
    if total_cap is None or minted is None:
        return None
    return max(0, total_cap - minted)


def wallet_mint_limit(
    stage: StageMetadata,
    eligibility_stage: StageEligibility,
) -> int | None:
    return next(
        (
            value
            for value in (
                eligibility_stage.eligible_max_total_mintable_by_wallet,
                eligibility_stage.max_total_mintable_by_wallet,
                stage.max_total_mintable_by_wallet,
            )
            if value is not None
        ),
        None,
    )


def validate_mint_limits(
    metadata: CollectionMetadata,
    selected_stage: StageMetadata,
    eligibility: EligibilitySnapshot,
    quantity: int,
) -> None:
    validate_snapshot_shape(metadata, eligibility)
    if selected_stage.stage_type != "PUBLIC_SALE" and drop_unavailable_reason(metadata) is not None:
        raise MintError("OpenSea reports that the drop is not currently mintable")
    stage = matching_eligibility_stage(eligibility, selected_stage)
    if not assess_wallet_eligibility(stage).is_eligible:
        raise MintError("configured wallet is not eligible for the selected stage")
    available = available_quantity(selected_stage, stage, eligibility)
    if available == 0:
        raise MintError("wallet mint limit has been reached for the selected stage")
    if not 0 < quantity <= UINT256_MAX:
        raise MintError("mint quantity must be a positive uint256")
    limit = available if available is not None else wallet_mint_limit(selected_stage, stage)
    if limit is not None and quantity > limit:
        raise MintError("selected quantity exceeds the wallet eligibility limit")


def warn_exhausted_stages(options: Sequence[PhaseOption]) -> None:
    for option in options:
        if (
            option.mint_limit_reached
            and option.minted_quantity is not None
            and option.wallet_mint_limit is not None
        ):
            message = (
                f"OpenSea reports {option.name} is unavailable: "
                f"{option.minted_quantity} of {option.wallet_mint_limit} wallet mints used."
            )
            logging.warn(message)


def public_only_eligibility(metadata: CollectionMetadata) -> EligibilitySnapshot:
    """Keep public stages selectable when optional wallet checks did not complete."""
    return EligibilitySnapshot(
        metadata.drop_kind,
        None,
        tuple(
            StageEligibility(
                stage.kind,
                stage.stage_type,
                stage.stage_index,
                stage.stage_type == "PUBLIC_SALE",
                None,
                stage.max_total_mintable_by_wallet,
                None,
                None,
                None,
                uuid=stage.uuid,
            )
            for stage in metadata.stages
        ),
    )


def drop_unavailable_reason(metadata: CollectionMetadata) -> str | None:
    if metadata.is_disabled:
        return "disabled"
    if metadata.is_minted_out:
        return "minted out"
    return None


def _collection_changed() -> MintError:
    return MintError(
        "collection or selected stage changed while the mint was waiting",
    )


def build_phase_options(
    metadata: CollectionMetadata,
    eligibility: EligibilitySnapshot,
    current_timestamp: int,
) -> list[PhaseOption]:
    validate_snapshot_shape(metadata, eligibility)
    options: list[PhaseOption] = []
    for stage in metadata.stages:
        unavailable_reason = (
            None if stage.stage_type == "PUBLIC_SALE" else drop_unavailable_reason(metadata)
        )
        stage_eligibility = matching_eligibility_stage(eligibility, stage)
        timing = stage_window(stage).execution_kind(current_timestamp)
        max_quantity = (
            0
            if unavailable_reason is not None
            else available_quantity(stage, stage_eligibility, eligibility)
        )
        assessment = assess_wallet_eligibility(stage_eligibility)
        wallet_limit = wallet_mint_limit(stage, stage_eligibility)
        if max_quantity is None:
            max_quantity = wallet_limit
        minted_quantity = eligibility.minter_quantity_minted
        mint_limit_reached = (
            unavailable_reason is None
            and assessment.is_eligible
            and wallet_limit is not None
            and minted_quantity is not None
            and max_quantity == 0
        )
        eligibility_status = (
            f"OpenSea limit reached ({minted_quantity}/{wallet_limit})"
            if mint_limit_reached
            else eligibility_label(assessment)
        )
        selectable = (
            assessment.is_eligible
            and (max_quantity is None or max_quantity > 0)
            and (stage.stage_type == "PUBLIC_SALE" or timing is not ExecutionKind.ENDED)
        )
        state = (
            unavailable_reason
            or {
                ExecutionKind.IMMEDIATE: "active",
                ExecutionKind.SCHEDULED_AT: "upcoming",
                ExecutionKind.ENDED: "ended",
            }[timing]
        )
        if stage.stage_type == "PUBLIC_SALE" and timing is ExecutionKind.ENDED:
            state = "requires on-chain timing check"
        options.append(
            PhaseOption(
                name=stage_name(stage),
                stage_type=stage.stage_type,
                starts_at=stage.start_time or "open",
                ends_at=stage.end_time or "no end",
                state=state,
                eligibility=eligibility_status,
                max_quantity=max_quantity,
                minted_quantity=minted_quantity,
                wallet_mint_limit=wallet_limit,
                mint_limit_reached=mint_limit_reached,
                is_selectable=selectable,
            )
        )
    return options
