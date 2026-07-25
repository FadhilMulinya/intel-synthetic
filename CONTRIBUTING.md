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
| `simulate.py` | entry point: bot roster, archetype logic, funding, concurrent run loop |
| `ckb.py` | CKB transaction construction, Molecule serialization, signing, JSON-RPC |
| `secp256k1_pure.py` | pure-Python secp256k1 key derivation + RFC 6979 signing |

See `README.md` for the full architecture write-up, archetype definitions, and
output schema.

## Guidelines

- Keep timing/amount/counterparty parameters fixed and non-random per
  archetype -- the point of this project is recognizably bot-like behavior,
  not human-like noise.
- No compiled/native dependencies (this is why `secp256k1_pure.py` exists
  instead of a `coincurve`/`libsecp256k1` binding) -- keep the project
  trivially installable with just `pip install -r requirements.txt`.
- `data/` (contains private keys) is gitignored; never commit run output.
- Run `python3 -m py_compile *.py` before submitting a change as a basic
  sanity check.

## Submitting changes

1. Fork the repo and create a branch off `main`.
2. Keep changes focused -- one logical change per pull request.
3. Describe what you tested (e.g. a smoke-test run) in the PR description.
