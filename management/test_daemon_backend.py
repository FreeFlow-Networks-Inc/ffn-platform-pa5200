import unittest
from unittest.mock import AsyncMock
from daemon_backend import execute

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_does_not_apply(self):
        backend=AsyncMock();backend.run.return_value={'revision':7}
        payload={'revision':7,'port':1,'enabled':False}
        self.assertEqual(await execute('faceplate','validate',payload,backend),{'validated':True})
        backend.run.assert_awaited_once_with('faceplate','status')
        backend.run.reset_mock()
        await execute('faceplate','apply',payload,backend)
        self.assertEqual(backend.run.await_args.args,('faceplate','set',payload))

    async def test_stale_and_raw_requests_rejected(self):
        backend=AsyncMock();backend.run.return_value={'revision':8}
        for payload in ({'revision':7,'port':1,'enabled':True},{'revision':8,'shell':'reboot'}):
            with self.assertRaises(ValueError): await execute('faceplate','apply',payload,backend)
        self.assertFalse(any(c.args[1]=='set' for c in backend.run.await_args_list))

if __name__=='__main__': unittest.main()
