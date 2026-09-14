"""Execute only the new registry cases; count mismatch refuses execution."""

import json
import sys
import unittest
from pathlib import Path


def main():
    config = json.loads(Path('/work/job.input.json').read_bytes())
    suite = unittest.defaultTestLoader.discover('/work', pattern='test_global_registry.py')
    assert suite.countTestCases() == config['expected_tests'], suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
