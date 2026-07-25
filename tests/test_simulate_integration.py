"""End-to-end smoke test against a real, already-running CKB devnet, if one
happens to be reachable. Skips (does not fail) when none is running, since
most environments running the unit suite won't have offckb up -- this is
for local development or a CI job that starts a devnet first."""
import json
import os
import shutil
import socket
import tempfile
import unittest

import simulate


def _devnet_reachable(host="127.0.0.1", port=8114, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@unittest.skipUnless(_devnet_reachable(), "no CKB devnet reachable on 127.0.0.1:8114")
class SimulateEndToEndTests(unittest.TestCase):
    def test_tiny_run_produces_bots_json_and_per_bot_add_files(self):
        # n=7 is the smallest roster where every archetype (notably
        # market_maker, which needs a partner) is satisfiable -- see
        # archetypes.py's build_roster guard.
        n_bots = 7
        out_dir = tempfile.mkdtemp(prefix="ckb_sim_test_")
        try:
            summary = simulate.run_stage(n_bots=n_bots, txs_per_bot=1, out_dir=out_dir)
            self.assertEqual(summary["total"], n_bots)

            with open(os.path.join(out_dir, "bots.json")) as f:
                bots = json.load(f)
            self.assertEqual(len(bots), n_bots)

            for i in range(n_bots):
                path = os.path.join(out_dir, f"add_{i + 1}.json")
                self.assertTrue(os.path.exists(path))
                with open(path) as f:
                    lines = [json.loads(l) for l in f]
                self.assertEqual(lines[0]["address"], bots[i]["address"])
                self.assertEqual(len(lines) - 1, 1)  # header + 1 confirmed tx
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
