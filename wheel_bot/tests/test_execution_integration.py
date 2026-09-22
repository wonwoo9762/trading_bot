"""Keep real-library checks isolated from the legacy suite's global stubs."""

from pathlib import Path
import subprocess
import sys
import unittest


class RealLibraryIntegrationTests(unittest.TestCase):
    def test_execution_safety_with_real_libraries(self):
        scenarios = Path(__file__).parent / "integration" / "execution_safety_scenarios.py"
        result = subprocess.run(
            [sys.executable, "-B", str(scenarios)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
