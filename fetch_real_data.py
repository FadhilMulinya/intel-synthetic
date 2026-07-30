"""
fetch_real_data.py

Pulls real CKB mainnet address activity from the public CKB Explorer API and
writes it out in the same flattened per-address NDJSON shape that
ckb-bot-simulator's `data/run_*/add_N.json` files use, so it can be fed
straight into `extract_features.py` unchanged.

WHY TWO POOLS, AND WHY THESE SPECIFIC SIGNALS
-----------------------------------------------
The whole point of pulling real data is to stress-test the one-class model
with something other than synthetic bots and a synthetic human proxy. But if
we *select* addresses using the same signal the model scores on (interval
regularity, tx volume), any resulting numbers are circular -- we'd just be
asking the model to rediscover the rule we used to build the labels.

So addresses are bucketed on signals that are orthogonal to the model's
feature set:

  bot_like  : Explorer's own `is_special` flag (known exchange / DEX /
              contract / mining-pool addresses) -- automated by *identity*,
              not by inferred timing pattern.
  human_like: very low lifetime `transactions_count` and *not* flagged
              special -- a proxy for "casual wallet", not proof of humanity.
              Still a heuristic, but it's an identity/lifetime-count filter,
              not an interval-CV or volume filter, so it doesn't leak into
              the axis being measured.

These are still proxy labels, not ground truth. Use this data for
EVALUATION of the trained one-class models, not as a second training class
(the model should stay one-class, trained on bot data only).

USAGE
-----
    pip install requests
    python3 fetch_real_data.py \
        --n-blocks 500 \
        --max-per-pool 40 \
        --max-tx-per-address 300 \
        --out-dir data/real

Respect the public API: this script rate-limits itself (default 4 req/s)
and backs off on 429s. Don't crank concurrency against a free public
endpoint.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "https://mainnet-api.explorer.nervos.org/api/v1"
HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "User-Agent": "ckb-bot-simulator-real-data-fetch/1.0",
}


class ApiError(Exception):
    pass


def api_get(path, params=None, min_interval=0.25, max_retries=6):
    """GET against the Explorer API with polite rate limiting and backoff.

    min_interval: minimum seconds between calls (self-imposed politeness).
    Retries on 429/5xx with exponential backoff; raises on 4xx (other than
    429) since those indicate a bad request, not a transient failure.
    """
    url = BASE_URL + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"

    delay = 1.0
    last_err = None
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            time.sleep(min_interval)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if e.code == 404:
                return None  # e.g. "Record Not Found" for an address with no txs
            raise ApiError(f"GET {url} -> HTTP {e.code}: {e.read()[:300]}")
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            # Raw read-timeouts (socket.timeout) aren't always wrapped in
            # URLError by urllib -- catch broadly here so transient network
            # hiccups get retried instead of killing the whole run.
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise ApiError(f"GET {url} failed after {max_retries} retries: {last_err}")


def get_tip_block_number():
    data = api_get("/statistics/tip_block_number")
    return int(data["data"]["attributes"]["tip_block_number"])


def get_block_hash(block_num):
    """/block_transactions requires a block HASH, not a number, despite the
    (stale, 2023) docs showing a number in the example curl call -- as of
    this writing it 422s on a bare number. Resolve number -> hash first."""
    data = api_get(f"/blocks/{block_num}")
    if not data or not data.get("data"):
        return None
    return data["data"]["attributes"].get("block_hash")


def discover_addresses(start_block, end_block, page_size=40):
    """Walk blocks (start_block down to end_block+1, inclusive of start_block)
    and collect unique addresses seen in transaction inputs/outputs. Cellbase
    (miner reward) outputs are skipped since those aren't wallet activity."""
    seen = set()
    for block_num in range(start_block, end_block, -1):
        block_hash = get_block_hash(block_num)
        if not block_hash:
            continue
        data = api_get(f"/block_transactions/{block_hash}", params={"page": 1, "page_size": page_size})
        if not data:
            continue
        for tx in data.get("data", []):
            attrs = tx.get("attributes", {})
            if attrs.get("is_cellbase"):
                continue
            for cell in attrs.get("display_inputs", []) + attrs.get("display_outputs", []):
                addr = cell.get("address_hash")
                if addr:
                    seen.add(addr)
    return seen


