"""Run with FFN_CLI_TEST_SOURCE pointing to the image or installed ffn-cli."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('installer',Path(__file__).with_name('install-cli.py'))
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
SOURCE=Path(os.environ.get('FFN_CLI_TEST_SOURCE','/usr/local/bin/ffn-cli'))


@unittest.skipUnless(SOURCE.is_file(),'FFN_CLI_TEST_SOURCE is required outside the appliance')
class InstalledShell(unittest.TestCase):
    def setUp(self):
        self.source=installer.merge(SOURCE.read_text())
        self.module=types.ModuleType('ffn_cli_test')
        exec(compile(self.source,'ffn-cli','exec'),self.module.__dict__)
        self.session=self.module.Session.__new__(self.module.Session)
        self.session.mode='operational';self.session.token='test-session'

    def test_idempotent_and_help_present(self):
        self.assertEqual(installer.merge(self.source),self.source)
        with contextlib.redirect_stdout(io.StringIO()) as output:self.session._cmd_help([])
        self.assertIn('help platform',output.getvalue())
        self.assertIn('show platform fe100 status',output.getvalue())

    def test_whitespace_dispatch_and_help_use_authenticated_extension(self):
        module=Mock();module.handle.return_value=True
        with patch.object(self.session,'_platform_cli',return_value=module):
            for line in ('  show   platform fe100 status','help platform fe100','? platform'):
                self.assertTrue(self.session._dispatch(line))
        self.assertEqual(module.handle.call_args_list[0].args,
                         ('show platform fe100 status',self.module.api,'test-session'))
        self.assertEqual(module.handle.call_count,3)

    def test_error_and_bad_quote_keep_session_alive(self):
        with patch.object(self.session,'_platform_cli',side_effect=RuntimeError('extension unavailable')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(self.session._dispatch('show platform fe100 status'))
        self.assertIn('Platform command failed',output.getvalue())
        with patch.object(self.session,'_platform_cli') as loader,contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.session._dispatch('show platform "'))
        loader.assert_not_called()

    def test_completion_uses_extension_without_api(self):
        module=Mock();module.complete.return_value=['recovery']
        with patch.object(self.session,'_platform_cli',return_value=module), \
                patch.object(self.module.readline,'get_line_buffer',return_value='show platform fe100 re'), \
                patch.object(self.module.readline,'get_begidx',return_value=20), \
                patch.object(self.module,'api') as api:
            self.assertEqual(self.session._complete('re',0),'recovery')
            self.assertIsNone(self.session._complete('re',1))
        module.complete.assert_called_with('show platform fe100 ','re')
        api.assert_not_called()

    def test_configurable_endpoint_and_environment_override(self):
        if not hasattr(self.module,'_cli_api_endpoint'):
            self.assertIn('# FFN console transport:',self.source);return
        with patch.dict(self.module.os.environ,{},clear=True), \
                patch.object(self.module.Path,'read_text',return_value='{"api_url":"https://localhost:9443/"}'):
            self.assertEqual(self.module._cli_api_endpoint(),'https://localhost:9443')
        with patch.dict(self.module.os.environ,{'FFN_CLI_API':'https://localhost:10443'}), \
                patch.object(self.module.Path,'read_text') as read:
            self.assertEqual(self.module._cli_api_endpoint(),'https://localhost:10443')
            read.assert_not_called()
        with patch.dict(self.module.os.environ,{},clear=True), \
                patch.object(self.module.Path,'read_text',side_effect=FileNotFoundError):
            self.assertEqual(self.module._cli_api_endpoint(),'https://127.0.0.1:8443')

    def test_invalid_endpoint_fails_without_printing_credentials(self):
        if not hasattr(self.module,'_cli_api_endpoint'):
            self.assertIn('# FFN console transport:',self.source);return
        for value in ('https://user:secret@localhost','file:///etc/passwd','https://localhost/?secret=yes'):
            with patch.dict(self.module.os.environ,{'FFN_CLI_API':value}):
                with self.assertRaises(RuntimeError) as raised:self.module._cli_api_endpoint()
            self.assertNotIn('secret',str(raised.exception))


if __name__=='__main__':unittest.main()
