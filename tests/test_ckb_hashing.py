import hashlib
import unittest

from ckb.hashing import _bech32_hrp_expand, _bech32_polymod, _BECH32M_CONST, bech32m_encode, ckbhash, lock_arg_to_address
from tests.helpers import configure_fake_sighash


class CkbhashTests(unittest.TestCase):
    def test_matches_personalized_blake2b(self):
        data = b"hello ckb"
        expected = hashlib.blake2b(data, digest_size=32, person=b"ckb-default-hash").digest()
        self.assertEqual(ckbhash(data), expected)

    def test_is_32_bytes_and_deterministic(self):
        h1, h2 = ckbhash(b"x"), ckbhash(b"x")
        self.assertEqual(len(h1), 32)
        self.assertEqual(h1, h2)

    def test_different_inputs_differ(self):
        self.assertNotEqual(ckbhash(b"a"), ckbhash(b"b"))


class Bech32mTests(unittest.TestCase):
    def test_checksum_is_valid_per_bech32m_spec(self):
        encoded = bech32m_encode("ckt", bytes(21))
        hrp, data_part = encoded.split("1", 1)
        charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        values = [charset.index(c) for c in data_part]
        # A well-formed bech32m string's full polymod (hrp-expand + data +
        # checksum) always equals the bech32m constant.
        self.assertEqual(_bech32_polymod(_bech32_hrp_expand(hrp) + values), _BECH32M_CONST)

    def test_corrupting_one_character_breaks_the_checksum(self):
        encoded = bech32m_encode("ckt", bytes(21))
        hrp, data_part = encoded.split("1", 1)
        charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        values = [charset.index(c) for c in data_part]
        values[0] = (values[0] + 1) % 32
        self.assertNotEqual(_bech32_polymod(_bech32_hrp_expand(hrp) + values), _BECH32M_CONST)


class LockArgToAddressTests(unittest.TestCase):
    def test_starts_with_hrp_and_is_stable(self):
        configure_fake_sighash()
        lock_arg = "0x" + "22" * 20
        addr1 = lock_arg_to_address(lock_arg)
        addr2 = lock_arg_to_address(lock_arg)
        self.assertTrue(addr1.startswith("ckt1"))
        self.assertEqual(addr1, addr2)

    def test_different_lock_args_give_different_addresses(self):
        configure_fake_sighash()
        addr_a = lock_arg_to_address("0x" + "aa" * 20)
        addr_b = lock_arg_to_address("0x" + "bb" * 20)
        self.assertNotEqual(addr_a, addr_b)


if __name__ == "__main__":
    unittest.main()
