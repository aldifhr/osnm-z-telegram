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
| Chain auto-detection across 6 networks | `osnmzbot/config.py` |
| Live `totalSupply()` progress, price/gas/balance breakdown | `osnmzbot/onchain.py`, `osnmzbot/render.py` |
| `/status`: receipt, confirmations, revert vs dropped | `osnmzbot/status.py`, `osnmzbot/onchain.py` |
| Dry run: calldata + `eth_call`, never signs | `osnmzbot/simulate.py` |
| Telegram UI, key handling, confirmations | `osnmzbot/commands.py`, `osnmzbot/callbacks.py` |

## Features

- **Link-to-mint.** Paste an OpenSea collection URL, slug, or contract address. No
  command needed. Ordinary chat text is ignored (see [Pre-filter](#pre-filter)).
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

- Linux with `systemd`, **or** Windows 10+ ([Windows](#windows))
- Python 3.12–3.13 (upstream constraint) via [`uv`](https://docs.astral.sh/uv/)
- An `osnm-z` checkout — this repo's files go into its `bot/` directory
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

Verified on: Python 3.12.13, `python-telegram-bot` 21.11, `httpx` 0.28.1,
`orjson` 3.11.9, `uv` 0.11.28, git 2.43.0. Exercised against Robinhood chain (4663)
and Base (8453).

## Install (Linux)

```bash
# 1. Get the upstream mint engine
git clone https://github.com/zunmax/osnm-z.git
cd osnm-z
uv sync --frozen --python 3.12
cp .env.example .env
chmod 600 .env            # holds WALLET_KEY; a plain cp leaves it 0644

# 2. Fill in WALLET_KEY and RPC_URL for your target chain
nano .env

# 3. Get this Telegram layer
cd ..
git clone https://github.com/aldifhr/osnm-z-telegram.git

# 4. Bot credentials — kept in a separate file, see "Two env files" below
cd osnm-z-telegram
cat > bot.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_ALLOWED_CHAT_ID=your-numeric-telegram-id
# Optional: extra HTTPS RPCs, comma separated. Only https:// is accepted.
OSNM_EXTRA_RPCS=https://mainnet.base.org
EOF
chmod 600 bot.env
```

Now assemble the install. `bot/` does not exist in an upstream clone, so create it
first — `cp -r osnmzbot osnm-z/bot/` fails otherwise:

```bash
uv pip install -r requirements-bot.txt    # into the osnm-z venv
uv pip install pytest                     # test-only, for the suite below

mkdir -p ../osnm-z/bot
cp -r osnmzbot ../osnm-z/bot/
cp bot.py supply.py test_bot.py run-bot.sh ../osnm-z/bot/

# bot/.env is the file the launcher reads; bot.env is the template you filled in
cp bot.env ../osnm-z/bot/.env
chmod 600 ../osnm-z/bot/.env

cp systemd/osnm-z-bot.service /etc/systemd/system/
sed -i "s#/opt/osnm-z#$(cd ../osnm-z && pwd)#g" /etc/systemd/system/osnm-z-bot.service
systemctl daemon-reload && systemctl enable --now osnm-z-bot
```

Verify:

```bash
cd ../osnm-z
uv run --frozen --no-sync python -m pytest bot/ -q   # 120 passed
systemctl status osnm-z-bot
tail -f bot/logs/bot.log
```

`bot.py` must end up at `<checkout>/bot/bot.py` with `osnmzbot/` beside it, so that
`../src` resolves the upstream package.

### Two env files, on purpose

`osnm_z.config._validate_known_settings` **rejects any key it does not recognise**.
Telegram secrets therefore cannot live in the app `.env` next to `WALLET_KEY` — the
bot reads `bot/.env` (sourced into the process environment by `run-bot.sh`) and
never exposes it to the mint library. This split is why the launcher exists at all,
and why `config.load()` pins the path instead of using `LoadedConfig.load()`: the
latter starts searching from `sys.argv[0]`, which under the bot is `bot/bot.py`, and
would find `bot/.env` and reject it.

```
osnm-z/.env        WALLET_KEY, RPC_URL, gas and retry settings  (0600)
osnm-z/bot/.env    TELEGRAM_BOT_TOKEN, chat id, extra RPCs     (0600)
```

## Windows

The bot is pure Python and runs unchanged. Only the launcher and autostart differ,
because there is no `systemd` and no mode 0600.

```powershell
git clone https://github.com/zunmax/osnm-z.git C:\src\osnm-z
cd C:\src\osnm-z
uv sync --frozen --python 3.12
copy .env.example .env            # set WALLET_KEY and RPC_URL

cd C:\src
git clone https://github.com/aldifhr/osnm-z-telegram.git
cd osnm-z-telegram
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -OsnmZPath C:\src\osnm-z
```

`-OsnmZPath` is required and is the directory holding `src\` and `uv.lock`. The
script refuses a path that is not an osnm-z checkout rather than half-installing.
It then:

1. installs `requirements-bot.txt` into the checkout's venv,
2. creates `<checkout>\bot\` and copies `osnmzbot\`, `bot.py`, `supply.py`,
   `test_bot.py`, and the launchers into it,
3. creates `<checkout>\.env` and `<checkout>\bot\.env` from the examples,
   without overwriting either if it already exists,
4. locks both env files to your account and SYSTEM via `icacls`,
5. runs `run-bot.ps1 -Check` against the real install,
6. registers a Task Scheduler task.

Steps 4, 5, and 6 warn and continue when their Windows-only tooling is missing,
so the script is also usable for inspection on other platforms. `-SkipTask` stops
after step 5; `-Uninstall` removes the task and needs no `-OsnmZPath`.

`setup.ps1` deliberately leaves the Wallet key alone: create `bot\.env` from
`bot.env.example`, fill in the token and chat id, then start.

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
| `systemctl restart` | `Stop-ScheduledTask` then `Start-ScheduledTask` |
| `setup.ps1 -Uninstall` | not applicable; `setup.ps1 -Uninstall` removes the task |

`icacls` is used instead of the ACL cmdlets because it exists on every edition,
including Home. `setup.ps1` aborts if `BUILTIN\Users` still has access after the
lock-down, so a misconfigured ACL fails loudly rather than silently exposing the
key.

Three things to keep in mind:

- `os.chmod` is a no-op on Windows, so the bot cannot enforce 0600 itself. The key
  file is only as private as its directory: keep the checkout out of
  `C:\Users\Public`, OneDrive, and any shared path.
- A logon-triggered task runs in the user context, where `uv` and the venv live.
  `-TaskAtStartup` registers an at-boot task as SYSTEM instead, which is headless
  but cannot see a user-scoped venv.
- In-memory state (`awaiting_key`, confirm nonces) is lost when the task restarts,
  same as on Linux.

## Layout

`bot.py` is a facade over the `osnmzbot` package. One concern per module, and the
modules reusable from another front-end import no Telegram types at all:

| Module | Concern | Telegram-free |
|---|---|---|
| `osnmzbot/locks` | process-wide state | yes |
| `osnmzbot/text` | user-facing copy | yes |
| `osnmzbot/onchain` | direct `eth_call` reads | yes |
| `osnmzbot/models` | the in-flight mint `Session` | yes |
| `osnmzbot/locator` | is this message a collection reference? | yes |
| `osnmzbot/app` | logging setup, handler registration | yes |
| `osnmzbot/wallet` | reading the wallet, persisting a key | yes, but touches `.env` |
| `osnmzbot/config` | app paths, chain registry, config loading | yes |
| `osnmzbot/render` | message text and price formatting | yes, no I/O |
| `osnmzbot/simulate` | dry-run a mint, never signs | yes, no Telegram |
| `osnmzbot/status` | tx receipt tracking and reporting | yes, no Telegram |
| `osnmzbot/flows` | session lifecycle | no |
| `osnmzbot/commands` | `/mint`, `/wallet`, `/status`, … | no |
| `osnmzbot/callbacks` | inline buttons, mint broadcast | no |
| `osnmzbot/handlers` | wallet commands, owner guard, re-exports | no |

```python
from osnmzbot.locator import looks_like_locator
from osnmzbot.config import load, app_dir
from osnmzbot.render import price_table
```

Three details worth knowing before extending this:

- `app_dir()` is a function, not the `APP_DIR` constant. A constant is frozen at
  import time, which makes redirecting the env files in a test impossible without
  patching every module that reads them. Use `set_app_dir()` to point the bot at a
  scratch root and `set_app_dir(None)` to restore.
- `config.APP_DIR` walks up three levels from `osnmzbot/config.py`. Getting that
  wrong points the mint library at `bot/.env`, which it rejects for holding
  `TELEGRAM_*` keys, so the bot fails with a config error that looks unrelated to
  the path. A test asserts the right directory.
- The log lands in `<checkout>/bot/logs/bot.log`, beside the launchers. When this
  code lived as a single `bot/bot.py`, `_log_dir()` naturally resolved to
  `bot/logs`; moving it into `osnmzbot/` silently moved the log inside the package
  while the README and `setup.ps1` still pointed at `bot/logs`. Three sources
  disagreed, and only a test caught it. `OSNM_Z_LOG_DIR` overrides the location.

## Chains

`DEFAULT_RPCS` probes six public endpoints, in order, and matches the collection's
real chain id. A mismatch is a retry signal, not a failure.

| Chain | id | Default RPC |
|---|---|---|
| Ethereum | 1 | `https://ethereum-rpc.publicnode.com` |
| Optimism | 10 | `https://mainnet.optimism.io` |
| Polygon | 137 | `https://polygon-rpc.com` |
| Robinhood | 4663 | `https://rpc.mainnet.chain.robinhood.com` |
| Base | 8453 | `https://mainnet.base.org` |
| Arbitrum | 42161 | `https://arb1.arbitrum.io/rpc` |

Sepolia (11155111) is recognised but has no default endpoint; supply one through
`OSNM_EXTRA_RPCS`. `RPC_URL` from the app `.env` is always tried first.

For a first-come-first-served public stage, a **private RPC matters**: public
endpoints rate-limit, and being seconds late is the difference between minting and
not.

## Commands

| Command | Effect |
|---|---|
| `<opensea link>` | Start a mint session, auto-detect the chain |
| `/mint <link>` | Same, explicit |
| `/wallet` | Active address plus balance on every reachable chain |
| `/wallet set <key>` | Replace the private key (the command message is deleted) |
| `/wallet clear` | Empty the key — requires an inline confirmation tap |
| `/doctor` | Config, wallet, RPC, and OpenSea client checks |
| `/status` | Where the last mint got to: pending, mined, reverted, or dropped |
| `/cancel` | Abandon the current session |

`/wallet set` accepts the key inline, or prompts for it if you send the bare
command. Both paths delete the message first and route through the same
`apply_wallet_key()`.

## `/status` — what happened to my transaction

Reads the receipt of the last broadcast mint. It exists because a transaction
link is not an answer: the four outcomes below need different responses from
you, and none of them can be told apart by looking at a block explorer.

| State | Meaning | What to do |
|---|---|---|
| `succeeded` | Mined with status 1, confirmations counted from the chain head | Nothing |
| `reverted` | Mined with status 0. **The gas is spent** and no NFT arrived | Stop; do not retry blindly |
| `pending` | No receipt yet, still young | Wait |
| `dropped` | No receipt after 300s, so the node probably dropped it from the mempool | The nonce was never consumed and no gas was spent, so a retry is still valid |

The confirmation count is `head - block + 1`, read live, so a transaction mined
recently shows a low number and climbs. `dropped` is deliberately distinct from
`reverted`: a dropped transaction cost nothing, and calling that a failure would
push you into paying gas twice for the same NFT.

One transaction is tracked per chat, the most recent. The record outlives the
mint session, because `on_go` closes the session as soon as the transaction is
sent. It stores the RPC endpoint that actually broadcast the transaction, since
another endpoint may report no receipt for a transaction that has already mined.

State is in memory, so a restart forgets it. A tx that is still in flight when
the service restarts is still visible on any explorer, just not through `/status`.

## Dry run — simulate before spending gas

The confirm screen offers **🧪 Simulasi dulu** next to **MINT**. The simulation
builds the exact calldata that would be sent and runs it as an `eth_call` against
pending state, then reports calldata, value, worst-case gas, balance, and whether
the wallet can afford it.

It never signs and never broadcasts. A structural test greps `simulate.py` for
`sign_transaction`, `broadcast_signed`, and `eth_sendRawTransaction` and fails if
any of them appear, so the no-signing property is enforced by the suite rather
than by review.

It catches two failures the OpenSea availability flag cannot show you, because
that flag is a cache and says nothing about your specific wallet:

- **Per-wallet quota exhausted** while the stage still looks open
- **An allowlist (GTD) that does not contain your address**

The simulation sends its own `eth_call` rather than reusing the upstream
`contract_call()`. That is not a preference: `contract_call()` omits `from`, so it
executes as the zero address, where SeaDrop sees an empty allowlist and no quota
use. An eligible wallet would be reported ineligible, and an exhausted one would
pass.

Gas is priced exactly only when fees are manual. With automatic fees the price
is only knowable at signing time, so the report says nothing about
affordability rather than guessing.

Manual mode has a trap worth stating: `FEE_AUTOMATIC=true` is **rejected** if
`MAX_FEE_PER_GAS_GWEI` or `MAX_PRIORITY_FEE_PER_GAS_GWEI` is present, so the
commented-out example lines in `.env.example` have to be deleted, not just
edited. All three settings change together:

```bash
FEE_AUTOMATIC=false
MAX_FEE_PER_GAS_GWEI=0.05
MAX_PRIORITY_FEE_PER_GAS_GWEI=0.01
```

Pick the cap from the chain you actually mint on rather than copying the example.
On Base the live base fee is around 0.005 gwei, so 0.05 gwei is roughly 8x
headroom; the 1.5 gwei in `.env.example` is 300x this chain's real price and
reserves enormously more than it needs to. Check before you set it:

```bash
curl -s -X POST "$RPC_URL" -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_gasPrice","params":[]}'
```

The trade-off of a manual cap is that if the base fee rises past it, the
transaction is unminable until it drops again. The cap has to sit above the
base fee with room to spare, not just above the current tip.

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

`totalSupply()` is collection-wide, not per-stage. `supply.py` implements a
per-stage reader over zero-address `Transfer` logs and is **not wired into the bot**:
a full-history scan needs ~37,000 `eth_getLogs` requests per stage on Robinhood
Chain, which times out against a public endpoint. Narrow windows (2,000 blocks) do
complete in under a second, so a stage that opened recently is countable — a stage
that already closed is not. Accurate per-stage history needs an indexer.

## Testing

```bash
uv run --frozen --no-sync python -m pytest bot/test_bot.py -q
# 97 passed
```

`uv sync --frozen` installs the upstream lockfile only. `python-telegram-bot` and
`pytest` are **not** in it — install from `requirements-bot.txt` first, or the bot
fails at import with `ModuleNotFoundError: No module named 'telegram'`.

97 passed from a clean `git clone` of both repos on Python 3.12.13.

`test_bot.py` builds sessions from the upstream dataclasses field-for-field rather
than mocking, so an upstream shape change fails the tests instead of crashing in
production. Destructive paths run against a `tempfile` copy with `set_app_dir()`
redirected, so no test can touch a real `.env`. Tests also assert structural
invariants that broke during development: the app root, the log location, module
import independence, a per-module size ceiling, and the absence of star imports.

## Operational notes

- The `systemd` unit hardens with `ProtectSystem=full`, `ProtectHome=read-only`, and
  `ReadWritePaths` scoped to the app directory. Because `ProtectHome` makes
  `/root/.cache` unwritable, `run-bot.sh` points `UV_CACHE_DIR` and `TMPDIR` inside
  the app tree, and resolves `uv` explicitly since `systemd` does not inherit the
  interactive shell `PATH`.
- The rotating log is at `bot/logs/bot.log`, mode 0600, 2 MB x 3. It records request
  and response detail, which is why it is owner-only and why `**/logs/` is ignored.
- In-memory state (`awaiting_key`, confirm nonces, the mint lock) is lost on restart.
  A key pasted more than a minute or two after `/wallet set` is ignored and remains
  in the chat.
- Minting uses one wallet per run. The upstream tool has no multi-wallet mode.
- Timed/Dutch auctions are not supported by either layer: `value` is computed once
  from `getPublicDrop()` and never varies with time.
- Use a dedicated wallet. A well-known burnable key such as `0x1111…11` is
  auto-detected by sweepers and drained within minutes of a deposit.

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
- Use a dedicated mint wallet holding only the amount intended.
- Upstream explicitly recommends a secondary wallet: only one wallet pays mint price
  and gas per run.

## License

MIT, matching upstream `osnm-z`. See `LICENSE` and `NOTICE`.
