import unittest

from archetypes import (
    ARCHETYPES,
    BASE_INTERVAL_SEC,
    BATCH_PAYER_RECIPIENTS,
    FAN_OUT_WIDTH,
    PERIODIC_COUNTERPARTY_COUNT,
    build_roster,
    compute_intervals,
    compute_round_outputs,
)
from tests.helpers import configure_fake_sighash


class ComputeIntervalsTests(unittest.TestCase):
    def test_no_duration_returns_base_intervals_unscaled(self):
        self.assertEqual(compute_intervals(100), BASE_INTERVAL_SEC)

    def test_duration_scales_slowest_archetype_to_target_span(self):
        txs_per_bot, hours = 50, 10
        intervals = compute_intervals(txs_per_bot, duration_hours=hours)
        slowest = max(BASE_INTERVAL_SEC, key=BASE_INTERVAL_SEC.get)
        self.assertAlmostEqual(intervals[slowest] * txs_per_bot, hours * 3600, places=6)

    def test_relative_pacing_between_archetypes_is_preserved(self):
        intervals = compute_intervals(10, duration_hours=1)
        ratio_before = BASE_INTERVAL_SEC["batch_payer"] / BASE_INTERVAL_SEC["market_maker"]
        ratio_after = intervals["batch_payer"] / intervals["market_maker"]
        self.assertAlmostEqual(ratio_before, ratio_after)


class BuildRosterTests(unittest.TestCase):
    def setUp(self):
        configure_fake_sighash()
        self.bots = build_roster(20, compute_intervals(20))

    def test_every_bot_gets_a_known_archetype_and_params(self):
        for b in self.bots:
            self.assertIn(b["archetype"], ARCHETYPES)
            self.assertIn("params", b)

    def test_indices_sequential_and_addresses_unique(self):
        self.assertEqual([b["index"] for b in self.bots], list(range(20)))
        self.assertEqual(len({b["address"] for b in self.bots}), 20)

    def test_periodic_excludes_self_and_respects_count(self):
        for b in self.bots:
            if b["archetype"] == "periodic":
                cps = b["params"]["counterparties"]
                self.assertNotIn(b["index"], cps)
                self.assertLessEqual(len(cps), PERIODIC_COUNTERPARTY_COUNT)

    def test_fan_out_hub_excludes_self_and_respects_width(self):
        for b in self.bots:
            if b["archetype"] == "fan_out_hub":
                cps = b["params"]["counterparties"]
                self.assertNotIn(b["index"], cps)
                self.assertLessEqual(len(cps), FAN_OUT_WIDTH)

    def test_batch_payer_excludes_self_and_respects_recipient_cap(self):
        for b in self.bots:
            if b["archetype"] == "batch_payer":
                recipients = b["params"]["recipients"]
                self.assertNotIn(b["index"], recipients)
                self.assertLessEqual(len(recipients), BATCH_PAYER_RECIPIENTS)

    def test_fan_in_sink_group_has_exactly_one_sink(self):
        roles = [b["params"]["role"] for b in self.bots if b["archetype"] == "fan_in_sink"]
        self.assertEqual(roles.count("sink"), 1)

    def test_market_maker_partnerships_are_mutual(self):
        by_index = {b["index"]: b for b in self.bots}
        for b in self.bots:
            if b["archetype"] == "market_maker":
                for partner_idx in b["params"]["partners"]:
                    self.assertIn(b["index"], by_index[partner_idx]["params"]["partners"])

    def test_smallest_valid_roster_does_not_crash(self):
        # n=7 is the smallest count where market_maker gets >= 2 bots
        # (round-robin puts one market_maker at index 1, the next at index
        # 6) -- every archetype needs to degrade gracefully at this size.
        build_roster(7, compute_intervals(7))


class MarketMakerPairingGuardTests(unittest.TestCase):
    """n in [2, 6] round-robins exactly one market_maker bot with nobody to
    pair with -- compute_round_outputs would later divide by zero on an
    empty partners list deep in a worker thread, mid-run. build_roster must
    reject this upfront instead, before any funding happens."""

    def test_rejects_bot_counts_with_an_unpairable_market_maker(self):
        configure_fake_sighash()
        for n in range(2, 7):
            with self.subTest(n=n):
                with self.assertRaises(ValueError):
                    build_roster(n, compute_intervals(n))

    def test_accepts_bot_count_where_market_maker_is_pairable(self):
        configure_fake_sighash()
        build_roster(7, compute_intervals(7))  # must not raise


class ComputeRoundOutputsTests(unittest.TestCase):
    def setUp(self):
        configure_fake_sighash()
        self.bots = build_roster(20, compute_intervals(20))

    def test_every_bot_produces_nonempty_outputs_that_avoid_self(self):
        for b in self.bots:
            outputs = compute_round_outputs(b, 0)
            self.assertGreater(len(outputs), 0)
            for target_idx, capacity in outputs:
                self.assertNotEqual(target_idx, b["index"])
                self.assertGreater(capacity, 0)

    def test_batch_payer_sends_to_every_recipient_in_one_round(self):
        for b in self.bots:
            if b["archetype"] == "batch_payer":
                outputs = compute_round_outputs(b, 0)
                self.assertEqual(len(outputs), len(b["params"]["recipients"]))

    def test_market_maker_alternates_capacity_by_round_parity(self):
        for b in self.bots:
            if b["archetype"] == "market_maker":
                _, cap_even = compute_round_outputs(b, 0)[0]
                _, cap_odd = compute_round_outputs(b, 1)[0]
                self.assertNotEqual(cap_even, cap_odd)

    def test_periodic_cycles_through_its_counterparty_list(self):
        for b in self.bots:
            if b["archetype"] == "periodic":
                cps = b["params"]["counterparties"]
                targets = [compute_round_outputs(b, r)[0][0] for r in range(len(cps))]
                self.assertEqual(sorted(targets), sorted(cps))


if __name__ == "__main__":
    unittest.main()
