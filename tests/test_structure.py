import unittest

import used_pc_finder


class StructureTests(unittest.TestCase):
    def test_package_imports(self):
        self.assertEqual(used_pc_finder.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
