from __future__ import annotations

import tomllib
import unittest

from support import WHEEL_BOT_DIR


class ProjectMetadataTests(unittest.TestCase):
    def test_runtime_dependencies_include_option_quote_imports(self):
        pyproject = tomllib.loads((WHEEL_BOT_DIR / "pyproject.toml").read_text())
        deps = pyproject["project"]["dependencies"]

        self.assertTrue(any(dep.startswith("alpaca-py") for dep in deps))
        self.assertTrue(any(dep.startswith("pytz") for dep in deps))


if __name__ == "__main__":
    unittest.main()
