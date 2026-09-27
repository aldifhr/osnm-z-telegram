"""Shared SeaDrop V1 identifiers and calldata stage extraction."""

from eth_utils.address import to_checksum_address

from .errors import MintError
from .opensea_protocol import MintTransactionAction

OPENSEA_SEADROP_ADDRESS = to_checksum_address("0x00005EA00Ac477B1030CE78506496e8C2dE24bf5")
ERC721_PUBLIC_MINT_SELECTOR = bytes.fromhex("161ac21f")
_PRIVATE_MINT_SELECTORS = (bytes.fromhex("4b61cd6f"), bytes.fromhex("4300a4e6"))
_SEADROP_ADDRESS_LOWER = OPENSEA_SEADROP_ADDRESS.lower()
# Bind the integer decoder once to avoid repeated method binding on the hot path.
_uint_from_bytes = int.from_bytes


def mint_action_stage_index(action: MintTransactionAction) -> int:
    """Read only the stage identity from a supported canonical SeaDrop call."""
    if action.target != OPENSEA_SEADROP_ADDRESS and action.target.lower() != _SEADROP_ADDRESS_LOWER:
        raise MintError("OpenSea returned an unsupported mint contract; cannot identify its stage")
    calldata = action.calldata
    selector = calldata[:4]
    if selector in _PRIVATE_MINT_SELECTORS:
        # Four arguments precede MintParams; dropStageIndex is its fifth ABI word.
        if len(calldata) < 292:
            raise MintError("OpenSea mint data is too short to read its stage index")
        return _uint_from_bytes(calldata[260:292], "big")
    if selector == ERC721_PUBLIC_MINT_SELECTOR:
        if len(calldata) < 132:
            raise MintError("OpenSea public mint data is incomplete")
        return 0
    raise MintError("OpenSea returned an unsupported mint function; cannot identify its stage")
