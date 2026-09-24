import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import plane_images as p
from ffn_payload import sign_manifest, ffn_ed25519


class PlaneImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.seed = bytes(range(32)); self.public = ffn_ed25519.publickey(self.seed)
        (self.root / 'server').write_text('url=https://patch.example:8445\n')
        (self.root / 'pub').write_text(self.public.hex())
        self.client = p.ImageClient(self.root / 'state', self.root / 'server', self.root / 'pub')
        self.payloads, self.files = {}, {}
        for role in p.ROLES:
            details = dict(platform='pa5200', role=role, architecture='mips64eb', runtime_abi=1,
                           core_commit='a'*40, platform_commit='b'*40, kernel_release='6.18.49-'+role,
                           hardware_boot_verified=False, operating_system={'distribution':'debian'})
            archive = self.root / (role + '.tar.xz')
            with tarfile.open(archive, 'w:xz') as tar:
                raw = json.dumps(details).encode(); info = tarfile.TarInfo('image.json'); info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
            digest = p.sha(archive)
            item = dict(details, sha256=digest, size=archive.stat().st_size, version='2026.1',
                        published=100, file='pa5200-'+role+'-'+digest+'.tar.xz')
            self.payloads['pa5200-'+role] = item
            self.files[item['file']] = archive.read_bytes()
        self.sign()
        self.requests = []
        self.launch = patch.object(self.client, 'launch').start()
        self.addCleanup(patch.stopall)

    def sign(self):
        self.catalog = {'payloads':self.payloads, 'updated':100}
        self.catalog['signature'], self.catalog['sig_alg'] = sign_manifest(self.catalog, seed=self.seed)

    def fetch(self, url, limit):
        return json.dumps(self.catalog).encode()

    def stream(self, url, path, size):
        self.requests.append(url); path.write_bytes(self.files[url.rsplit('/',1)[-1]])

    def run_job(self, role, operation):
        state = self.client.state(role)
        job = self.client.submit({'role':role, 'operation':operation, 'revision':state['revision'],
                                 'sha256':(state.get('available') or {}).get('sha256','') if operation=='download' else ''})
        return self.client.work(role, job['id'], fetch=self.fetch, stream=self.stream)

    def test_independent_roles_and_no_activation(self):
        self.assertEqual(self.run_job('cp','check')['status'], 'succeeded')
        self.assertEqual(self.run_job('cp','download')['status'], 'succeeded')
        self.assertEqual(len(self.requests), 1)
        self.assertIn('pa5200-cp-', self.requests[0])
        self.assertEqual(self.client.state('dp'), {'revision':0})
        self.assertFalse(self.client.state('cp')['staged']['activated'])
        self.assertEqual(self.run_job('dp','check')['status'], 'succeeded')
        self.assertEqual(self.run_job('dp','download')['status'], 'succeeded')
        self.assertEqual(len(self.requests), 2)

    def test_unpublished_role_is_not_an_error(self):
        del self.payloads['pa5200-dp']; self.sign()
        self.assertEqual(self.run_job('dp','check')['status'], 'succeeded')
        self.assertIsNone(self.client.state('dp')['available'])
        with self.assertRaisesRegex(ValueError, 'digest'): self.run_job('dp','download')

    def test_tampering_fails_without_replacing_download(self):
        self.run_job('cp','check'); self.run_job('cp','download')
        previous = self.client.state('cp')['staged']
        self.payloads['pa5200-cp']['published'] += 1
        self.assertEqual(self.run_job('cp','check')['status'], 'failed')
        self.assertEqual(self.client.state('cp')['staged'], previous)

    def test_truncated_or_wrong_role_bytes_never_staged(self):
        self.run_job('cp','check')
        item = self.payloads['pa5200-cp']; self.files[item['file']] = b'bad'
        self.assertEqual(self.run_job('cp','download')['status'], 'failed')
        self.assertNotIn('staged', self.client.state('cp'))
        self.assertEqual(list((self.client.root/'cp/cache').iterdir()), [])
        item = dict(self.payloads['pa5200-dp'], role='cp')
        with self.assertRaisesRegex(ValueError, 'descriptor mismatch'): p.inspect_image(self.root/'dp.tar.xz',item)

    def test_stale_revision_and_unknown_fields_rejected(self):
        request = dict(role='cp', operation='check', revision=2, sha256='')
        with self.assertRaisesRegex(ValueError,'changed'): self.client.submit(request)
        request['revision']=0; request['command']='reboot'
        with self.assertRaises(ValueError): self.client.submit(request)

    def test_role_and_platform_confusion_rejected(self):
        for field, value in [('role','dp'),('platform','other'),('architecture','mips64el'),('runtime_abi',True),('file','../../escape')]:
            old=self.payloads['pa5200-cp'][field];self.payloads['pa5200-cp'][field]=value;self.sign()
            with self.subTest(field=field), self.assertRaises(ValueError):p.metadata(self.catalog,'cp',self.public)
            self.payloads['pa5200-cp'][field]=old
        with self.assertRaises(ValueError):self.client.state('../dp')

    def test_catalog_rollback_rejected(self):
        self.run_job('cp','check');self.payloads['pa5200-cp']['published']=99;self.sign()
        self.assertEqual(self.run_job('cp','check')['status'],'failed')
        self.assertEqual(self.client.state('cp')['high_water']['published'],100)

    def test_changed_server_requires_new_check(self):
        self.run_job('cp','check');(self.root/'server').write_text('url=https://other.example\n')
        self.assertEqual(self.run_job('cp','download')['status'],'failed')
        self.assertEqual(self.requests,[])

    def test_same_role_busy_other_role_allowed(self):
        job=self.client.submit(dict(role='cp',operation='check',revision=0,sha256=''))
        with self.assertRaisesRegex(ValueError,'running'):
            self.client.submit(dict(role='cp',operation='check',revision=1,sha256=''))
        self.client.submit(dict(role='dp',operation='check',revision=0,sha256=''))
        self.assertEqual(self.launch.call_count,2)

    def test_cached_corruption_detected(self):
        self.run_job('cp','check');self.run_job('cp','download')
        Path(self.client.state('cp')['staged']['path']).write_bytes(b'corrupt')
        self.assertEqual(self.run_job('cp','download')['status'],'failed')
        self.assertEqual(len(self.requests),1)

    def test_http_server_refused(self):
        (self.root/'server').write_text('url=http://patch.example\n')
        with self.assertRaises(ValueError):self.run_job('cp','check')

    def test_publisher_keeps_other_plane_and_code_patch(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('publish_plane',Path(__file__).parents[1]/'octeon/images/publish_patch.py')
        publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
        build=self.root/'build';build.mkdir()
        manifest={k:self.payloads['pa5200-cp'][k] for k in ('platform','architecture','runtime_abi','core_commit','platform_commit','hardware_boot_verified')}
        manifest['assets']=[]
        for role in p.ROLES:
            item=self.payloads['pa5200-'+role];name='ffn-pa5200-'+role+'.tar.xz'
            (build/name).write_bytes(self.files[item['file']])
            manifest['assets'].append(dict(role=role,name=name,sha256=item['sha256'],size=item['size'],kernel_release=item['kernel_release']))
        (build/'manifest.json').write_text(json.dumps(manifest))
        seed=self.root/'seed';seed.write_text(self.seed.hex());seed.chmod(0o600)
        feed=self.root/'feed';feed.mkdir()
        prior={'payloads':{'patch':{'version':'core-baseline'}},'updated':1}
        prior['signature'],prior['sig_alg']=sign_manifest(prior,seed=self.seed)
        p.save(feed/'manifest.json',prior)
        if os.name == 'nt':
            self.skipTest('Publisher enforces Unix signing-key permissions')
        for role in p.ROLES:publisher.publish(feed,build,role,'2026.1',seed,self.root/'pub')
        final=json.loads((feed/'manifest.json').read_text())
        self.assertEqual(set(final['payloads']),{'patch','pa5200-cp','pa5200-dp'})
        self.assertEqual(final['payloads']['patch']['version'],'core-baseline')
        self.assertEqual(p.metadata(final,'cp',self.public)['role'],'cp')
        self.assertEqual(p.metadata(final,'dp',self.public)['role'],'dp')

    def test_launch_failure_recorded(self):
        self.launch.side_effect=OSError('systemd missing')
        result=self.client.submit(dict(role='cp',operation='check',revision=0,sha256=''))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.client.state('cp')['job']['status'],'failed')

    def test_validate_has_no_filesystem_or_job_side_effects(self):
        result=self.client.validate(dict(role='cp',operation='check',revision=0,sha256=''))
        self.assertTrue(result['valid'])
        self.assertFalse(self.client.root.exists())
        self.launch.assert_not_called()

    def test_real_plane_dispatch_validates_then_records_submission(self):
        import asyncio
        from ffn_planed import Plane
        seen=[]
        async def runner(argv, data, timeout):
            request=json.loads(data);action=Path(argv[0]).name;seen.append(action)
            result=self.client.validate(request) if action=='validate' else self.client.submit(request)
            return result
        config={'role':'mp','commands':{'plane-images':{a:[str(self.root/a)] for a in ('status','validate','apply')}}}
        plane=Plane(config,self.root/'journal')
        plane.runner=runner
        self.addCleanup(plane.db.close)
        import uuid
        request=dict(v=1,id=str(uuid.uuid4()),resource='plane-images',action='apply',
                     payload=dict(role='cp',operation='check',revision=0,sha256=''))
        result=asyncio.run(plane.dispatch(request))
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['state'],'applied')
        self.assertEqual(seen,['validate','apply'])
        again=asyncio.run(plane.dispatch(request))
        self.assertEqual(result,again)
        self.assertEqual(self.launch.call_count,1)

    def test_interrupted_worker_retryable(self):
        job=self.client.submit(dict(role='cp',operation='check',revision=0,sha256=''))
        state=self.client.state('cp');state['job']['created_at']=0;p.save(self.client.root/'cp/state.json',state)
        with patch.object(self.client,'alive',return_value=False):
            self.assertEqual(self.client.status()['roles']['cp']['job']['status'],'interrupted')
            retry=self.client.submit(dict(role='cp',operation='check',revision=1,sha256=''))
        self.assertNotEqual(job['id'],retry['id'])


if __name__ == '__main__': unittest.main()
