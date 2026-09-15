import hashlib
import json
import subprocess
import unittest
from types import SimpleNamespace
from policy_guard import before_commit


class Guard(unittest.TestCase):
    def test_drain_uses_current_revision_and_exact_candidate_digest(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append((argv,json.loads(kwargs['input'])))
            result={'revision':8} if len(calls)==1 else dict(revision=9,phase='blocked',sessions=0,recovery_required=False)
            return SimpleNamespace(stdout=json.dumps(result))
        self.assertEqual(before_commit(b'<config/>',run),dict(revision=9,drained=True,admission_enabled=False))
        self.assertTrue(calls[0][0][1].endswith(' status'))
        self.assertTrue(calls[1][0][1].endswith(' replace'))
        self.assertEqual(calls[1][1],dict(revision=8,digest=hashlib.sha256(b'<config/>').hexdigest()))

    def test_failed_or_incomplete_drain_propagates(self):
        for result in ({'phase':'draining','sessions':0,'recovery_required':False},
                       {'phase':'blocked','sessions':1,'recovery_required':False},
                       {'phase':'blocked','sessions':0,'recovery_required':True},[]):
            answers=iter([{'revision':8},result])
            with self.assertRaises(RuntimeError):
                before_commit(b'<config/>',lambda *a,**k:SimpleNamespace(stdout=json.dumps(next(answers))))
        def timeout(*a,**k):raise subprocess.TimeoutExpired(a[0],30)
        with self.assertRaises(subprocess.TimeoutExpired):before_commit(b'<config/>',timeout)


if __name__=='__main__':unittest.main()
