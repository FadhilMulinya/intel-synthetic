import unittest

from ckb.molecule import mol_bytes, mol_dynamic, mol_fixvec, mol_u32, mol_u64, mol_witness_args


class IntEncodingTests(unittest.TestCase):
    def test_u32_little_endian(self):
        self.assertEqual(mol_u32(1), b"\x01\x00\x00\x00")
        self.assertEqual(mol_u32(0x01020304), b"\x04\x03\x02\x01")

    def test_u64_little_endian(self):
        self.assertEqual(mol_u64(1), b"\x01" + b"\x00" * 7)
        self.assertEqual(len(mol_u64(0)), 8)


class BytesAndFixvecTests(unittest.TestCase):
    def test_bytes_has_u32_length_prefix(self):
        encoded = mol_bytes(b"abc")
        self.assertEqual(encoded[:4], mol_u32(3))
        self.assertEqual(encoded[4:], b"abc")

    def test_empty_bytes(self):
        self.assertEqual(mol_bytes(b""), mol_u32(0))

    def test_fixvec_count_prefix_and_concatenation(self):
        items = [b"\x01\x02", b"\x03\x04", b"\x05\x06"]
        encoded = mol_fixvec(items)
        self.assertEqual(encoded[:4], mol_u32(3))
        self.assertEqual(encoded[4:], b"".join(items))


class DynamicTests(unittest.TestCase):
    def test_total_size_header_matches_actual_length(self):
        encoded = mol_dynamic([b"AAAA", b"BB", b"CCCCCC"])
        total_size = int.from_bytes(encoded[0:4], "little")
        self.assertEqual(total_size, len(encoded))

    def test_offsets_point_at_the_right_parts(self):
        parts = [b"AAAA", b"BB", b"CCCCCC"]
        encoded = mol_dynamic(parts)
        offsets = [int.from_bytes(encoded[4 + 4 * i:8 + 4 * i], "little") for i in range(len(parts))]
        for part, offset in zip(parts, offsets):
            self.assertEqual(encoded[offset:offset + len(part)], part)

    def test_empty_parts_list(self):
        encoded = mol_dynamic([])
        self.assertEqual(int.from_bytes(encoded[0:4], "little"), 4)


class WitnessArgsTests(unittest.TestCase):
    def test_populated_lock_is_larger_than_empty(self):
        empty = mol_witness_args()
        with_lock = mol_witness_args(lock=b"\x99" * 65)
        self.assertGreater(len(with_lock), len(empty))

    def test_all_none_fields_still_produces_valid_dynamic_header(self):
        encoded = mol_witness_args()
        total_size = int.from_bytes(encoded[0:4], "little")
        self.assertEqual(total_size, len(encoded))


if __name__ == "__main__":
    unittest.main()
