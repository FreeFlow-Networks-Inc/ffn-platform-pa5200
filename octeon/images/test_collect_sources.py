import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collect_sources as sources


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'root';self.cache=self.base/'cache';self.cache.mkdir()
        p=self.root/'var/lib/dpkg/status';p.parent.mkdir(parents=True)
        p.write_text('Package: helper\nVersion: 2+b1\nStatus: install ok installed\nBuilt-Using: runtime (= 3)\n')
        for name,version in [('helper','2'),('runtime','3')]:
            archive=self.cache/(name+'.tar.xz');archive.write_bytes((name+version).encode())
            (self.cache/(name+'.dsc')).write_text('Format: 3.0 (native)\nSource: '+name+'\nVersion: '+version+
                '\nChecksums-Sha256:\n '+hashlib.sha256(archive.read_bytes()).hexdigest()+' '+str(archive.stat().st_size)+' '+archive.name+'\n')

    def test_exact_sources_include_static_runtime_and_strip_binary_rebuild_suffix(self):
        with patch.object(sources.subprocess,'run') as run:
            report=sources.collect(self.root,[self.cache],self.base/'out')
        run.assert_not_called()
        self.assertEqual([(x['source'],x['version']) for x in report['sources']],[('helper','2'),('runtime','3')])

    def test_modified_cached_archive_is_rejected(self):
        (self.cache/'helper.tar.xz').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'checksum'):
            sources.collect(self.root,[self.cache],self.base/'out')

    def test_clearsigned_descriptor_retains_source_identity(self):
        p=self.cache/'helper.dsc';p.write_text('-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n'+p.read_text()+
            '\n-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n')
        self.assertEqual(sources.descriptor(p)['Source'],'helper')


if __name__=='__main__': unittest.main()
