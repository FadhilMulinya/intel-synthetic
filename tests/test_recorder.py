import json
import os
import queue
import tempfile
import threading
import unittest
from unittest import mock

import recorder


class TxWriterTests(unittest.TestCase):
    def test_writes_address_header_then_records_as_ndjson(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "add_1.json")
            w = recorder.TxWriter(path, "addr1")
            w.write({"transaction_hash": "0x1"})
            w.write({"transaction_hash": "0x2"})
            w.close()
            with open(path) as f:
                lines = [json.loads(l) for l in f]

        self.assertEqual(lines[0], {"address": "addr1"})
        self.assertEqual([l["transaction_hash"] for l in lines[1:]], ["0x1", "0x2"])

    def test_reopening_an_existing_file_does_not_duplicate_the_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "add_1.json")
            recorder.TxWriter(path, "addr1").close()
            recorder.TxWriter(path, "addr1").close()
            with open(path) as f:
                lines = list(f)
        self.assertEqual(len(lines), 1)


class BotTxPathTests(unittest.TestCase):
    def test_index_plus_one_naming_convention(self):
        self.assertEqual(recorder.bot_tx_path("data/out", 0), os.path.join("data/out", "add_1.json"))
        self.assertEqual(recorder.bot_tx_path("data/out", 299), os.path.join("data/out", "add_300.json"))


class ConfirmationWorkerTests(unittest.TestCase):
    def test_confirmed_record_gets_block_info_and_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "add_1.json")
            writer = recorder.TxWriter(path, "addr1")
            writers = {"addr1": writer}
            work_queue = queue.Queue()
            counters = {"submitted": 0, "confirmed": 0}
            counters_lock = threading.Lock()

            record = {"transaction_hash": "0xabc", "display_inputs": [{"address_hash": "addr1"}]}
            work_queue.put(("0xabc", record))
            work_queue.put(None)  # sentinel: tells the worker to stop

            with mock.patch.object(recorder, "wait_for_confirmation", return_value=7), \
                 mock.patch.object(recorder, "get_block_timestamp_ms", return_value=123456):
                recorder.confirmation_worker(work_queue, writers, counters, counters_lock)
            writer.close()

            with open(path) as f:
                lines = [json.loads(l) for l in f]

        self.assertEqual(counters["confirmed"], 1)
        self.assertEqual(lines[1]["block_number"], "7")
        self.assertEqual(lines[1]["block_timestamp"], "123456")
        self.assertEqual(lines[1]["tx_status"], "committed")

    def test_a_failed_confirmation_is_skipped_without_crashing(self):
        writers = {"addr1": mock.Mock()}
        work_queue = queue.Queue()
        counters = {"submitted": 0, "confirmed": 0}
        counters_lock = threading.Lock()
        work_queue.put(("0xabc", {"transaction_hash": "0xabc", "display_inputs": [{"address_hash": "addr1"}]}))
        work_queue.put(None)

        with mock.patch.object(recorder, "wait_for_confirmation", side_effect=TimeoutError("nope")):
            recorder.confirmation_worker(work_queue, writers, counters, counters_lock)

        self.assertEqual(counters["confirmed"], 0)
        writers["addr1"].write.assert_not_called()


class PatchOutputStatusTests(unittest.TestCase):
    def test_marks_spent_outputs_dead_and_leaves_unspent_ones_live(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "add_1.json")
            with open(path, "w") as f:
                f.write(json.dumps({"address": "addr1"}) + "\n")
                f.write(json.dumps({
                    "transaction_hash": "0xaa",
                    "display_inputs": [{"generated_tx_hash": "0xseed", "cell_index": "0"}],
                    "display_outputs": [{"cell_index": "0", "status": "live"}],
                }) + "\n")
                f.write(json.dumps({
                    "transaction_hash": "0xbb",
                    "display_inputs": [{"generated_tx_hash": "0xaa", "cell_index": "0"}],
                    "display_outputs": [{"cell_index": "0", "status": "live"}],
                }) + "\n")

            recorder.patch_output_status(path)
            with open(path) as f:
                lines = [json.loads(l) for l in f]

        self.assertEqual(lines[1]["display_outputs"][0]["status"], "dead")
        self.assertEqual(lines[1]["display_outputs"][0]["consumed_tx_hash"], "0xbb")
        self.assertEqual(lines[2]["display_outputs"][0]["status"], "live")

    def test_header_line_is_preserved_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "add_1.json")
            with open(path, "w") as f:
                f.write(json.dumps({"address": "addr1"}) + "\n")
                f.write(json.dumps({
                    "transaction_hash": "0xaa", "display_inputs": [], "display_outputs": [],
                }) + "\n")
            recorder.patch_output_status(path)
            with open(path) as f:
                header = json.loads(next(f))
        self.assertEqual(header, {"address": "addr1"})


if __name__ == "__main__":
    unittest.main()
