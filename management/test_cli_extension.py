import contextlib
import io
import unittest
from unittest.mock import Mock
from cli_extension import handle

class CLITests(unittest.TestCase):
    def test_shared_authenticated_path(self):
        api=Mock(return_value={'control':{'trace':['mp']}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle('show platform faceplate',api,'session'))
            api.assert_called_with('/api/system/runtime/faceplate',token='session')
            self.assertTrue(handle('request platform faceplate set \'{"revision":7,"port":1,"enabled":false}\'',api,'session'))
            api.assert_called_with('/api/system/runtime/faceplate/set',method='POST',body={'revision':7,'port':1,'enabled':False},token='session')
        self.assertFalse(handle('show system info',api,'session'))
        with self.assertRaises(ValueError):handle('request platform shell run {}',api,'session')

if __name__=='__main__':unittest.main()
