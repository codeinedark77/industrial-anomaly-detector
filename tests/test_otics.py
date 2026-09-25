import unittest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestOtIcs(unittest.TestCase):
    def test_dirs(self):
        self.assertTrue(os.path.exists('api'))
        self.assertTrue(os.path.exists('detection'))
        self.assertTrue(os.path.exists('simulator'))

if __name__ == '__main__':
    unittest.main()
