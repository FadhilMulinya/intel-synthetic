"""secp256k1_pure has no compiled dependency to fall back on if it's wrong,
so these check it against known values and its own verification equation
independently of the code path that produced the signature."""
import unittest

from secp256k1_pure import G, N, P, _inv, _point_add, privkey_to_pubkey_compressed, scalar_mult, sign_recoverable


class PubkeyDerivationTests(unittest.TestCase):
    def test_generator_pubkey_matches_known_value(self):
        # privkey=1 -> pubkey is G itself; this compressed encoding is a
        # widely published constant, independent of this implementation.
        pub = privkey_to_pubkey_compressed(1)
        self.assertEqual(
            pub.hex(),
            "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
        )

    def test_pubkey_is_33_bytes_with_valid_prefix(self):
        pub = privkey_to_pubkey_compressed(424242)
        self.assertEqual(len(pub), 33)
        self.assertIn(pub[0], (2, 3))


class ScalarMultTests(unittest.TestCase):
    def test_doubling_matches_point_add(self):
        self.assertEqual(scalar_mult(2, G), _point_add(G, G))

    def test_scalar_mult_is_additive(self):
        k1, k2 = 12345, 67890
        lhs = _point_add(scalar_mult(k1, G), scalar_mult(k2, G))
        rhs = scalar_mult(k1 + k2, G)
        self.assertEqual(lhs, rhs)

    def test_scalar_mult_by_group_order_is_identity(self):
        self.assertIsNone(scalar_mult(N, G))


class SignRecoverableTests(unittest.TestCase):
    def test_deterministic_for_same_inputs(self):
        digest = b"\x01" * 32
        self.assertEqual(sign_recoverable(5, digest), sign_recoverable(5, digest))

    def test_differs_across_privkeys(self):
        digest = b"\x01" * 32
        self.assertNotEqual(sign_recoverable(5, digest), sign_recoverable(6, digest))

    def test_low_s_normalized(self):
        sig = sign_recoverable(424242, b"\x02" * 32)
        s = int.from_bytes(sig[32:64], "big")
        self.assertLessEqual(s, N // 2)

    def test_signature_shape(self):
        sig = sign_recoverable(7, b"\x03" * 32)
        self.assertEqual(len(sig), 65)
        self.assertIn(sig[64], (0, 1))

    def test_signature_satisfies_ecdsa_verification_equation(self):
        # Independent check: R' = s^-1*z*G + s^-1*r*Pub must have x == r.
        # Deliberately not reusing sign_recoverable's own math to verify.
        privkey = 999331
        pub = privkey_to_pubkey_compressed(privkey)
        digest = b"\x04" * 32
        sig = sign_recoverable(privkey, digest)
        r = int.from_bytes(sig[0:32], "big")
        s = int.from_bytes(sig[32:64], "big")
        z = int.from_bytes(digest, "big")

        x = int.from_bytes(pub[1:], "big")
        y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
        if (y % 2 == 0) != (pub[0] == 2):
            y = P - y
        pubkey_point = (x, y)

        s_inv = _inv(s, N)
        u1 = (z * s_inv) % N
        u2 = (r * s_inv) % N
        recovered = _point_add(scalar_mult(u1, G), scalar_mult(u2, pubkey_point))
        self.assertEqual(recovered[0] % N, r)


if __name__ == "__main__":
    unittest.main()
