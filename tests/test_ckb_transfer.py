"""build_and_sign_transfer_from_cell takes its input cell explicitly (that's
the whole point -- see its docstring), so it's fully testable offline with
no RPC involved."""
import unittest

import ckb
from ckb.transfer import build_and_sign_transfer_from_cell
from tests.helpers import configure_fake_sighash


class BuildAndSignTransferFromCellTests(unittest.TestCase):
    def setUp(self):
        configure_fake_sighash()
        self.privkey, _, self.lock_arg, _ = ckb.generate_keypair()
        _, _, self.to_lock_arg, _ = ckb.generate_keypair()
        self.input_cell = {
            "out_point": {"tx_hash": "0x" + "aa" * 32, "index": "0x0"},
            # large enough that leftover change clears MIN_CELL_CAPACITY_SHANNON (61 CKB)
            "capacity": 1000 * ckb.SHANNONS_PER_CKB,
        }

    def test_builds_one_recipient_output_plus_change(self):
        outputs = [(self.to_lock_arg, 70 * ckb.SHANNONS_PER_CKB)]
        tx_json, tx_hash, change_cell, tx_size = build_and_sign_transfer_from_cell(
            self.privkey, self.lock_arg, self.input_cell, outputs, fee_shannon=1000)

        self.assertTrue(tx_hash.startswith("0x"))
        self.assertEqual(len(tx_json["inputs"]), 1)
        self.assertEqual(len(tx_json["outputs"]), 2)  # recipient + change
        self.assertIsNotNone(change_cell)
        expected_change = self.input_cell["capacity"] - 70 * ckb.SHANNONS_PER_CKB - 1000
        self.assertEqual(change_cell["capacity"], expected_change)
        self.assertGreater(tx_size, 0)

    def test_multi_output_batch_transfer(self):
        _, _, to2, _ = ckb.generate_keypair()
        outputs = [(self.to_lock_arg, 10 * ckb.SHANNONS_PER_CKB), (to2, 10 * ckb.SHANNONS_PER_CKB)]
        tx_json, _, change_cell, _ = build_and_sign_transfer_from_cell(
            self.privkey, self.lock_arg, self.input_cell, outputs, fee_shannon=1400)
        self.assertEqual(len(tx_json["outputs"]), 3)  # 2 recipients + change
        self.assertIsNotNone(change_cell)

    def test_no_change_output_when_spend_is_exact(self):
        exact = self.input_cell["capacity"] - 1000
        outputs = [(self.to_lock_arg, exact)]
        tx_json, _, change_cell, _ = build_and_sign_transfer_from_cell(
            self.privkey, self.lock_arg, self.input_cell, outputs, fee_shannon=1000)
        self.assertIsNone(change_cell)
        self.assertEqual(len(tx_json["outputs"]), 1)

    def test_insufficient_balance_raises(self):
        outputs = [(self.to_lock_arg, 10_000 * ckb.SHANNONS_PER_CKB)]
        with self.assertRaises(RuntimeError):
            build_and_sign_transfer_from_cell(self.privkey, self.lock_arg, self.input_cell, outputs)

    def test_dust_change_below_min_cell_capacity_raises(self):
        # Leaves 1 shannon of change -- nonzero but far below MIN_CELL_CAPACITY_SHANNON.
        almost_all = self.input_cell["capacity"] - 1000 - 1
        outputs = [(self.to_lock_arg, almost_all)]
        with self.assertRaises(RuntimeError):
            build_and_sign_transfer_from_cell(self.privkey, self.lock_arg, self.input_cell, outputs, fee_shannon=1000)

    def test_witnesses_present_for_signed_input(self):
        outputs = [(self.to_lock_arg, 10 * ckb.SHANNONS_PER_CKB)]
        tx_json, _, _, _ = build_and_sign_transfer_from_cell(
            self.privkey, self.lock_arg, self.input_cell, outputs, fee_shannon=1000)
        self.assertEqual(len(tx_json["witnesses"]), 1)
        self.assertTrue(tx_json["witnesses"][0].startswith("0x"))


if __name__ == "__main__":
    unittest.main()
