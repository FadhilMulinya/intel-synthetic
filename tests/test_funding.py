"""funding.py talks to `offckb` (subprocess) and the devnet RPC (via ckb);
both are mocked here so the tests run instantly with no external process."""
import unittest
from unittest import mock

import funding


class ParseOffckbAccountsTests(unittest.TestCase):
    def test_parses_multiple_accounts_from_offckb_output(self):
        fake_stdout = """
- "#": 0
  lock_arg: 0xaaaa
  address: ckt1qaaa
  privkey: 0x1111
- "#": 1
  lock_arg: 0xbbbb
  address: ckt1qbbb
  privkey: 0x2222
"""
        with mock.patch.object(funding.subprocess, "run") as mock_run:
            mock_run.return_value.stdout = fake_stdout
            accounts = funding.parse_offckb_accounts()

        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["address"], "ckt1qaaa")
        self.assertEqual(accounts[0]["lock_arg"], "0xaaaa")
        self.assertEqual(accounts[1]["privkey"], "0x2222")

    def test_raises_on_empty_output(self):
        with mock.patch.object(funding.subprocess, "run") as mock_run:
            mock_run.return_value.stdout = ""
            with self.assertRaises(RuntimeError):
                funding.parse_offckb_accounts()


class EstimateFundingShannonTests(unittest.TestCase):
    def test_scales_up_with_more_transactions(self):
        self.assertGreater(funding.estimate_funding_shannon(100), funding.estimate_funding_shannon(10))

    def test_always_positive(self):
        self.assertGreater(funding.estimate_funding_shannon(1), 0)


class GetBlockTimestampMsTests(unittest.TestCase):
    def setUp(self):
        funding._block_timestamp_cache.clear()

    def test_caches_after_first_lookup(self):
        with mock.patch.object(funding.ckb, "get_block_by_number") as mock_get_block:
            mock_get_block.return_value = {"header": {"timestamp": "0x64"}}
            ts1 = funding.get_block_timestamp_ms(123)
            ts2 = funding.get_block_timestamp_ms(123)

        self.assertEqual(ts1, 0x64)
        self.assertEqual(ts2, 0x64)
        mock_get_block.assert_called_once()

    def test_different_blocks_each_trigger_a_lookup(self):
        with mock.patch.object(funding.ckb, "get_block_by_number") as mock_get_block:
            mock_get_block.return_value = {"header": {"timestamp": "0x1"}}
            funding.get_block_timestamp_ms(1)
            funding.get_block_timestamp_ms(2)

        self.assertEqual(mock_get_block.call_count, 2)


class WaitForConfirmationTests(unittest.TestCase):
    def test_returns_block_number_once_committed(self):
        responses = [
            {"tx_status": {"status": "pending"}},
            {"tx_status": {"status": "committed", "block_number": "0x2a"}},
        ]
        with mock.patch.object(funding.ckb, "get_transaction", side_effect=responses):
            block_number = funding.wait_for_confirmation("0xdead", timeout=5, poll=0)
        self.assertEqual(block_number, 42)

    def test_raises_runtime_error_on_rejected(self):
        with mock.patch.object(funding.ckb, "get_transaction",
                                return_value={"tx_status": {"status": "rejected"}}):
            with self.assertRaises(RuntimeError):
                funding.wait_for_confirmation("0xdead", timeout=5, poll=0)

    def test_raises_timeout_error_when_never_confirmed(self):
        with mock.patch.object(funding.ckb, "get_transaction",
                                return_value={"tx_status": {"status": "pending"}}):
            with self.assertRaises(TimeoutError):
                funding.wait_for_confirmation("0xdead", timeout=0.05, poll=0.01)


if __name__ == "__main__":
    unittest.main()