def classify_address(address, human_max_tx=50, bot_min_tx=1000):
    """Fetch an address's summary and bucket it using identity/lifetime
    signals only -- never interval or amount regularity."""
    data = api_get(f"/addresses/{address}")
    if not data or not data.get("data"):
        return None
    record = data["data"]
    # Some addresses (e.g. full-format addresses backing multiple lock-script
    # variants) return `data` as a list of records instead of a single
    # object. When that happens, sum tx counts and OR the special flag
    # across records rather than picking one arbitrarily.
    records = record if isinstance(record, list) else [record]
    tx_count = 0
    is_special = False
    for r in records:
        attrs = r.get("attributes", {})
        tx_count += int(attrs.get("transactions_count") or 0)
        is_special = is_special or (str(attrs.get("is_special", "false")).lower() == "true")

    if is_special or tx_count >= bot_min_tx:
        return "bot_like"
    if 1 <= tx_count <= human_max_tx and not is_special:
        return "human_like"
    return None  # ambiguous middle ground -- don't guess, just skip


def fetch_address_transactions(address, max_tx=300, page_size=50):
    """Pull an address's tx history and flatten it into the same per-line
    shape as ckb-bot-simulator's add_N.json (drops the JSON:API id/type
    wrapper, keeps `attributes` fields flat)."""
    out = []
    page = 1
    while len(out) < max_tx:
        data = api_get(
            f"/address_transactions/{address}",
            params={"page": page, "page_size": page_size, "sort": "time.asc"},
        )
        if not data or not data.get("data"):
            break
        rows = data["data"]
        if not rows:
            break
        for tx in rows:
            attrs = dict(tx.get("attributes", {}))
            # Address Transaction List doesn't return transaction_fee (only
            # the singular Transaction endpoint does). Rather than 1 extra
            # API call per tx (expensive against a shared public endpoint),
            # default it to "0" and flag the field as unreliable downstream.
            attrs.setdefault("transaction_fee", "0")
            out.append(attrs)
            if len(out) >= max_tx:
                break
        if len(rows) < page_size:
            break
        page += 1
    return out


def write_address_file(path, address, txs):
    with open(path, "w") as f:
        f.write(json.dumps({"address": address}) + "\n")
        for tx in txs:
            f.write(json.dumps(tx) + "\n")


def checkpoint_path(out_dir):
    return os.path.join(out_dir, "checkpoint.json")


def load_checkpoint(out_dir):
    path = checkpoint_path(out_dir)
    if not os.path.exists(path):
        state = {}
    else:
        state = json.load(open(path))
    # setdefault everything so a checkpoint.json written by an older
    # version of this script (missing newer keys) still loads cleanly.
    state.setdefault("oldest_scanned_block", None)  # None = haven't scanned anything yet, start at tip
    state.setdefault("discovered", [])              # all addresses ever seen during block-walking
    state.setdefault("classified", {})               # address -> "bot_like" | "human_like" | null (cached, don't re-query)
    state.setdefault("fetched", {"bot_like": [], "human_like": []})  # addresses already pulled + written to disk, in file-index order
    state.setdefault("failed", {})                   # address -> error string; permanently skipped after failing once
    return state


