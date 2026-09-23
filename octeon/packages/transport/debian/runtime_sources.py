"""Find exact Debian source versions for the statically linked runtimes."""
from pathlib import Path
import subprocess
import sys

prefix = sys.argv[1]
packages = []
for library in ('libc.a', 'libgcc.a'):
    filename = subprocess.check_output([prefix + 'gcc', '-print-file-name=' + library], text=True).strip()
    filename = str(Path(filename).resolve(strict=True))
    owner = subprocess.check_output(['dpkg-query', '-S', filename], text=True).strip().split(': ', 1)[0]
    if '\n' in owner:
        raise SystemExit('Ambiguous static runtime owner')
    source = subprocess.check_output(['dpkg-query', '-W', '-f=${source:Package} (= ${source:Version})', owner], text=True)
    if not source or source.endswith('(= )'):
        raise SystemExit('Static runtime source version unavailable')
    packages.append(source)
print('ffn:Built-Using=' + ', '.join(sorted(set(packages))))
