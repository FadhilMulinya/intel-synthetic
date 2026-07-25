# Contributing

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# offckb (local CKB devnet) must be on PATH
export PATH="$HOME/.npm-global/bin:$PATH"
offckb node
```

## Project layout

| file | purpose |
|---|---|
| `run.sh` | one-shot orchestrator: starts the devnet if needed, launches a detached run |
| `simulate.py` | entry point: wires the pieces below into one run (`run_stage`, CLI `main`) |
| `archetypes.py` | bot roster: keypairs, archetype assignment, per-round output computation |
| `funding.py` | genesis account discovery, per-bot funding, confirmation polling |
| `recorder.py` | per-bot NDJSON writer, confirmation watchers, live/dead status patching |
| `worker.py` | per-bot send loop (`bot_worker`): builds, signs, submits each round |
| `ckb/` | CKB transaction construction: `config`, `hashing`, `molecule`, `keys`, `rpc`, `transfer` |
| `secp256k1_pure.py` | pure-Python secp256k1 key derivation + RFC 6979 signing |
| `tests/` | unit tests (offline/mocked) + one opportunistic live-devnet smoke test |

See `README.md` for the full architecture write-up, archetype definitions,
and output schema.

## Guidelines

- Keep timing/amount/counterparty parameters fixed and non-random per
  archetype -- the point of this project is recognizably bot-like behavior,
  not human-like noise.
- No compiled/native dependencies (this is why `secp256k1_pure.py` exists
  instead of a `coincurve`/`libsecp256k1` binding) -- keep the project
  trivially installable with just `pip install -r requirements.txt`.
- `data/` (contains private keys) is gitignored; never commit run output.
- Run `python3 -m unittest discover -s tests -v` before submitting a change
  (see README.md's "Running the tests"). It needs nothing installed beyond
  the standard library and runs offline by default.

## Submitting changes

1. Fork the repo and create a branch off `main`.
2. Keep changes focused -- one logical change per pull request.
3. Describe what you tested (e.g. a smoke-test run) in the PR description.
