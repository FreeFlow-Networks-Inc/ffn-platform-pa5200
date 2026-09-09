"""Regression for a real CINT redeclaration error followed by a DONE marker."""
import pathlib
import tempfile
import unittest
from ffn_bcmd import op_cint_run

class CintResultTest(unittest.TestCase):
    def run_recipe(self, transcript):
        with tempfile.TemporaryDirectory() as root:
            pathlib.Path(root, 'ffn_bcm_front_init.c').touch()
            class Chip:
                cfg_dir = root
                def run(self, command, timeout):
                    return transcript
            return op_cint_run(Chip(), {'script': 'ffn_bcm_front_init.c'})

    def test_redeclaration_cannot_report_completed(self):
        result = self.run_recipe(
            "** /tmp/bcmcfg/ffn_bcm_qsfp.c:62: error: identifier 'a' redeclared\n"
            "FFN_FRONT_DONE\n")
        self.assertFalse(result['completed'])

    def test_clean_completion(self):
        self.assertTrue(self.run_recipe('FFN_FRONT speed port=16 mbps=10000 rv=0\n'
                                        'FFN_FRONT_DONE\n')['completed'])

    def test_api_failure(self):
        self.assertFalse(self.run_recipe('FFN_FRONT_FAILED errors=1\n')['completed'])

if __name__ == '__main__':
    unittest.main()
