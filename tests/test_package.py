import importlib
import unittest


class CorePackageTest(unittest.TestCase):
    def test_automation_package_imports(self):
        try:
            module = importlib.import_module("automation")
        except ModuleNotFoundError as exc:
            self.fail(f"automation package should import: {exc}")

        self.assertEqual(module.__name__, "automation")


if __name__ == "__main__":
    unittest.main()
