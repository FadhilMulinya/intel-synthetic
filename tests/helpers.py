"""Shared fixtures for the test suite: fake devnet-discovered sighash state,
standing in for what ckb.discover_system_script() would fill in from a real
running devnet, so tests can build/sign transactions and addresses offline."""
import ckb

FAKE_CODE_HASH = "0x" + "ab" * 32
FAKE_DEP_TX_HASH = "0x" + "11" * 32


def configure_fake_sighash():
    ckb.config.SIGHASH_CODE_HASH = FAKE_CODE_HASH
    ckb.config.SIGHASH_DEP = {"tx_hash": FAKE_DEP_TX_HASH, "index": 0, "dep_type": "dep_group"}
