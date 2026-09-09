#!/usr/bin/env python3
"""Correct the host-inferred sqv dependency without rebuilding unchanged APT."""
from pathlib import Path
import re
import hashlib
import shutil
import subprocess
import tempfile

repo = Path('/tmp/repo')
originals = list((repo / 'pool/main/a/apt').glob('apt_3.3.3_mips64.deb'))
assert len(originals) == 1, originals
original = originals[0]
backup = repo / 'archive/apt-metadata-input'
backup.mkdir(parents=True, exist_ok=True)
shutil.copy2(original, backup / original.name)
output_dir = Path('/tmp/ffn-packages')
output_dir.mkdir(exist_ok=True)
output = output_dir / 'apt_3.3.3+ffn1_mips64.deb'
work = Path(tempfile.mkdtemp(prefix='apt-metadata-', dir=output_dir))
subprocess.run(['dpkg-deb', '-R', str(original), str(work)], check=True)
method = work / 'usr/lib/apt/methods/gpgv'
assert method.is_file(), 'Original APT must contain its supported gpgv backend'
control = work / 'DEBIAN/control'
text = control.read_text()
assert re.search(r'^Architecture: mips64$', text, re.M)
assert re.search(r'^Version: 3\.3\.3$', text, re.M)
assert 'sqv (>= 1.3.0)' in text
text = text.replace('sqv (>= 1.3.0)', 'gpgv', 1)
text = re.sub(r'^Version: 3\.3\.3$', 'Version: 3.3.3+ffn1', text, flags=re.M)
text = re.sub(r'^Source:.*\n', '', text, flags=re.M)
text = 'Source: apt (3.3.3)\n' + text
control.write_text(text)
note = work / 'usr/share/doc/apt/README.ffn-port'
note.write_text(
    'Local MIPS64 BE packaging correction: depend on gpgv instead of sqv.\n'
    'Upstream APT 3.3.3 already selects its gpgv backend when sqv is absent.\n'
    'Executable files are unchanged from the source-built MIPS64 package.\n')
with (work / 'DEBIAN/md5sums').open('a') as sums:
    sums.write(hashlib.md5(note.read_bytes()).hexdigest() + '  usr/share/doc/apt/README.ffn-port\n')
subprocess.run(['dpkg-deb', '--root-owner-group', '-b', str(work), str(output)], check=True)
subprocess.run(['reprepro', '-b', str(repo), 'includedeb', 'rebootstrap', str(output)], check=True)
print('Imported:', output)
