"""ckb.rpc talks to a live devnet, so these mock `requests` entirely --
they check the JSON-RPC envelope shape and error handling, not the network."""
import unittest
from unittest import mock

import ckb.rpc as rpc_mod


class CallRpcTests(unittest.TestCase):
    def test_returns_result_and_sends_proper_envelope(self):
        with mock.patch.object(rpc_mod, "requests") as mock_requests:
            mock_requests.post.return_value.json.return_value = {"result": 42}
            mock_requests.post.return_value.raise_for_status.return_value = None
            result = rpc_mod.call_rpc("get_tip_block_number")

        self.assertEqual(result, 42)
        payload = mock_requests.post.call_args.kwargs["json"]
        self.assertEqual(payload["method"], "get_tip_block_number")
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertEqual(payload["params"], [])

    def test_forwards_params(self):
        with mock.patch.object(rpc_mod, "requests") as mock_requests:
            mock_requests.post.return_value.json.return_value = {"result": None}
            mock_requests.post.return_value.raise_for_status.return_value = None
            rpc_mod.call_rpc("get_transaction", ["0xdead"])

        payload = mock_requests.post.call_args.kwargs["json"]
        self.assertEqual(payload["params"], ["0xdead"])

    def test_raises_runtime_error_on_rpc_error_response(self):
        with mock.patch.object(rpc_mod, "requests") as mock_requests:
            mock_requests.post.return_value.json.return_value = {"error": {"message": "boom"}}
            mock_requests.post.return_value.raise_for_status.return_value = None
            with self.assertRaises(RuntimeError):
                rpc_mod.call_rpc("send_transaction")

    def test_request_ids_increment(self):
        with mock.patch.object(rpc_mod, "requests") as mock_requests:
            mock_requests.post.return_value.json.return_value = {"result": None}
            mock_requests.post.return_value.raise_for_status.return_value = None
            rpc_mod.call_rpc("get_tip_block_number")
            rpc_mod.call_rpc("get_tip_block_number")

        ids = [c.kwargs["json"]["id"] for c in mock_requests.post.call_args_list]
        self.assertEqual(ids[1], ids[0] + 1)


class PublicPackageApiTests(unittest.TestCase):
    """Regression test for the ckb.rpc name collision: a submodule import
    must resolve to the actual module, not to a re-exported function that
    happens to share its name."""

    def test_ckb_rpc_submodule_is_importable_as_a_module(self):
        import ckb

        self.assertTrue(hasattr(ckb.rpc, "call_rpc"))
        self.assertEqual(ckb.rpc.__name__, "ckb.rpc")


if __name__ == "__main__":
    unittest.main()
