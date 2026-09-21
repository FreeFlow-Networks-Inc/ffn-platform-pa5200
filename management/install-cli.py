#!/usr/bin/env python3
"""Install selected PA5200 CLI dispatch, help and completion without replacing the shell."""
import argparse
from pathlib import Path
import shutil
import time

ENDPOINT = '''def _cli_api_endpoint():
    value = os.getenv('FFN_CLI_API')
    if not value:
        try:
            settings = json.loads(Path('/etc/ffn-ngfw/cli.json').read_text())
            value = settings['api_url']
        except FileNotFoundError:
            value = 'https://127.0.0.1:8443'
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError('Invalid CLI endpoint configuration in /etc/ffn-ngfw/cli.json') from error
    if not isinstance(value, str):
        raise RuntimeError('CLI api_url must be an HTTP(S) base URL')
    parsed = urllib.parse.urlparse(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or parsed.path not in ('', '/') or
            any(c.isspace() for c in value)):
        raise RuntimeError('CLI api_url must be an HTTP(S) base URL without credentials')
    return value.rstrip('/')


FFN_API = _cli_api_endpoint()
'''

OLD_HOOK = """        # FFN selected-platform command hook
        if line.startswith(('show platform', 'request platform')):
            import importlib.util
            spec = importlib.util.spec_from_file_location('ffn_cli_platform', '/opt/ffn-platforms/pa5200-management/cli_extension.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if module.handle(line, api, self.token):
                return True
"""

LOADER = '''    @staticmethod
    def _platform_cli():
        import importlib.util
        path = Path('/opt/ffn-platforms/pa5200-management/cli_extension.py')
        if not path.is_file():
            raise RuntimeError('Selected platform CLI extension is unavailable')
        spec = importlib.util.spec_from_file_location('ffn_cli_platform', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

'''

DISPATCH = '''        # FFN platform CLI v2: parse first, report failures without ending the session.
        if tokens[:2] in (['show', 'platform'], ['request', 'platform'], ['help', 'platform'], ['?', 'platform']):
            try:
                if self._platform_cli().handle(shlex.join(tokens), api, self.token):
                    return True
            except (ValueError, OSError, RuntimeError, APIError) as error:
                print('Platform command failed: ' + str(error))
                return True
'''

COMPLETION = '''        # FFN platform CLI completion uses local vocabulary, never the API.
        if self.mode == 'operational':
            try:
                prefix = readline.get_line_buffer()[:readline.get_begidx()]
                matches = self._platform_cli().complete(prefix, text)
                if matches is not None:
                    return matches[state] if state < len(matches) else None
            except (ValueError, OSError, RuntimeError, AttributeError):
                pass
'''


def once(source, old, new):
    if source.count(old) != 1: raise ValueError('CLI source changed at ' + old.splitlines()[0])
    return source.replace(old, new, 1)


def merge(source):
    if 'def _cli_api_endpoint():' not in source and '# FFN console transport:' not in source:
        source = once(source, 'FFN_API = os.getenv("FFN_CLI_API", "https://127.0.0.1:8443")\n', ENDPOINT)
    if '# FFN platform CLI v2:' in source:
        if not all(block in source for block in (LOADER, DISPATCH, COMPLETION)):
            raise ValueError('Existing platform CLI integration differs')
        return source
    if '# FFN selected-platform command hook' in source:
        source = once(source, OLD_HOOK, '')
    source = once(source, '    def _complete(self, text, state):\n',
                  LOADER + '    def _complete(self, text, state):\n' + COMPLETION)
    source = once(source, '        readline.set_completer(self._complete)\n',
                  '        readline.set_completer_delims(" \\t\\n")\n        readline.set_completer(self._complete)\n')
    source = once(source, '        cmd, args = tokens[0], tokens[1:]\n',
                  DISPATCH + '        cmd, args = tokens[0], tokens[1:]\n')
    source = once(source, '                ("show", "Display system/config information"),\n',
                  '                ("show", "Display system/config information"),\n'
                  '                ("show platform fe100 status", "FE100 driver, policy and recovery health"),\n'
                  '                ("help platform", "Platform command help"),\n')
    compile(source, 'ffn-cli', 'exec')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, default=Path('/usr/local/bin/ffn-cli'))
    args = parser.parse_args()
    old = args.cli.read_text(); updated = merge(old)
    if old == updated:
        print('FFN-CLI platform integration already current'); return
    backup = Path('/var/backups/ffn/cli-' + str(time.time_ns()))
    backup.mkdir(parents=True); shutil.copy2(args.cli, backup / 'ffn-cli')
    temp = args.cli.with_name(args.cli.name + '.platform-new')
    temp.write_text(updated); shutil.copystat(args.cli, temp); temp.replace(args.cli)
    print('Updated FFN-CLI; backup ' + str(backup) + '. Reconnect existing CLI sessions to load it.')


if __name__ == '__main__': main()