def save_checkpoint(out_dir, state):
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = checkpoint_path(out_dir) + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
    os.replace(tmp_path, checkpoint_path(out_dir))  # atomic-ish, avoids truncated file on crash mid-write


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-blocks", type=int, default=500, help="how many additional (older) blocks to scan THIS run")
    ap.add_argument("--block-page-size", type=int, default=40)
    ap.add_argument("--max-per-pool", type=int, default=40, help="target addresses per bucket, ACROSS ALL RUNS (resumable)")
    ap.add_argument("--max-tx-per-address", type=int, default=300)
    ap.add_argument("--human-max-tx", type=int, default=50, help="lifetime tx count ceiling for the human_like bucket")
    ap.add_argument("--bot-min-tx", type=int, default=1000, help="lifetime tx count floor for the bot_like bucket")
    ap.add_argument("--out-dir", default="data/real")
    args = ap.parse_args()

    state = load_checkpoint(args.out_dir)
    discovered = set(state["discovered"])
    classified = state["classified"]
    fetched = state["fetched"]

    # --- 1. discover: advance the scan window further into the past each run ---
    if state["oldest_scanned_block"] is None:
        tip = get_tip_block_number()
        scan_start, scan_end = tip, tip - args.n_blocks
    else:
        scan_start = state["oldest_scanned_block"] - 1
        scan_end = scan_start - args.n_blocks
    scan_end = max(scan_end, 0)

    print(f"scanning blocks {scan_start} down to {scan_end + 1} "
          f"({scan_start - scan_end} blocks) for candidate addresses...", file=sys.stderr)
    new_addrs = discover_addresses(scan_start, scan_end, page_size=args.block_page_size)
    discovered |= new_addrs
    state["discovered"] = sorted(discovered)
    state["oldest_scanned_block"] = scan_end
    save_checkpoint(args.out_dir, state)
    print(f"found {len(new_addrs)} new addresses this run ({len(discovered)} accumulated total)", file=sys.stderr)

    # --- 2. classify: only addresses not already cached from a prior run ---
    to_classify = [a for a in discovered if a not in classified]
    print(f"classifying {len(to_classify)} not-yet-classified addresses "
          f"({len(classified)} already cached)...", file=sys.stderr)
    for i, addr in enumerate(to_classify):
        classified[addr] = classify_address(addr, human_max_tx=args.human_max_tx, bot_min_tx=args.bot_min_tx)
        if i % 25 == 0:
            state["classified"] = classified
            save_checkpoint(args.out_dir, state)
            b = sum(1 for v in classified.values() if v == "bot_like")
            h = sum(1 for v in classified.values() if v == "human_like")
            print(f"  classified {i}/{len(to_classify)} new "
                  f"(pools so far: bot_like={b}, human_like={h})", file=sys.stderr)
    state["classified"] = classified
    save_checkpoint(args.out_dir, state)

    # --- 3. fetch tx history: top up each pool to --max-per-pool, skipping already-fetched/failed addresses ---
    pool_candidates = {"bot_like": [], "human_like": []}
    for addr, bucket in classified.items():
        if bucket in pool_candidates:
            pool_candidates[bucket].append(addr)

    for bucket in ("bot_like", "human_like"):
        bucket_dir = os.path.join(args.out_dir, bucket)
        os.makedirs(bucket_dir, exist_ok=True)
        already = set(fetched[bucket])
        usable = [a for a in pool_candidates[bucket] if a not in already and a not in state["failed"]]
        needed = max(args.max_per_pool - len(fetched[bucket]), 0)
        print(f"'{bucket}': {len(fetched[bucket])}/{args.max_per_pool} already fetched, "
              f"{len(usable)} usable candidates available, need {needed} more...", file=sys.stderr)

        got = 0
        for addr in usable:
            if got >= needed:
                break
            try:
                txs = fetch_address_transactions(addr, max_tx=args.max_tx_per_address)
            except ApiError as e:
                # A single address's history can be genuinely slow/broken
                # server-side (e.g. an exchange hot wallet with millions of
                # txs) -- don't let one bad address kill the whole run.
                # Blacklist it permanently so future runs don't retry it.
                print(f"  SKIPPING {addr}: {e}", file=sys.stderr)
                state["failed"][addr] = str(e)
                save_checkpoint(args.out_dir, state)
                continue
            if not txs:
                continue
            idx = len(fetched[bucket])
            write_address_file(os.path.join(bucket_dir, f"addr_{idx}.json"), addr, txs)
            fetched[bucket].append(addr)
            got += 1
            state["fetched"] = fetched
            save_checkpoint(args.out_dir, state)  # checkpoint after EVERY address -- a crash loses at most one

        manifest = [{"index": i, "address": a, "label_source": bucket} for i, a in enumerate(fetched[bucket])]
        with open(os.path.join(bucket_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"  '{bucket}' now has {len(fetched[bucket])}/{args.max_per_pool} addresses fetched", file=sys.stderr)

    total = len(fetched["bot_like"]) + len(fetched["human_like"])
    target = args.max_per_pool * 2
    if total < target:
        print(f"\n{total}/{target} total addresses so far. Candidate pool from classification "
              f"may already cover the rest -- rerun the same command to fetch more from it, "
              f"or rerun again to scan further back if candidates run out.", file=sys.stderr)
    print("done. NOTE: transaction_fee is not returned by the address-transactions "
          "endpoint and defaults to 0 here -- treat fee_cv on this data as unreliable.",
          file=sys.stderr)


if __name__ == "__main__":
    main()