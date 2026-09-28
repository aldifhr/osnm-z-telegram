# osnm-z-telegram-bot

Telegram front-end for [`osnm-z`](https://github.com/zunmax/osnm-z) — a single-wallet
Python CLI for minting ERC-721 NFTs from OpenSea SeaDrop drops.

Send an OpenSea collection link, pick a quantity, and the transaction is signed and
broadcast. No browser, no wallet extension, no scraping of terminal output.

---

## What this is (and is not)

This repo contains **only the Telegram layer**. All minting logic — SIWE sign-in,
eligibility resolution, calldata construction, signing, broadcast, receipt polling —
is the upstream `osnm-z` library, imported and driven directly. No terminal output is
ever parsed, and no minting behaviour is reimplemented here.

| Concern | Owner |
|---|---|
| Mint stages, eligibility, calldata, signing, broadcast, receipt | [`zunmax/osnm-z`](https://github.com/zunmax/osnm-z) |
| Chain auto-detection across 6 chains | this repo (`bot.py`) |
| Live `totalSupply()` progress + price/gas/balance breakdown | this repo |
| Telegram UI, key handling, confirmations | this repo |

## Features

- **Link-to-mint.** Paste an OpenSea collection URL, slug, or contract address. No
  command needed. Ordinary chat text is ignored (see *Pre-filter* below).
- **Chain auto-detection.** Probes candidate RPCs and matches the collection's real
  chain. A chain mismatch retries the next endpoint instead of failing.
- **Full cost breakdown before sending.** Per-NFT price, subtotal, gas estimate from
  a live `eth_maxPriorityFeePerGas`, and a balance-sufficiency check.
- **Live supply.** `ERC721.totalSupply()` read on-chain, so sold-out detection does
  not depend on a cached API flag.
- **One-tap fast path.** When exactly one stage is mintable, the stage screen is
  skipped and tapping a quantity broadcasts.
- **Private key management.** Owner-only; key messages are deleted on receipt, never
  echoed, never logged, written atomically with mode 0600.

## Requirements

- Linux with `systemd`
- Python 3.12–3.13 (upstream constraint) via [`uv`](https://docs.astral.sh/uv/)
- An `osnm-z` checkout at `../osnm-z` (see [Install](#install))
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

Verified on: Python 3.12.13, `python-telegram-bot` 21.11, `httpx` 0.28.1,
`orjson` 3.11.9, `uv` 0.11.28, git 2.43.0, Robinhood chain (4663), Base (8453).

## Install

```bash
# 1. Get the upstream mint engine
git clone https://github.com/zunmax/osnm-z.git
cd osnm-z
uv sync --frozen --python 3.12
cp .env.example .env
chmod 600 .env            # holds WALLET_KEY; a plain cp leaves it world-readable

# 2. Fill in WALLET_KEY and RPC_URL for your target chain
nano .env

# 3. Get this Telegram layer
cd ..
git clone <this-repo> osnm-z-telegram-bot
cd osnm-z-telegram-bot

# 4. Bot credentials — kept in a separate file, see "Two env files"
cat > bot.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_ALLOWED_CHAT_ID=your-numeric-telegram-id
# Optional: extra HTTPS RPCs, comma separated. Only https:// is accepted.
OSNM_EXTRA_RPCS=https://mainnet.base.org
EOF
chmod 600 bot.env
```

`bot.py` expects to sit inside the `osnm-z` checkout at `bot/`, so that
`../src` resolves the upstream package:

```bash
uv pip install -r requirements-bot.txt   # into the osnm-z venv
uv pip install pytest                    # test-only
cp -r osnmzbot osnm-z/bot/
mv bot.py supply.py test_bot.py run-bot.sh requirements-bot.txt osnm-z/bot/
cp systemd/osnm-z-bot.service /etc/systemd/system/
sed -i "s#/opt/osnm-z#$PWD/osnm-z#g" /etc/systemd/system/osnm-z-bot.service
systemctl daemon-reload && systemctl enable --now osnm-z-bot
```

### Two env files, on purpose

`osnm_z.config._validate_known_settings` **rejects any key it does not recognise**.
Telegram secrets therefore cannot live in the app `.env` next to `WALLET_KEY` — the
bot reads `bot/.env` (sourced into the process environment by `run-bot.sh`) and
never exposes it to the mint library.

```
osnm-z/.env        WALLET_KEY, RPC_URL, gas and retry settings  (0600)
osnm-z/bot/.env    TELEGRAM_BOT_TOKEN, chat id, extra RPCs     (0600)
```

## Windows

`bot.py` is pure Python and runs unchanged on Windows. Only the launcher and
autostart differ, because there is no systemd and no mode 0600.

```powershell
git clone https://github.com/zunmax/osnm-z.git C:\src\osnm-z
cd C:\src\osnm-z
uv sync --frozen --python 3.12
copy .env.example .env            # set WALLET_KEY and RPC_URL

cd C:\src
git clone https://github.com/aldifhr/osnm-z-telegram.git
cd osnm-z-telegram
powershell -ExecutionPolicy Bypass -File .\setup.ps1     # deps, ACLs, autostart
```

`setup.ps1` creates `C:\src\osnm-z\bot\.env` from `bot.env.example`, installs
`requirements-bot.txt`, registers a Task Scheduler task, and locks the env files
down. Then:

```powershell
.\run-bot.ps1            # foreground
.\run-bot.ps1 -Check     # validate config and exit
.\run-bot.cmd            # double-clickable
Get-Content C:\src\osnm-z\bot\logs\bot.log -Tail 50
```

Task Scheduler has no equivalent of the `systemd` hardening, so the ACLs matter
more on Windows, not less:

| Linux | Windows |
|---|---|
| `.env` at mode 0600 | NTFS ACL: current user + SYSTEM only |
| `ProtectSystem=full` | not available; relies on the file ACLs |
| `journalctl -u` | `bot\logs\bot.log`, rotated at 2 MB x 3 |
| `systemctl restart` | `Restart-ScheduledTask`, or `Stop-ScheduledTask` then `Start-ScheduledTask` |

`icacls` is used instead of the ACL cmdlets because it exists on every edition,
including Home. `setup.ps1` aborts if `BUILTIN\Users` still has access after the
lock-down, so a misconfigured ACL fails loudly rather than silently exposing the
key.

Three things to keep in mind:

- `os.chmod` is a no-op on Windows, so `bot.py` cannot enforce 0600 itself. The
  key file is only as private as its directory: keep the checkout out of
  `C:\Users\Public`, OneDrive, and any shared path.
- A logon-triggered task runs in the user context, where `uv` and the venv live.
  `-TaskAtStartup` registers an at-boot task as SYSTEM instead, which is headless
  but cannot see a user-scoped venv.
- In-memory state (`awaiting_key`, confirm nonces) is lost when the task restarts,
  same as on Linux.

## Layout

`bot.py` is a facade over the `osnmzbot` package. One concern per module, and
the modules that can be reused from another front-end are free of Telegram
imports:

| Module | Concern | Reusable outside a bot |
|---|---|---|
| `osnmzbot/config` | app paths, chain registry, config loading | yes |
| `osnmzbot/locator` | is this message a collection reference? | yes |
| `osnmzbot/render` | message text and price formatting | yes, no I/O |
| `osnmzbot/onchain` | direct `eth_call` reads | yes |
| `osnmzbot/models` | the in-flight mint `Session` | yes |
| `osnmzbot/locks` | process-wide state | yes |
| `osnmzbot/text` | user-facing copy | yes |
| `osnmzbot/wallet` | reading the wallet, persisting a key | Telegram-free, but touches `.env` |
| `osnmzbot/flows` | session lifecycle | needs Telegram |
| `osnmzbot/handlers` | command and callback handlers | needs Telegram |
| `osnmzbot/app` | logging setup, handler registration | entry point |

```python
from osnmzbot.locator import looks_like_locator
from osnmzbot.config import load, app_dir
```

Two details worth knowing if you extend this:

- `app_dir()` is a function, not the `APP_DIR` constant. A constant is frozen at
  import time, which makes redirecting the env files in a test impossible
  without patching every module that reads them. Use `set_app_dir()` to point
  the bot at a scratch root and `set_app_dir(None)` to restore.
- `config.APP_DIR` walks up three levels from `osnmzbot/config.py`. Getting that
  wrong points the mint library at `bot/.env`, which it rejects for holding
  `TELEGRAM_*` keys, so the bot fails with a config error that looks unrelated
  to the path. A test asserts the right directory.

## Commands

| Command | Effect |
|---|---|
| `<opensea link>` | Start a mint session, auto-detect the chain |
| `/mint <link>` | Same, explicit |
| `/wallet` | Active address plus balance on every reachable chain |
| `/wallet set <key>` | Replace the private key (command message is deleted) |
| `/wallet clear` | Empty the key — requires an inline confirmation tap |
| `/doctor` | Config, wallet, RPC, and OpenSea client checks |
| `/cancel` | Abandon the current session |

## Owner-only enforcement

A `TypeHandler` runs in group `-1` and inspects every update before any other
handler. Updates from a chat id other than `TELEGRAM_ALLOWED_CHAT_ID` get
`Unauthorized` and raise `ApplicationHandlerStop`, so no other command can run.

In a group, the guard checks `update.effective_chat.id`, which is the group id for
every member. Add the bot to a private chat to scope it to you personally, or set
`TELEGRAM_ALLOWED_CHAT_ID` to the group id if you want group-wide access.

## Pre-filter

Automatic triggering must not fire on ordinary conversation. `looks_like_locator()`
is a cheap pre-filter; the upstream `parse_collection_locator()` remains the real
authority.

| Input | Triggers |
|---|---|
| `https://opensea.io/collection/<slug>[/...]` | yes |
| `0x` + 40 hex | yes |
| `some-collection_2`, `cryptopunks` | yes |
| `halo`, `gas`, `cek` | no |
| `https://opensea.io/`, `/item/<id>`, `http://` | no |
| `berapa gasnya?` | no |

Bare slugs must be at least 5 characters and contain a digit, `-`, or `_`. A short
slug is indistinguishable from a word, so `abc` is reachable only via `/mint abc`.
`test_slug_shape_matches_library` asserts `SLUG_SHAPE` stays byte-identical to
`opensea_protocol._SLUG`, so an upstream change fails the tests instead of silently
widening what triggers a mint.

## Private key handling

A key pasted into Telegram has already passed Telegram's servers and entered chat
history. Nothing in this code can undo that. The mitigations reduce the blast radius:

- The message is deleted first, before validation or any other work.
- The key is never echoed in a reply, and never written to a log.
- `apply_wallet_key()` never includes the key in any error path.
- Invalid input is still deleted, then rejected.
- Writes are atomic: `O_CREAT | 0600` from the start, `fsync`, then `os.replace`,
  so there is no window where the key is world-readable and no truncated key on crash.
- `/wallet clear` uses a single-use nonce. A stale confirmation button tapped again,
  or after a new key was set, is refused. It also refuses when the key is already
  empty, so a replay cannot re-wipe a replacement.

For zero chat trace, edit `osnm-z/.env` on the host instead.

## Sold-out and supply detection

Three signals, weakest to strongest:

1. `is_disabled` — OpenSea drop metadata.
2. `is_minted_out` — OpenSea drop metadata; gates non-public stages.
3. `totalSupply()` — `eth_call` on the NFT contract, selector `0x18160ddd`.

The third is what the bot displays, because OpenSea's availability flag is a cache
that can lag a sold-out drop while `totalSupply()` cannot. Display thresholds:
`< 90%` normal, `>= 90%` "hampir habis", `100%` "SOLD OUT".

`totalSupply()` is a collection-wide figure, not per-stage. Accurate per-stage
counts would need an indexer; see `supply.py` for why log scanning is not viable
on a public RPC.

## Testing

```bash
uv run --frozen --no-sync python -m pytest bot/test_bot.py -q
# 84 passed
```

`uv sync --frozen` installs the upstream lockfile only. `python-telegram-bot` and
`pytest` are **not** in it — install from `requirements-bot.txt` first, or the bot
fails at import with `ModuleNotFoundError: No module named 'telegram'`.

Verified end-to-end from clean `git clone`s of both repos on Python 3.12.13.

The test suite runs against a real checkout, not a mock: `uv sync --frozen`,
then `pytest bot/test_bot.py` gives 94 passed on a clean clone of both repos.

`test_bot.py` builds sessions from the upstream dataclasses field-for-field rather
than mocking, so an upstream shape change fails the tests instead of crashing in
production. Destructive paths are exercised against a `tempfile` copy with `APP_DIR`
redirected, so no test can touch a real `.env`.

## Operational notes

- `systemd` unit hardens with `ProtectSystem=full`, `ProtectHome=read-only`, and
  `ReadWritePaths` scoped to the app directory. Because `ProtectHome` makes
  `/root/.cache` unwritable, `run-bot.sh` points `UV_CACHE_DIR` and `TMPDIR` inside
  the app tree, and resolves `uv` explicitly since `systemd` does not inherit the
  interactive shell `PATH`.
- In-memory state (`awaiting_key`, confirm nonces, the mint lock) is lost on restart.
  A key pasted more than a minute or two after `/wallet set` is ignored and remains
  in the chat.
- Minting uses one wallet per run. The upstream tool has no multi-wallet mode.
- Timed/Dutch auctions are not supported by either layer: `value` is computed once
  from `getPublicDrop()` and never varies with time.

## Sources

- Upstream engine — <https://github.com/zunmax/osnm-z> at commit
  `b001c82bde34e408ab6f21e6e8be470ca2f189ab` ("Fixed bugs & simplified the code")
- OpenSea SeaDrop singleton — `0x00005EA00Ac477B1030CE78506496e8C2dE24bf5`
- ERC-721 `totalSupply()` selector — `0x18160ddd`
- ERC-721 `Transfer(address,address,uint256)` topic —
  `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`
- Robinhood chain RPC — `https://rpc.mainnet.chain.robinhood.com` (chain id 4663)
- OpenSea API v2 — `https://api.opensea.io/api/v2/`
- OpenSea GraphQL — `https://gql.opensea.io/`
- `python-telegram-bot` — <https://python-telegram-bot.org> v21.11
- `uv` — <https://docs.astral.sh/uv/>

## Security notes for operators

- **Revoke any bot token that has been pasted into a chat.** A token in chat history
  is a compromised token; the owner-id guard limits blast radius but does not
  substitute for rotation.
- Use a dedicated mint wallet holding only the amount intended. A well-known
  burnable key such as `0x1111…11` is auto-detected by sweepers.
- Upstream explicitly recommends a secondary wallet: only one wallet pays mint price
  and gas per run.

## License

MIT, matching upstream `osnm-z`. See `LICENSE`.
