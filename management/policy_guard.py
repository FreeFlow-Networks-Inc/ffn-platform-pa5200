"""Optional MP pre-commit barrier for the selected PA-5200 extension."""
import hashlib
import json
import subprocess


def verify_drain(state,result,digest):
    if not isinstance(state,dict) or not isinstance(result,dict):raise RuntimeError('invalid policy acknowledgement')
    revision=state.get('revision')
    if type(revision) is not int or not 0<=revision<2**64-1:raise RuntimeError('invalid policy owner revision')
    if (result.get('phase')!='blocked' or type(result.get('sessions')) is not int or result['sessions']!=0 or
            result.get('recovery_required') is not False or result.get('admission_enabled') is not False or
            result.get('revision')!=revision+1 or result.get('digest')!=digest or
            result.get('control_owner')!=state.get('control_owner')):
        raise RuntimeError('hardware sessions did not drain')
    return {'revision':result['revision'],'drained':True,'admission_enabled':False}


def before_commit(candidate_bytes, run=subprocess.run):
    if not isinstance(candidate_bytes,bytes):raise ValueError('candidate bytes required')
    command='/usr/local/sbin/ffn-cp'
    ld='/usr/local/lib64:/usr/local/lib64/3p:/usr/local/lib/ffn/owner-deps'
    def call(op,payload):
        # The vendor search path contains an obsolete SQLite with the same
        # SONAME. Pin Debian's library for the durable Python journal.
        r=run([command,'env LD_PRELOAD=/usr/lib/mips64-linux-gnuabi64/libsqlite3.so.0 LD_LIBRARY_PATH='+ld+
              ' python3 /usr/local/sbin/ffn_fe100_policy_control.py '+op],
              input=json.dumps(payload),text=True,capture_output=True,timeout=30,check=True)
        value=json.loads(r.stdout)
        if not isinstance(value,dict):raise RuntimeError('invalid policy owner response')
        return value
    state=call('status',{})
    revision=state.get('revision')
    if type(revision) is not int or not 0<=revision<2**64-1:raise RuntimeError('invalid policy owner revision')
    digest=hashlib.sha256(candidate_bytes).hexdigest()
    result=call('replace',{'revision':revision,'digest':digest})
    return verify_drain(state,result,digest)
