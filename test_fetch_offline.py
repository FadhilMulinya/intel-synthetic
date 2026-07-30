"""Offline sanity check for fetch_real_data.py's parsing logic, using
fixtures shaped exactly like the documented Explorer API responses.
No network calls -- this only proves the code paths are correct against
the schema; it can't catch live-API surprises (rate limits, schema drift).
"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(__file__))
import fetch_real_data as frd

TIP_RESP = {"data": {"attributes": {"tip_block_number": 1000}}}

BLOCK_HASH_RESP = {"data": {"attributes": {"block_hash": "0xblockhash999"}}}

BLOCK_TX_RESP = {
    "data": [
        {
            "attributes": {
                "is_cellbase": False,
                "transaction_hash": "0xabc",
                "block_number": "999",
                "block_timestamp": "1700000000000",
                "display_inputs": [{"address_hash": "ckb1addrBOT"}],
                "display_outputs": [
                    {"address_hash": "ckb1addrBOT", "capacity": "1000000000"},
                    {"address_hash": "ckb1addrHUMAN", "capacity": "500000000"},
                ],
                "income": "0",
            }
        },
        {
            "attributes": {
                "is_cellbase": True,  # should be skipped (miner reward, not wallet activity)
                "transaction_hash": "0xdef",
                "block_number": "999",
                "block_timestamp": "1700000000000",
                "display_inputs": [],
                "display_outputs": [{"address_hash": "ckb1addrMINER", "capacity": "100000000"}],
                "income": "0",
            }
        },
    ]
}

ADDR_BOT_RESP = {"data": {"attributes": {"transactions_count": "5000", "is_special": "false"}}}
ADDR_HUMAN_RESP = {"data": {"attributes": {"transactions_count": "12", "is_special": "false"}}}
ADDR_AMBIGUOUS_RESP = {"data": {"attributes": {"transactions_count": "300", "is_special": "false"}}}

ADDR_TX_PAGE1 = {
    "data": [
        {"attributes": {"transaction_hash": "0x1", "block_number": "10", "block_timestamp": "1700000000000",
                         "display_inputs": [], "display_outputs": [{"address_hash": "ckb1addrHUMAN", "capacity": "100000000"}]}},
    ]
}
ADDR_TX_PAGE_EMPTY = {"data": []}


def fake_api_get(path, params=None, **kwargs):
    if path == "/statistics/tip_block_number":
        return TIP_RESP
    if path == "/blocks/1000":
        return BLOCK_HASH_RESP
    if path == "/block_transactions/0xblockhash999":
        return BLOCK_TX_RESP
    if path == "/addresses/ckb1addrBOT":
        return ADDR_BOT_RESP
    if path == "/addresses/ckb1addrHUMAN":
        return ADDR_HUMAN_RESP
    if path == "/addresses/ckb1addrMINER":
        return ADDR_AMBIGUOUS_RESP
    if path.startswith("/address_transactions/"):
        page = (params or {}).get("page", 1)
        return ADDR_TX_PAGE1 if page == 1 else ADDR_TX_PAGE_EMPTY
    raise AssertionError(f"unexpected path in test: {path}")


def run():
    with mock.patch.object(frd, "api_get", side_effect=fake_api_get):
        # discovery: should find 3 unique addrs, cellbase output excluded
        # only insofar as its OWN tx is skipped, but ckb1addrMINER still
        # wasn't emitted by any non-cellbase tx, so it should NOT appear.
        addrs = frd.discover_addresses(start_block=1000, end_block=999, page_size=10)
        assert addrs == {"ckb1addrBOT", "ckb1addrHUMAN"}, f"unexpected discovery set: {addrs}"
        print("PASS: discover_addresses excludes cellbase-only addresses ->", addrs)

        assert frd.classify_address("ckb1addrBOT", human_max_tx=50, bot_min_tx=1000) == "bot_like"
        assert frd.classify_address("ckb1addrHUMAN", human_max_tx=50, bot_min_tx=1000) == "human_like"
        assert frd.classify_address("ckb1addrMINER", human_max_tx=50, bot_min_tx=1000) is None
        print("PASS: classify_address buckets correctly on tx-count/is_special only")

        txs = frd.fetch_address_transactions("ckb1addrHUMAN", max_tx=300, page_size=50)
        assert len(txs) == 1
        assert txs[0]["transaction_fee"] == "0", "fee should default to '0' when absent"
        print("PASS: fetch_address_transactions flattens attrs + defaults missing fee")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "addr_0.json")
            frd.write_address_file(path, "ckb1addrHUMAN", txs)
            with open(path) as f:
                lines = f.readlines()
            first = json.loads(lines[0])
            second = json.loads(lines[1])
            assert first == {"address": "ckb1addrHUMAN"}
            assert second["transaction_hash"] == "0x1"
        print("PASS: write_address_file matches add_N.json shape (header line + flat tx lines)")

    with tempfile.TemporaryDirectory() as tmp:
        state = frd.load_checkpoint(tmp)
        assert state["oldest_scanned_block"] is None and state["discovered"] == []
        state["oldest_scanned_block"] = 500
        state["discovered"] = ["ckb1x", "ckb1y"]
        state["classified"] = {"ckb1x": "bot_like"}
        state["fetched"] = {"bot_like": ["ckb1x"], "human_like": []}
        frd.save_checkpoint(tmp, state)
        reloaded = frd.load_checkpoint(tmp)
        assert reloaded == state, f"checkpoint didn't round-trip: {reloaded} != {state}"
    print("PASS: checkpoint save/load round-trips correctly")

    print("\nALL OFFLINE CHECKS PASSED")


if __name__ == "__main__":
    run()
