"""bot_worker talks to the devnet only through ckb.build_and_sign_transfer_from_cell
and ckb.send_transaction, so both are mocked here -- no network involved,
and interval_sec=0 keeps the loop instant."""
import queue
import threading
import unittest
from unittest import mock

import worker


def make_bot(index, archetype="periodic", interval_sec=0):
    return {
        "index": index,
        "privkey": "0xprivkey",
        "lock_arg": f"0xlock{index}",
        "address": f"addr{index}",
        "archetype": archetype,
        "params": {"interval_sec": interval_sec, "capacity_ckb": 70, "counterparties": [1]},
        "initial_cell": {"out_point": {"tx_hash": "0xseed", "index": "0x0"}, "capacity": 100_000_000_000},
    }


def fake_build_and_sign():
    """Stand-in for ckb.build_and_sign_transfer_from_cell: hands back a
    fresh change cell each call so bot_worker's chain keeps advancing."""
    call = {"n": 0}

    def _build(privkey, lock_arg, cell, outputs, fee_shannon=1000):
        call["n"] += 1
        sent = sum(capacity for _, capacity in outputs)
        change_cell = {
            "out_point": {"tx_hash": f"0xtx{call['n']}", "index": "0x1"},
            "capacity": cell["capacity"] - sent - fee_shannon,
        }
        return {"witnesses": ["0x00"]}, f"0xtx{call['n']}", change_cell, 200

    return _build


class BotWorkerTests(unittest.TestCase):
    def setUp(self):
        worker.ckb.config.SIGHASH_DEP = {"tx_hash": "0x" + "11" * 32, "index": 0, "dep_type": "dep_group"}
        self.confirm_queue = queue.Queue()
        self.counters = {"submitted": 0, "confirmed": 0}
        self.counters_lock = threading.Lock()

    def test_submits_exactly_txs_per_bot_rounds(self):
        bot = make_bot(0)
        bots = [bot, make_bot(1)]
        with mock.patch.object(worker.ckb, "build_and_sign_transfer_from_cell", side_effect=fake_build_and_sign()), \
             mock.patch.object(worker.ckb, "send_transaction", return_value=None):
            worker.bot_worker(bot, bots, txs_per_bot=3, confirm_queue=self.confirm_queue,
                               counters=self.counters, counters_lock=self.counters_lock)

        self.assertEqual(self.counters["submitted"], 3)
        self.assertEqual(self.confirm_queue.qsize(), 3)

    def test_record_shape_matches_explorer_like_schema(self):
        bot = make_bot(0)
        bots = [bot, make_bot(1)]
        with mock.patch.object(worker.ckb, "build_and_sign_transfer_from_cell", side_effect=fake_build_and_sign()), \
             mock.patch.object(worker.ckb, "send_transaction", return_value=None):
            worker.bot_worker(bot, bots, txs_per_bot=1, confirm_queue=self.confirm_queue,
                               counters=self.counters, counters_lock=self.counters_lock)

        tx_hash, record = self.confirm_queue.get()
        self.assertEqual(tx_hash, "0xtx1")
        self.assertEqual(record["archetype"], "periodic")
        self.assertEqual(record["is_cellbase"], False)
        self.assertEqual(len(record["display_inputs"]), 1)
        self.assertEqual(record["display_inputs"][0]["address_hash"], "addr0")
        # one recipient output + one change output back to the sender
        self.assertEqual(len(record["display_outputs"]), 2)
        self.assertEqual(record["display_outputs"][-1]["address_hash"], "addr0")

    def test_batch_payer_produces_multiple_display_outputs_in_one_record(self):
        bot = make_bot(0, archetype="batch_payer")
        bot["params"] = {"interval_sec": 0, "capacity_ckb": 70, "recipients": [1, 2, 3]}
        bots = [bot] + [make_bot(i) for i in (1, 2, 3)]
        with mock.patch.object(worker.ckb, "build_and_sign_transfer_from_cell", side_effect=fake_build_and_sign()), \
             mock.patch.object(worker.ckb, "send_transaction", return_value=None):
            worker.bot_worker(bot, bots, txs_per_bot=1, confirm_queue=self.confirm_queue,
                               counters=self.counters, counters_lock=self.counters_lock)

        _, record = self.confirm_queue.get()
        self.assertEqual(len(record["display_outputs"]), 4)  # 3 recipients + change

    def test_stops_early_and_submits_nothing_when_no_change_cell_is_returned(self):
        bot = make_bot(0)
        bots = [bot, make_bot(1)]

        def _no_change(privkey, lock_arg, cell, outputs, fee_shannon=1000):
            return {"witnesses": ["0x00"]}, "0xtx1", None, 200

        with mock.patch.object(worker.ckb, "build_and_sign_transfer_from_cell", side_effect=_no_change), \
             mock.patch.object(worker.ckb, "send_transaction", return_value=None):
            worker.bot_worker(bot, bots, txs_per_bot=5, confirm_queue=self.confirm_queue,
                               counters=self.counters, counters_lock=self.counters_lock)

        self.assertEqual(self.counters["submitted"], 0)
        self.assertEqual(self.confirm_queue.qsize(), 0)

    def test_retries_after_a_transient_send_failure(self):
        bot = make_bot(0)
        bots = [bot, make_bot(1)]
        build = fake_build_and_sign()
        attempts = {"n": 0}

        def _flaky_send(tx_json):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("devnet hiccup")

        with mock.patch.object(worker.ckb, "build_and_sign_transfer_from_cell", side_effect=build), \
             mock.patch.object(worker.ckb, "send_transaction", side_effect=_flaky_send), \
             mock.patch.object(worker.time, "sleep", return_value=None):
            worker.bot_worker(bot, bots, txs_per_bot=1, confirm_queue=self.confirm_queue,
                               counters=self.counters, counters_lock=self.counters_lock)

        # first attempt failed (not counted), second attempt succeeded
        self.assertEqual(self.counters["submitted"], 1)
        self.assertEqual(attempts["n"], 2)


if __name__ == "__main__":
    unittest.main()
