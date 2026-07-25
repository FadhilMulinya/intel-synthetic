# ckb-bot-simulator

Simulates bot-like transaction behavior on a local CKB (Nervos) devnet and
records every submitted+confirmed transaction as JSON. Built to produce
labeled data (ground-truth bot archetype per address) for downstream
analysis -- the code itself is intentionally minimal and functional, not a
general-purpose framework.

## How it works / approach used

**Devnet:** [`offckb`](https://github.com/ckb-devrel/offckb), installed via
npm, spins up a local CKB node + miner with 20 pre-funded genesis accounts.
It manages one shared devnet under `~/.local/share/offckb-nodejs/`
regardless of which project directory you run it from.

**Chain access:** no actively-maintained Python CKB SDK exists today
(`ckb-sdk-python` and `ckb-python-toolkit` are both multi-year-stale with no
recent PyPI releases), and `ckb-cli`'s `wallet transfer` command doesn't
cleanly support the single-transaction multi-output transfers the
`batch_payer` archetype needs. So this project talks to the devnet's
JSON-RPC directly over HTTP (`requests`), with a small hand-rolled
transaction-construction package (`ckb/`, split into config/hashing/molecule/
keys/rpc/transfer modules) implementing just enough
Molecule serialization to compute a transaction hash and signing message --
`send_transaction` itself takes a plain JSON transaction object, so no
Molecule encoding is needed for submission, only for hashing/signing.

**Signing:** secp256k1 has no prebuilt Python 3.14 wheel yet for the usual
`coincurve` binding, and building it from source hit unrelated packaging
bugs in this environment. `secp256k1_pure.py` is a small (~100 line) pure-Python
implementation (point arithmetic + RFC 6979 deterministic nonces) with no
compiled dependency, validated against known secp256k1 test vectors and
against offckb's own genesis keypairs before being trusted with real funds.

**Concurrency:** each bot runs in its own thread and spends its own
single-cell UTXO chain (seeded once at funding time). Chains are
thread-local by design -- a shared queue mixing "my own change" with
"funds I received from other bots" turned out to race (whichever arrived
first at the front of the queue got spent, sometimes leaving a bot with too
little balance for its next round). Since every bot is funded generously
enough to cover its whole quota from its own chain, thread-local chains
sidestep the race entirely while still recording the real on-chain
transfers between bots.

Transactions are submitted back-to-back without waiting for confirmation in
between (devnet's tx pool happily accepts several unconfirmed
ancestor transactions chained together), so each bot's round-to-round pace
is governed purely by its fixed archetype interval, not block time. A pool
of background "confirmation watcher" threads polls each submitted tx until
it's committed, then appends the record (now including block number) to the
*sending* bot's own `add_N.json`.

## Quick start

Once `offckb` is on `PATH` and this project's dependencies are installed
(see setup below), `run.sh` starts the devnet if it isn't already running
and launches a full run detached from the shell, so it survives your
terminal closing:

```bash
./run.sh --bots 300 --hours 24 --txs 700
# check progress any time:
tail -f data/bot_300/run.log
wc -l data/bot_300/add_*.json | tail -1
```

Output goes to `data/bot_<bots>/` by default (override with `--out NAME`).
`run.sh` refuses to run if that output folder already exists, so it never
overwrites a previous run's data. `offckb`'s own devnet chain data is
likewise reused, never wiped, across invocations.

## Setup / running it manually

```bash
cd ckb-bot-simulator
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# make sure offckb is on PATH (installed to a user-local npm prefix)
export PATH="$HOME/.npm-global/bin:$PATH"

# start the devnet (leave running in another terminal / tmux pane)
offckb node

# quick smoke test: fast fixed intervals (whole run takes a couple minutes)
python3 simulate.py --bots 20 --txs-per-bot 100 --out stage1_20bots_100tx

# a run spread over a target duration: 300 bots, 100 tx each, over ~12 hours
python3 -u simulate.py --bots 300 --txs-per-bot 100 --out run_300bots_100tx --duration-hours 12
```

Output goes to `data/<out>/bots.json` plus one `data/<out>/add_N.json` per
bot (`N` = that bot's roster `index` + 1). `data/` (private keys included)
is gitignored -- never commit it.

**`--duration-hours`** stretches every archetype's fixed interval by the
same factor so the slowest archetype's `txs_per_bot` rounds span roughly
that many hours (all archetypes finish around the same wall-clock time).
Omit it for fast fixed intervals (2-8s, good for smoke tests -- the whole
run takes a couple of minutes regardless of bot count, since bots run
concurrently).

For a long run meant to keep going unattended, launch it detached from the
shell so it survives independent of your terminal/session (`python3 -u` to
keep the log unbuffered and useful for checking progress), same as what
`run.sh` does automatically:

```bash
nohup python3 -u simulate.py --bots 300 --txs-per-bot 100 \
  --out run_300bots_100tx --duration-hours 12 > run_300bots_100tx.log 2>&1 &
disown
# check progress any time:
tail -f run_300bots_100tx.log
wc -l data/run_300bots_100tx/add_*.json | tail -1
```

## Output schema

### `bots.json`

A JSON array, one object per bot -- the ground-truth labels:

| field | meaning |
|---|---|
| `index` | position in the roster (also used to resolve counterparty references) |
| `address` | CKB address (bech32m, `ckt1...` devnet/testnet format) |
| `lock_arg` | raw 20-byte blake160 lock arg backing the address |
| `pubkey` | compressed secp256k1 public key (33 bytes, hex) |
| `privkey` | secp256k1 private key (32 bytes, hex) -- **devnet-only test material** |
| `archetype` | one of `periodic`, `market_maker`, `fan_out_hub`, `fan_in_sink`, `batch_payer` |
| `params` | archetype-specific fixed parameters (see below), with counterparties resolved to addresses |

### `add_N.json` (per-bot transaction files)

Each bot gets its own file, `add_<index + 1>.json` (so bot `index=0` ->
`add_1.json`, ... bot `index=299` -> `add_300.json`, matching `bots.json`'s
`index` field). Every record in a given `add_N.json` was sent *by* that
bot -- transfers it merely received show up in the *sender's* file instead,
never here.

**Newline-delimited JSON**: the first line is a header, `{"address":
"<this bot's address>"}`; every line after that is one JSON object per
transaction. Not a single JSON array -- this is what "stream as it happens,
survive interruption" means in practice -- appending one line at a time is a
single atomic write; growing a top-level JSON array under concurrent
writers would mean rewriting the whole file (or careful bracket/comma
surgery) on every transaction, with a real risk of leaving the file corrupt
if the process is killed mid-write. Read it with:

```python
lines = list(open(path))
address = json.loads(lines[0])["address"]
transactions = [json.loads(line) for line in lines[1:]]
```

Each transaction line mirrors the shape of a transaction returned by
[explorer.nervos.org's public API](https://mainnet-api.explorer.nervos.org/api/v1/transactions/)
(fetched a live example to match field names exactly), plus one extra
`archetype` field:

| field | meaning |
|---|---|
| `transaction_hash` | transaction hash |
| `version` | always `"0"` |
| `is_cellbase` | always `false` (none of these are miner reward transactions) |
| `transaction_fee` | fee paid, in shannon (string) |
| `bytes` | exact on-wire serialized transaction size |
| `cycles` | `null` -- real explorer gets this from running the CKB VM script verifier, which this project doesn't do |
| `cell_deps` / `header_deps` / `witnesses` | as submitted on-chain |
| `display_inputs` | array of cells this transaction spent (always exactly 1 here -- see below); each has `address_hash`, `capacity` (shannon, string), `occupied_capacity` (min capacity backing the cell, shannon string), `generated_tx_hash` (the transaction that created this cell), `cell_index`, `cell_type` |
| `display_outputs` | array of cells this transaction created: one per recipient, plus a change cell back to the sender. Each has `address_hash`, `capacity`, `occupied_capacity`, `cell_index`, `cell_type`, and `status` (`"live"` or `"dead"`) / `consumed_tx_hash` (which later transaction, if any, spent this cell -- see below) |
| `block_number`, `block_timestamp`, `tx_status` | filled in once confirmed (`block_timestamp` in ms, matching explorer) |
| `archetype` | the *sender's* archetype -- not part of the real explorer schema, added for downstream labeling |

`batch_payer` transactions bundle multiple recipients into one on-chain
transaction, so they show up as **multiple entries in `display_outputs`
within a single line** -- still one line, one `transaction_hash`, and one
sent transaction toward the bot's quota.

**On `status`/`consumed_tx_hash`:** each bot spends its own change cell
forward every round (that's how it keeps sending without waiting on anyone
else), so a bot's own successive change outputs form a visible spend chain
within that bot's own `add_N.json` and correctly flip to `"dead"` once the next round's
transaction consumes them. Cells sent *to* another bot are real, genuine
transfers -- verifiable on-chain -- but the receiving bot never spends
*those specific* cells forward in this simulation (see `worker.py`'s
`bot_worker` docstring for why), so transfer-recipient outputs legitimately
stay `"live"` for the life of the dataset, same as a real explorer would
show for any UTXO nobody has spent yet.

## Archetypes

All parameters below are fixed for a bot's entire run -- no per-transaction
randomness in timing, amounts, or counterparty choice, since the point is
recognizably *bot* behavior, not human-like noise.

- **`periodic`** -- fires one transaction every fixed interval (6s), cycling
  through a small fixed list of 3 counterparties in order.
- **`market_maker`** -- paired with 1-2 other `market_maker` bots; sends
  rapidly (2s interval) back and forth within a tight, near-constant
  capacity range (70 or 75 CKB, alternating).
- **`fan_out_hub`** -- sends to a wide rotating set of up to 12 other bots
  (4s interval), a small fixed capacity increment (70 CKB) each time.
- **`fan_in_sink`** -- mirror of `fan_out_hub`: within each group, one bot
  is the `sink` and the rest are `feeder`s. Feeders periodically (5s) send a
  fixed amount to the sink; the sink itself sends the same fixed amount back
  out to its feeders on rotation (so it also accumulates its own send
  quota, mirroring the fan-out pattern in reverse).
- **`batch_payer`** -- every round (8s interval), bundles 3 fixed recipients
  into a single multi-output transaction instead of sending one transaction
  per recipient.

Archetypes are assigned round-robin across the bot roster, so they end up
roughly evenly split.

## Funding

Each bot is funded once, from one of the 20 offckb genesis accounts
(round-robin), with:

```
funding = (outputs_per_round x capacity_per_output + fee) x txs_per_bot x 1.5   [+ small buffer]
```

using the worst case (`batch_payer`, 3 outputs/round) so every archetype has
comfortable headroom over its actual per-round spend.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

No extra dependencies needed -- the suite uses only the standard library
(`unittest` + `unittest.mock`). Almost everything runs fully offline: the
devnet RPC and `offckb`/`subprocess` calls are mocked, so the suite is fast
and safe to run with nothing else installed or running.

The one exception is `tests/test_simulate_integration.py`, an end-to-end
smoke test that opportunistically runs a tiny real `simulate.py` stage
(7 bots, 1 tx each) against a live devnet on `127.0.0.1:8114` if one happens
to be reachable, and otherwise skips itself -- it's meant for local
development or a CI job that starts a devnet first, not for routine runs.

## Contributing

Issues and pull requests are welcome -- see [CONTRIBUTING.md](CONTRIBUTING.md)
for project layout and setup.

## License

[MIT](LICENSE)
