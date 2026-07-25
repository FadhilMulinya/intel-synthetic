"""Bot roster construction: keypairs, archetype assignment, fixed
per-archetype parameters, and per-round output computation. All parameters
here are fixed for a bot's entire run -- no per-transaction randomness in
timing, amounts, or counterparty choice, since the point is recognizably
*bot* behavior, not human-like noise.
"""
from collections import defaultdict

import ckb

ARCHETYPES = ["periodic", "market_maker", "fan_out_hub", "fan_in_sink", "batch_payer"]

CAPACITY_CKB = 70              # just above the 61 CKB minimum cell capacity
MARKET_MAKER_ALT_CKB = 75      # tight oscillation range: 70 <-> 75 CKB

# Relative pacing "shape" across archetypes (market_maker fastest, batch_payer
# slowest) at a fast testing scale. When --duration-hours is given, every
# value here is scaled up by the same factor so the slowest archetype's
# txs_per_bot rounds span the requested duration, and all archetypes finish
# around the same wall-clock time -- see compute_intervals().
BASE_INTERVAL_SEC = {
    "periodic": 6,
    "market_maker": 2,
    "fan_out_hub": 4,
    "fan_in_sink": 5,
    "batch_payer": 8,
}
PERIODIC_COUNTERPARTY_COUNT = 3
FAN_OUT_WIDTH = 12
BATCH_PAYER_RECIPIENTS = 3


def compute_intervals(txs_per_bot, duration_hours=None):
    """Scale BASE_INTERVAL_SEC so the whole run spans duration_hours (each
    archetype keeps its relative pacing but stretches to fit). Without
    --duration-hours, uses the fast base intervals as-is -- useful for quick
    smoke tests."""
    if duration_hours is None:
        return dict(BASE_INTERVAL_SEC)
    target_seconds = duration_hours * 3600
    slowest_base = max(BASE_INTERVAL_SEC.values())
    scale = target_seconds / (slowest_base * txs_per_bot)
    return {k: v * scale for k, v in BASE_INTERVAL_SEC.items()}


def build_roster(n, intervals):
    bots = []
    for i in range(n):
        privkey, pubkey, lock_arg, address = ckb.generate_keypair()
        bots.append({
            "index": i,
            "privkey": privkey,
            "pubkey": pubkey,
            "lock_arg": lock_arg,
            "address": address,
            "archetype": ARCHETYPES[i % len(ARCHETYPES)],
        })
    assign_params(bots, intervals)
    return bots


def assign_params(bots, intervals):
    n = len(bots)
    by_archetype = defaultdict(list)
    for b in bots:
        by_archetype[b["archetype"]].append(b["index"])

    for idx in by_archetype["periodic"]:
        offsets = range(1, PERIODIC_COUNTERPARTY_COUNT + 1)
        cps = sorted({(idx + off) % n for off in offsets} - {idx})
        bots[idx]["params"] = {
            "interval_sec": intervals["periodic"],
            "capacity_ckb": CAPACITY_CKB,
            "counterparties": cps,
        }

    mm = by_archetype["market_maker"]
    if len(mm) == 1:
        # "fold a lone leftover into the last pair" below only works once a
        # prior group already exists to fold into -- with exactly 1 bot
        # total there is no pair to form at all, and compute_round_outputs
        # would later divide by zero on an empty partners list. Fail here,
        # before any funding happens, instead of crashing mid-run.
        raise ValueError(
            f"roster of {n} bots yields exactly 1 market_maker bot (index {mm[0]}), "
            "which has no one to pair with. Use a bot count where market_maker gets "
            "at least 2 bots (n >= 7)."
        )
    i = 0
    while i < len(mm):
        if len(mm) - i >= 3 and (len(mm) - i) % 2 == 1:
            group, i = mm[i:i + 3], i + 3
        else:
            group, i = mm[i:i + 2], i + 2
        if len(group) < 2:
            group = mm[-2:]  # fold a lone leftover into the last pair
        for gi in group:
            partners = [x for x in group if x != gi]
            bots[gi]["params"] = {
                "interval_sec": intervals["market_maker"],
                "capacity_ckb_base": CAPACITY_CKB,
                "capacity_ckb_alt": MARKET_MAKER_ALT_CKB,
                "partners": partners,
            }

    for idx in by_archetype["fan_out_hub"]:
        width = min(FAN_OUT_WIDTH, n - 1)
        cps = sorted({(idx + off) % n for off in range(1, width + 1)} - {idx})
        bots[idx]["params"] = {
            "interval_sec": intervals["fan_out_hub"],
            "capacity_ckb": CAPACITY_CKB,
            "counterparties": cps,
        }

    fis = by_archetype["fan_in_sink"]
    if fis:
        sink_idx = fis[0]
        feeders = fis[1:] or [fis[0]]  # degenerate case: nothing else to do, self-loop guarded below
        bots[sink_idx]["params"] = {
            "interval_sec": intervals["fan_in_sink"],
            "capacity_ckb": CAPACITY_CKB,
            "role": "sink",
            "feeders": [f for f in feeders if f != sink_idx] or feeders,
        }
        for f in fis[1:]:
            bots[f]["params"] = {
                "interval_sec": intervals["fan_in_sink"],
                "capacity_ckb": CAPACITY_CKB,
                "role": "feeder",
                "sink": sink_idx,
            }

    for idx in by_archetype["batch_payer"]:
        k = min(BATCH_PAYER_RECIPIENTS, n - 1)
        recipients = sorted({(idx + off) % n for off in range(1, k + 1)} - {idx})
        bots[idx]["params"] = {
            "interval_sec": intervals["batch_payer"],
            "capacity_ckb": CAPACITY_CKB,
            "recipients": recipients,
        }


def compute_round_outputs(bot, round_num):
    """Returns [(target_bot_index, capacity_shannon), ...] for this round."""
    p = bot["params"]
    a = bot["archetype"]
    cap = lambda ckb_amount: ckb_amount * ckb.SHANNONS_PER_CKB

    if a == "periodic" or a == "fan_out_hub":
        cps = p["counterparties"]
        target = cps[round_num % len(cps)]
        return [(target, cap(p["capacity_ckb"]))]
    if a == "market_maker":
        partners = p["partners"]
        target = partners[round_num % len(partners)]
        amount = p["capacity_ckb_base"] if round_num % 2 == 0 else p["capacity_ckb_alt"]
        return [(target, cap(amount))]
    if a == "fan_in_sink":
        if p["role"] == "feeder":
            return [(p["sink"], cap(p["capacity_ckb"]))]
        feeders = p["feeders"]
        target = feeders[round_num % len(feeders)]
        return [(target, cap(p["capacity_ckb"]))]
    if a == "batch_payer":
        return [(r, cap(p["capacity_ckb"])) for r in p["recipients"]]
    raise ValueError(f"unknown archetype {a}")
