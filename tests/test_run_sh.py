"""Fast, offline checks of run.sh's argument parsing and safety guards.
Deliberately never lets run.sh reach the point of starting a devnet or a
real simulate.py run -- every case here is rejected by validation before
that point (or, in the existing-run case, refused before either starts).
Machines that happen to have offckb on PATH and a devnet already running
would otherwise let a fully-valid argument set through to a real,
long-running background process, so there is deliberately no test here
that passes a complete, valid argument set to run.sh."""
import os
import re
import stat
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(REPO_ROOT, "run.sh")
RUN_SH_SOURCE = open(RUN_SH).read()


def run(*args):
    return subprocess.run(["bash", RUN_SH, *args], capture_output=True, text=True, timeout=10)


class RunShTests(unittest.TestCase):
    def test_script_is_executable(self):
        mode = os.stat(RUN_SH).st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_help_prints_usage_and_exits_zero(self):
        result = run("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_missing_required_args_exits_nonzero(self):
        result = run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_non_numeric_bots_is_rejected(self):
        result = run("--bots", "abc", "--hours", "1", "--txs", "10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--bots must be a positive integer", result.stderr)

    def test_non_numeric_txs_is_rejected(self):
        result = run("--bots", "10", "--hours", "1", "--txs", "abc")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--txs must be a positive integer", result.stderr)

    def test_non_numeric_hours_is_rejected(self):
        result = run("--bots", "10", "--hours", "soon", "--txs", "10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--hours must be a number", result.stderr)

    def test_hours_validation_regex_accepts_fractional_values(self):
        # Checked against run.sh's own --hours regex directly (rather than
        # by actually invoking run.sh with a fully-valid argument set) so
        # this can never accidentally fall through validation and start a
        # real devnet + simulate.py run on a machine that happens to have
        # offckb on PATH.
        match = re.search(r'"\$HOURS" =~ (\S+) \]\]', RUN_SH_SOURCE)
        self.assertIsNotNone(match, "could not find the --hours validation regex in run.sh")
        pattern = match.group(1)
        self.assertRegex("0.5", pattern)
        self.assertRegex("24", pattern)
        self.assertNotRegex("soon", pattern)

    def test_unknown_argument_is_rejected(self):
        result = run("--bots", "10", "--hours", "1", "--txs", "10", "--bogus", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown argument", result.stderr)


class RunShExistingOutputGuardTests(unittest.TestCase):
    """Uses its own throwaway directory rather than a real run under data/
    -- data/ is gitignored, so a fresh clone won't have any prior runs to
    point at, but the guard still needs to be exercised."""

    def setUp(self):
        self.out_name = "test_existing_out_guard_tmp"
        self.out_dir = os.path.join(REPO_ROOT, "data", self.out_name)
        os.makedirs(self.out_dir, exist_ok=True)

    def tearDown(self):
        if os.path.isdir(self.out_dir):
            os.rmdir(self.out_dir)

    def test_refuses_to_reuse_an_existing_output_directory(self):
        result = run("--bots", "5", "--hours", "1", "--txs", "5", "--out", self.out_name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertIn("refusing to overwrite", result.stderr)


if __name__ == "__main__":
    unittest.main()
