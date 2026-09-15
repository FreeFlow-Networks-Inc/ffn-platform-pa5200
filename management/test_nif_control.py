import unittest
from unittest.mock import patch
from ffn_nif_control import validate, links


class NIFTests(unittest.TestCase):
    def test_enable_is_revisioned_and_requires_installed_service(self):
        cfg={'revision':3,'enabled':False}
        result=validate(cfg,{'revision':3,'enabled':True},{'LoadState':'loaded'})
        self.assertEqual(result,{'revision':4,'enabled':True})
        for request in ({'revision':2,'enabled':True},{'revision':True,'enabled':True},
                        {'revision':3,'enabled':False},{'revision':3,'enabled':1},
                        {'revision':3,'enabled':True,'command':'reset'}):
            with self.assertRaises(ValueError): validate(cfg,request,{'LoadState':'loaded'})
        with self.assertRaises(ValueError): validate(cfg,{'revision':3,'enabled':True},{'LoadState':'not-found'})
    def test_missing_link_observation_is_not_success(self):
        with patch('ffn_nif_control.subprocess.run',side_effect=OSError('private diagnostic')):
            result=links()
        self.assertIsNone(result['physical_links_up'])
        self.assertNotIn('private diagnostic',str(result))


if __name__=='__main__': unittest.main()
