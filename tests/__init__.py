"""Making tests/ a package lets every test file do plain top-level imports
(`import ckb`, `import archetypes`, ...) regardless of which directory the
test runner is invoked from -- this runs before any test module in the
package is imported, so the path fix is always in place first."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
