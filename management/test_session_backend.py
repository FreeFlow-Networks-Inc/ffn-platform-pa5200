import unittest
from unittest.mock import patch
import session_backend as backend


class SessionBackendTests(unittest.TestCase):
    def reply(self,argv,payload):
        if argv==backend.DP:
            return dict(nonce=payload['nonce'],available=True,producer={'pid':4},policy={'revision':1})
        return dict(payload['observation'],hardware_admission=False)

    def test_only_fixed_read_only_dp_cp_path(self):
        with patch.object(backend,'call',side_effect=self.reply) as call:
            result=backend.execute('status',{})
        self.assertFalse(result['hardware_admission']);self.assertEqual(call.call_count,2)
        self.assertEqual(call.call_args_list[0].args[0],backend.DP)
        self.assertEqual(call.call_args_list[1].args[0],backend.CP)
        for action,payload in [('apply',{}),('status',{'nonce':'user'}),('status',[]),('delete',{})]:
            with patch.object(backend,'call') as call,self.assertRaises(ValueError):backend.execute(action,payload)
            call.assert_not_called()

    def test_round_trip_or_identity_change_rejected(self):
        with patch.object(backend,'call',side_effect=self.reply),patch.object(backend.time,'monotonic',side_effect=[0,16]):
            with self.assertRaises(ValueError):backend.execute('status',{})
        def changed(argv,payload):
            result=self.reply(argv,payload)
            if argv==backend.CP:result['producer']={'pid':5}
            return result
        with patch.object(backend,'call',side_effect=changed),self.assertRaises(ValueError):backend.execute('status',{})


if __name__=='__main__':unittest.main()
