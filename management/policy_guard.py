"""Optional MP pre-commit barrier for the selected PA-5200 extension."""
import hashlib
import json
import subprocess


def before_commit(candidate_bytes, run=subprocess.run):
    if not isinstance(candidate_bytes,bytes):raise ValueError('candidate bytes required')
    command='/usr/local/sbin/ffn-cp'
    ld='/opt/ffn-compat/tmp/dpfs/usr/local/lib64:/opt/ffn-compat/tmp/dpfs/usr/local/lib64/3p:/opt/ffn-compat/tmp/dpfs/usr/lib64'
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
    result=call('replace',{'revision':state['revision'],'digest':hashlib.sha256(candidate_bytes).hexdigest()})
    if result.get('phase')!='blocked' or result.get('sessions')!=0 or result.get('recovery_required') is not False:
        raise RuntimeError('hardware sessions did not drain')
    return {'revision':result['revision'],'drained':True,'admission_enabled':False}
