"""Strict environment-file configuration for one minting wallet."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from . import logging
from .domain import UINT256_MAX
from .signing import WalletSigner, WalletSignerError

SITE_URL = "https://opensea.io"
GRAPHQL_URL = "https://gql.opensea.io/graphql"
APP_ID = "os2-web"
GWEI_IN_WEI = 1_000_000_000
HTTP_KEEPALIVE_EXPIRY_SECONDS = 30.0
DEFAULT_RPC_REQUEST_TIMEOUT_MS = 10_000
DEFAULT_OPENSEA_ATTEMPTS = 6
DEFAULT_OPENSEA_CALLDATA_ATTEMPTS = 15
OPENSEA_ATTEMPT_LIMITS = (DEFAULT_OPENSEA_ATTEMPTS, 10)
OPENSEA_CALLDATA_ATTEMPT_LIMITS = (DEFAULT_OPENSEA_CALLDATA_ATTEMPTS, 1_000)
KNOWN_SETTINGS = frozenset(
    {
        "WALLET_KEY",
        "RPC_URL",
        "RPC_REQUEST_TIMEOUT_MS",
        "FEE_AUTOMATIC",
        "MAX_FEE_PER_GAS_GWEI",
        "MAX_PRIORITY_FEE_PER_GAS_GWEI",
        "GAS_LIMIT",
        "PUBLIC_MINT_BROADCAST_OFFSET_MS",
        "PENDING_TIMEOUT_SECONDS",
        "RECEIPT_POLL_INTERVAL_MS",
        "OPENSEA_REQUEST_TIMEOUT_MS",
        "OPENSEA_ACTION_REQUEST_TIMEOUT_MS",
        "ELIGIBILITY_REQUEST_TIMEOUT_MS",
        "OPENSEA_ATTEMPTS",
        "OPENSEA_RETRY_INTERVAL_MS",
        "OPENSEA_CALLDATA_ATTEMPTS",
    }
)
_SETTING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """Configuration discovery, parsing, or validation failed."""


@dataclass(frozen=True, slots=True)
class OpenSeaConfig:
    request_timeout_ms: int = 10_000
    action_request_timeout_ms: int = 3_000
    eligibility_request_timeout_ms: int = 5_000
    attempts: int = DEFAULT_OPENSEA_ATTEMPTS
    retry_interval_ms: int = 250
    calldata_attempts: int = DEFAULT_OPENSEA_CALLDATA_ATTEMPTS


@dataclass(frozen=True, slots=True)
class SchedulingConfig:
    public_mint_broadcast_offset_ms: int = 0


@dataclass(frozen=True, slots=True)
class RetryConfig:
    pending_timeout_seconds: int = 20
    poll_interval_ms: int = 250


@dataclass(frozen=True, slots=True)
class FeesConfig:
    automatic: bool
    max_fee_per_gas: int | None = None
    max_priority_fee_per_gas: int | None = None

    @classmethod
    def automatic_mode(cls) -> FeesConfig:
        return cls(True)

    @classmethod
    def manual(cls, max_fee_per_gas: int, max_priority_fee_per_gas: int) -> FeesConfig:
        return cls(False, max_fee_per_gas, max_priority_fee_per_gas)


@dataclass(frozen=True, slots=True)
class AppConfig:
    opensea: OpenSeaConfig
    scheduling: SchedulingConfig
    retry: RetryConfig
    fees: FeesConfig
    rpc_url: str = field(repr=False)
    gas_limit: int
    rpc_request_timeout_ms: int = DEFAULT_RPC_REQUEST_TIMEOUT_MS

    def validate(self) -> None:
        config = self.opensea
        bounds = (
            ("OPENSEA_REQUEST_TIMEOUT_MS", config.request_timeout_ms, 100, 120_000),
            ("OPENSEA_ACTION_REQUEST_TIMEOUT_MS", config.action_request_timeout_ms, 100, 120_000),
            ("ELIGIBILITY_REQUEST_TIMEOUT_MS", config.eligibility_request_timeout_ms, 100, 120_000),
            ("OPENSEA_ATTEMPTS", config.attempts, *OPENSEA_ATTEMPT_LIMITS),
            ("OPENSEA_RETRY_INTERVAL_MS", config.retry_interval_ms, 50, 30_000),
            (
                "OPENSEA_CALLDATA_ATTEMPTS",
                config.calldata_attempts,
                *OPENSEA_CALLDATA_ATTEMPT_LIMITS,
            ),
        )
        for name, value, minimum, maximum in bounds:
            if not minimum <= value <= maximum:
                raise _invalid(name, f"must be between {minimum} and {maximum}")
        if not 100 <= self.rpc_request_timeout_ms <= 120_000:
            raise ConfigError(
                "configuration is invalid: RPC_REQUEST_TIMEOUT_MS is outside the "
                "100-120000 millisecond range"
            )
        offset = self.scheduling.public_mint_broadcast_offset_ms
        if type(offset) is not int or not 0 <= offset <= 60_000:
            raise ConfigError(
                "configuration is invalid: PUBLIC_MINT_BROADCAST_OFFSET_MS requires one "
                "millisecond offset between 0 and 60000"
            )
        if self.gas_limit <= 0 or self.gas_limit > (1 << 64) - 1:
            raise _invalid("GAS_LIMIT", "must be between 1 and 18446744073709551615")
        if (
            not 1 <= self.retry.pending_timeout_seconds <= 86_400
            or not 50 <= self.retry.poll_interval_ms <= 60_000
        ):
            raise ConfigError(
                "configuration is invalid: receipt polling requires a 1-86400 second "
                "timeout and a 50-60000 ms polling interval"
            )
        mode = self.fees
        if not mode.automatic and (
            mode.max_fee_per_gas is None
            or mode.max_priority_fee_per_gas is None
            or mode.max_fee_per_gas == 0
            or mode.max_fee_per_gas < mode.max_priority_fee_per_gas
        ):
            raise ConfigError(
                "configuration is invalid: manual max fee must be nonzero and cover the "
                "priority fee"
            )


@dataclass(frozen=True, slots=True)
class ChainConfig:
    """One verified chain and its sole RPC endpoint."""

    chain_id: int
    rpc_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    app: AppConfig
    source_path: Path
    signer: WalletSigner = field(repr=False)

    @classmethod
    def load(cls) -> LoadedConfig:
        current = Path.cwd()
        executable = Path(sys.argv[0]).resolve()
        return cls.load_from(environment_search_start(current, executable), current)

    @classmethod
    def load_from(cls, start: Path, boundary: Path | None = None) -> LoadedConfig:
        source_path = find_environment_file(start, boundary)
        return cls.from_values(parse_environment_file(source_path), source_path)

    @classmethod
    def from_values(cls, values: dict[str, str], source_path: Path) -> LoadedConfig:
        values = dict(values)
        _validate_known_settings(values)
        wallet_key = _take_required(values, "WALLET_KEY")
        try:
            signer = WalletSigner.from_private_key(wallet_key)
        except WalletSignerError as error:
            raise _invalid("WALLET_KEY", "expected a valid private key") from error
        app = _parse_app_config(values)
        if values:
            raise ConfigError(f".env contains the unknown setting {sorted(values)[0]}")
        return cls(app, source_path, signer)


def environment_search_start(current: Path, executable: Path) -> Path:
    try:
        executable.relative_to(current)
    except ValueError:
        return current
    return executable.parent


def find_environment_file(start: Path, boundary: Path | None = None) -> Path:
    resolved_start = start.resolve()
    directory = resolved_start.parent if resolved_start.is_file() else resolved_start
    search_boundary = (boundary or directory).resolve()
    try:
        directory.relative_to(search_boundary)
    except ValueError as error:
        raise ConfigError(".env search start is outside the active working directory") from error
    candidate_directory = directory
    while True:
        candidate = candidate_directory / ".env"
        if candidate.is_file():
            return candidate
        if candidate_directory == search_boundary:
            break
        candidate_directory = candidate_directory.parent
    raise ConfigError("no .env file was found in the active working directory")


def parse_environment_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"cannot parse .env configuration at {path}") from error
    values: dict[str, str] = {}
    for line_number, source in enumerate(lines, 1):
        line = source.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"cannot parse .env configuration at {path}:{line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _SETTING_NAME.fullmatch(name):
            raise ConfigError(f"cannot parse .env configuration at {path}:{line_number}")
        if name in values:
            raise ConfigError(f".env contains the duplicate setting {name}")
        values[name] = _parse_dotenv_value(raw_value.strip(), path, line_number, values)
    return values


def parse_gwei(value: str, name: str) -> int:
    value = value.strip()
    whole, separator, fraction = value.partition(".")
    if (
        not whole
        or not whole.isascii()
        or not whole.isdigit()
        or (separator and (not fraction.isascii() or not fraction.isdigit()))
        or len(fraction) > 9
    ):
        raise _invalid(name, "expected a nonnegative gwei value with at most 9 decimals")
    whole = whole.lstrip("0") or "0"
    if len(whole) > len(str(UINT256_MAX)):
        raise _invalid(name, "gwei value is too large")
    result = int(whole) * GWEI_IN_WEI + int(fraction.ljust(9, "0"))
    if result > UINT256_MAX:
        raise _invalid(name, "gwei value is too large")
    return result


def _parse_app_config(values: dict[str, str]) -> AppConfig:
    rpc_url = _normalize_url(_take_required(values, "RPC_URL"), "RPC_URL")
    if not _is_allowed_rpc_url(urlsplit(rpc_url)):
        raise _invalid("RPC_URL", "must use HTTPS, except for an HTTP loopback test endpoint")
    opensea_defaults = OpenSeaConfig()
    scheduling_defaults = SchedulingConfig()
    retry_defaults = RetryConfig()
    app = AppConfig(
        opensea=OpenSeaConfig(
            request_timeout_ms=_take_uint(
                values, "OPENSEA_REQUEST_TIMEOUT_MS", opensea_defaults.request_timeout_ms
            ),
            action_request_timeout_ms=_take_uint(
                values,
                "OPENSEA_ACTION_REQUEST_TIMEOUT_MS",
                opensea_defaults.action_request_timeout_ms,
            ),
            eligibility_request_timeout_ms=_take_uint(
                values,
                "ELIGIBILITY_REQUEST_TIMEOUT_MS",
                opensea_defaults.eligibility_request_timeout_ms,
            ),
            attempts=_take_bounded_attempts(
                values,
                "OPENSEA_ATTEMPTS",
                OPENSEA_ATTEMPT_LIMITS,
            ),
            retry_interval_ms=_take_uint(
                values, "OPENSEA_RETRY_INTERVAL_MS", opensea_defaults.retry_interval_ms
            ),
            calldata_attempts=_take_bounded_attempts(
                values,
                "OPENSEA_CALLDATA_ATTEMPTS",
                OPENSEA_CALLDATA_ATTEMPT_LIMITS,
            ),
        ),
        scheduling=SchedulingConfig(
            public_mint_broadcast_offset_ms=_take_uint(
                values,
                "PUBLIC_MINT_BROADCAST_OFFSET_MS",
                scheduling_defaults.public_mint_broadcast_offset_ms,
            ),
        ),
        retry=RetryConfig(
            pending_timeout_seconds=_take_uint(
                values, "PENDING_TIMEOUT_SECONDS", retry_defaults.pending_timeout_seconds
            ),
            poll_interval_ms=_take_uint(
                values, "RECEIPT_POLL_INTERVAL_MS", retry_defaults.poll_interval_ms
            ),
        ),
        fees=_parse_fees(values),
        rpc_url=rpc_url,
        gas_limit=_parse_uint(_take_required(values, "GAS_LIMIT"), "GAS_LIMIT"),
        rpc_request_timeout_ms=_take_uint(
            values, "RPC_REQUEST_TIMEOUT_MS", DEFAULT_RPC_REQUEST_TIMEOUT_MS
        ),
    )
    app.validate()
    return app


def _parse_dotenv_value(
    raw: str, path: Path, line_number: int, parsed_values: dict[str, str]
) -> str:
    output: list[str] = []
    strong_quote = weak_quote = escaped = expecting_end = False
    index = 0
    while index < len(raw):
        character = raw[index]
        if expecting_end:
            if character in " \t":
                index += 1
                continue
            if character == "#":
                break
            raise ConfigError(f"cannot parse .env configuration at {path}:{line_number}")
        if escaped:
            escapes = {"\\": "\\", "'": "'", '"': '"', "$": "$", " ": " ", "n": "\n"}
            if character not in escapes:
                raise ConfigError(f"cannot parse .env configuration at {path}:{line_number}")
            output.append(escapes[character])
            escaped = False
        elif strong_quote:
            if character == "'":
                strong_quote = False
            else:
                output.append(character)
        elif character == "$":
            substitution, index = _parse_substitution(raw, index + 1, path, line_number)
            output.append(os.environ.get(substitution, parsed_values.get(substitution, "")))
            continue
        elif weak_quote:
            if character == '"':
                weak_quote = False
            elif character == "\\":
                escaped = True
            else:
                output.append(character)
        elif character == "'":
            strong_quote = True
        elif character == '"':
            weak_quote = True
        elif character == "\\":
            output.append(character)
        elif character in " \t":
            expecting_end = True
        else:
            output.append(character)
        index += 1
    if strong_quote or weak_quote or escaped:
        raise ConfigError(f"cannot parse .env configuration at {path}:{line_number}")
    return "".join(output)


def _parse_substitution(raw: str, start: int, path: Path, line_number: int) -> tuple[str, int]:
    if start < len(raw) and raw[start] == "{":
        end = raw.find("}", start + 1)
        if end < 0:
            raise ConfigError(f"cannot parse .env configuration at {path}:{line_number}")
        return raw[start + 1 : end], end + 1
    end = start
    while end < len(raw) and (raw[end].isalnum() or raw[end] == "_"):
        end += 1
    return raw[start:end], end


def _parse_fees(values: dict[str, str]) -> FeesConfig:
    automatic = _parse_bool(_take_required(values, "FEE_AUTOMATIC"), "FEE_AUTOMATIC")
    if automatic:
        if "MAX_FEE_PER_GAS_GWEI" in values or "MAX_PRIORITY_FEE_PER_GAS_GWEI" in values:
            raise _invalid(
                "FEE_AUTOMATIC", "remove manual fee settings when automatic fees are enabled"
            )
        return FeesConfig.automatic_mode()
    return FeesConfig.manual(
        parse_gwei(_take_required(values, "MAX_FEE_PER_GAS_GWEI"), "MAX_FEE_PER_GAS_GWEI"),
        parse_gwei(
            _take_required(values, "MAX_PRIORITY_FEE_PER_GAS_GWEI"),
            "MAX_PRIORITY_FEE_PER_GAS_GWEI",
        ),
    )


def _validate_known_settings(values: dict[str, str]) -> None:
    unknown = sorted(values.keys() - KNOWN_SETTINGS)
    if unknown:
        raise ConfigError(f".env contains the unknown setting {unknown[0]}")


def _take_required(values: dict[str, str], name: str) -> str:
    value = values.pop(name, None)
    if value is None or not value.strip():
        raise ConfigError(f".env is missing the required setting {name}")
    return value


def _take_uint(values: dict[str, str], name: str, default: int, *, bits: int = 64) -> int:
    value = values.pop(name, None)
    return default if value is None else _parse_uint(value, name, bits=bits)


def _take_bounded_attempts(values: dict[str, str], name: str, limits: tuple[int, int]) -> int:
    minimum, maximum = limits
    configured = _take_uint(values, name, minimum, bits=32)
    if configured < minimum:
        logging.warn(f"{name}={configured} is below the minimum of {minimum}. Using {minimum}.")
        return minimum
    if configured > maximum:
        logging.warn(f"{name}={configured} is above the maximum of {maximum}. Using {maximum}.")
        return maximum
    return configured


def _parse_uint(value: str, name: str, *, bits: int = 64) -> int:
    if not value.isascii() or not value.isdigit():
        raise _invalid(name, "expected an unsigned integer")
    value = value.lstrip("0") or "0"
    if len(value) > len(str((1 << bits) - 1)):
        raise _invalid(name, "expected an unsigned integer")
    result = int(value)
    if result >= 1 << bits:
        raise _invalid(name, "expected an unsigned integer")
    return result


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise _invalid(name, "expected true or false")


def _normalize_url(value: str, name: str) -> str:
    try:
        if any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            raise ValueError
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname or parsed.port == 0:
            raise ValueError
    except ValueError as error:
        raise _invalid(name, "expected an absolute URL with a valid host and port") from error
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment)
    )


def _is_allowed_rpc_url(url: SplitResult) -> bool:
    if url.scheme == "https":
        return bool(url.hostname)
    if url.scheme != "http" or not url.hostname:
        return False
    host = url.hostname.lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _invalid(name: str, reason: str) -> ConfigError:
    return ConfigError(f".env setting {name} is invalid: {reason}")
