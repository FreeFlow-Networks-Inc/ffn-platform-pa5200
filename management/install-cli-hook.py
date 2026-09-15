#!/usr/bin/env python3
"""Install the selected platform's authenticated CLI command hook with a backup."""
from pathlib import Path
import os
import shutil

path=Path('/usr/local/bin/ffn-cli')
text=path.read_text()
marker='# FFN selected-platform command hook'
anchor='        # Parse\n'
if marker not in text:
    assert text.count(anchor)==1, 'Unsupported CLI version; no changes made'
    hook='''        # FFN selected-platform command hook
        if line.startswith(('show platform', 'request platform')):
            import importlib.util
            spec = importlib.util.spec_from_file_location('ffn_cli_platform', '/opt/ffn-platforms/pa5200-management/cli_extension.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if module.handle(line, api, self.token):
                return True
'''
    updated=text.replace(anchor,hook+anchor)
    compile(updated,str(path),'exec')
    backup=path.with_name('ffn-cli.pre-platform-daemon')
    assert not backup.exists(), 'Backup already exists; inspect before replacing'
    shutil.copy2(path,backup)
    temp=path.with_name('ffn-cli.new');temp.write_text(updated)
    shutil.copymode(path,temp);os.replace(temp,path)
