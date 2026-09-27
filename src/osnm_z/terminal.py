"""Interactive collection, phase, and quantity selection."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import logging
from .domain import UINT256_MAX


class TerminalError(RuntimeError):
    """Terminal interaction failed."""


class Cancelled(TerminalError):
    def __init__(self) -> None:
        super().__init__("mint setup was cancelled")


class NoSelectablePhase(TerminalError):
    def __init__(self) -> None:
        super().__init__("OpenSea reported no selectable phase for this wallet")


@dataclass(frozen=True, slots=True)
class PhaseOption:
    name: str
    stage_type: str
    starts_at: str
    ends_at: str
    state: str
    eligibility: str
    max_quantity: int | None
    minted_quantity: int | None
    wallet_mint_limit: int | None
    mint_limit_reached: bool
    is_selectable: bool


def prompt_collection_locator() -> str:
    logging.section("Collection selection")
    logging.input_message("Paste an OpenSea slug, collection or mint URL, or NFT contract address.")
    while True:
        value = _prompt("Mint URL or contract: ")
        if value:
            return value
        logging.warn("Collection input cannot be empty.")


def select_phase(options: list[PhaseOption]) -> int:
    _print_phase_options(options)
    selectable = [index for index, option in enumerate(options) if option.is_selectable]
    if not selectable:
        raise NoSelectablePhase
    if len(options) == 1 and len(selectable) == 1:
        only = selectable[0]
        logging.success(f"Selected {options[only].name} automatically.")
        return only
    logging.input_message("Choose one eligible phase number, or q to cancel.")
    while True:
        value = _prompt("Phase: ")
        if value.lower() == "q":
            raise Cancelled
        selection = parse_phase_selection(value, options)
        if selection is not None:
            return selection
        logging.warn("Select one eligible phase number.")


def prompt_quantity(option: PhaseOption) -> int:
    logging.section("Quantity")
    logging.info(f"Selected phase: {option.name}.")
    if option.minted_quantity is None:
        logging.warn("Minted count unavailable; remaining allowance is unverified.")
    return _prompt_number("Quantity: ", option.max_quantity)


def parse_phase_selection(value: str, options: list[PhaseOption]) -> int | None:
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        return None
    option_index = int(value) - 1
    if not 0 <= option_index < len(options) or not options[option_index].is_selectable:
        return None
    return option_index


def _print_phase_options(options: list[PhaseOption]) -> None:
    for index, option in enumerate(options, 1):
        heading = logging.styled(f"{index:>2}. {option.name}", logging.Style.CYAN_BOLD)
        stage_type = logging.styled(_stage_type_name(option.stage_type), logging.Style.MAGENTA_BOLD)
        state_style = (
            logging.Style.GREEN_BOLD
            if option.state == "active"
            else logging.Style.YELLOW_BOLD
            if option.state == "upcoming"
            else logging.Style.DARK_GREY
        )
        state = logging.styled(option.state, state_style)
        eligibility = logging.styled(
            option.eligibility,
            logging.Style.GREEN if option.is_selectable else logging.Style.RED,
        )
        availability = logging.styled(
            "available" if option.is_selectable else "unavailable",
            logging.Style.GREEN_BOLD if option.is_selectable else logging.Style.RED_BOLD,
        )
        separator = logging.styled(" | ", logging.Style.DARK_GREY)
        print("  " + separator.join((heading, stage_type, state, eligibility, availability)))
        print(
            logging.styled(
                f"      start={option.starts_at} | end={option.ends_at}",
                logging.Style.DARK_GREY,
            )
        )
        print()


def _stage_type_name(stage_type: str) -> str:
    return "Public" if stage_type == "PUBLIC_SALE" else "Allowlist"


def _prompt_number(label: str, maximum: int | None) -> int:
    instruction = (
        "Enter a positive whole number"
        if maximum is None
        else f"Enter a whole number from 1 to {maximum}"
    )
    logging.input_message(f"{instruction} (default: 1), or q to cancel.")
    while True:
        value = _prompt(label)
        if value.lower() == "q":
            raise Cancelled
        if not value:
            return 1
        if not value.isascii() or not value.isdigit():
            logging.warn(f"{instruction} using digits 0-9.")
            continue
        try:
            parsed = int(value)
        except ValueError:
            pass
        else:
            if 1 <= parsed <= UINT256_MAX and (maximum is None or parsed <= maximum):
                return parsed
            if parsed > UINT256_MAX:
                logging.warn("Quantity exceeds the Ethereum uint256 range.")
                continue
        logging.warn(f"{instruction}.")


def _prompt(label: str) -> str:
    try:
        return input(logging.styled(label, logging.Style.GREEN_BOLD)).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise Cancelled from error
    finally:
        if not sys.stdout.isatty():
            print(flush=True)
