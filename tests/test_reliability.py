"""Offline regressions: never connect to a VPS or GitHub."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy_nodes
import merge_subscription
import ssh_run

BASH = (r'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt'
        else shutil.which('bash'))
CFG = dict(VPS_HOST='example.test', VPS_USER='root', DOMAIN='example.test',
           PANEL_PORT='46821', PANEL_PATH='panel', API_TOKEN="token'with$chars",
           MERGED_EMAIL='merged', MERGED_SUBID='subscription', SUB_PORT='2096',
           SUB_PATH='sub', REALITY_PORT='443', HY2_PORT='443', TUIC_PORT='8443')


class ConfigTests(unittest.TestCase):
    def test_env_only_options_and_empty_override(self):
        with patch.object(ssh_run, '_candidate_env_files', return_value=[]), \
             patch.dict(os.environ, {'DISABLE_IPV6': '0', 'MERGED_SUBID': '',
                                    'PANEL_PASS': "a'b", 'NODE_PREFIX': 'custom'}, clear=True):
            self.assertEqual(ssh_run.load_env(), {'DISABLE_IPV6': '0',
                'MERGED_SUBID': '', 'PANEL_PASS': "a'b", 'NODE_PREFIX': 'custom'})

    def test_bom_quotes_and_empty_override(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'deploy.env'
            path.write_text('DOMAIN=test\nPANEL_PASS=endswith\'\nPANEL_PATH="secret"\n',
                            encoding='utf-8-sig')
            with patch.object(ssh_run, '_candidate_env_files', return_value=[str(path)]), \
                 patch.dict(os.environ, {'PANEL_PATH': ''}, clear=True):
                self.assertEqual(ssh_run.load_env(), dict(DOMAIN='test',
                    PANEL_PASS="endswith'", PANEL_PATH=''))

    def test_reject_shell_in_environment_key(self):
        with self.assertRaises(ValueError):
            ssh_run.env_exports({'X; touch injected': 'value'})

    @unittest.skipUnless(BASH, 'bash required')
    def test_shell_values_round_trip(self):
        value = "quotes'\" $HOME $(echo injected) `echo injected`\n中文"
        cmd = ssh_run.env_exports({'VALUE': value}) + '\nprintf %s "$VALUE"'
        result = subprocess.run([BASH, '-c', cmd], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode('utf-8'), value)


class DeploymentTests(unittest.TestCase):
    def run_main(self, module, failure_command):
        calls = []
        def run(client, command, **kwargs):
            calls.append(command)
            return (23, '', 'simulated failure') if failure_command in command else (0, '', '')
        cli = Mock()
        with patch.object(module, 'load_env', return_value=CFG.copy()), \
             patch.object(module, 'get_client', return_value=cli), \
             patch.object(module, 'run', side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = module.main()
        cli.close.assert_called_once()
        return rc, calls

    def test_deploy_script_is_plain_bash_and_payload_fields_are_strings(self):
        rc, calls = self.run_main(deploy_nodes, 'bash /root/.xui-skill/_deploy.sh')
        self.assertEqual(rc, 23)
        script_upload = next(c for c in calls if '> /root/.xui-skill/_deploy.sh' in c)
        decoded = base64.b64decode(re.search(r"echo '([^']+)'", script_upload)[1]).decode()
        self.assertEqual(decoded, deploy_nodes.REMOTE_SCRIPT)
        for name in ('reality', 'hy2', 'tuic'):
            command = next(c for c in calls if f'> /root/.xui-skill/in_{name}.json' in c)
            payload = json.loads(base64.b64decode(re.search(r"echo '([^']+)'", command)[1]))
            for key in ('settings', 'streamSettings', 'sniffing'):
                self.assertIsInstance(payload[key], str)
                json.loads(payload[key])
            if name == 'tuic':
                self.assertIs(json.loads(payload['settings'])['server']['zero_rtt_handshake'], False)
            if name == 'hy2':
                self.assertNotIn('serverName', json.loads(payload['streamSettings'])['tlsSettings']['settings'])
        self.assertFalse(any('node-credentials.json' in c for c in calls))

    def test_success_persists_readback_keys_and_reports_save_failure(self):
        for save_rc in (0, 23):
            with self.subTest(save_rc=save_rc):
                calls = []
                def run(client, command, **kwargs):
                    calls.append(command)
                    if "python3 - <<'PYEOF'" in command:
                        return 0, json.dumps(dict(reality_priv='actual-private',
                                                  reality_pub='actual-public')), ''
                    if 'cat > /root/.xui-skill/node-credentials.json' in command:
                        return save_rc, '', 'save failed' if save_rc else ''
                    return 0, '', ''
                with patch.object(deploy_nodes, 'load_env', return_value=CFG.copy()), \
                     patch.object(deploy_nodes, 'get_client', return_value=Mock()), \
                     patch.object(deploy_nodes, 'run', side_effect=run), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(deploy_nodes.main(), save_rc)
                save = next(c for c in calls if 'cat > /root/.xui-skill/node-credentials.json' in c)
                credentials = json.loads(save.split("<<'EOF'\n", 1)[1].split('\nEOF', 1)[0])
                self.assertEqual(credentials['reality_priv'], 'actual-private')
                self.assertEqual(credentials['reality_pub'], 'actual-public')

    def test_upload_failure_stops_deploy(self):
        rc, calls = self.run_main(deploy_nodes, '> /root/.xui-skill/_deploy.sh')
        self.assertEqual(rc, 23)
        self.assertFalse(any('bash /root/.xui-skill/_deploy.sh' in c for c in calls))

    def test_merge_failure_propagates(self):
        rc, _ = self.run_main(merge_subscription, 'bash /root/.xui-skill/_merge.sh')
        self.assertEqual(rc, 23)

    def test_merge_upload_failure_stops_execution(self):
        rc, calls = self.run_main(merge_subscription, '> /root/.xui-skill/_merge.sh')
        self.assertEqual(rc, 23)
        self.assertFalse(any('bash /root/.xui-skill/_merge.sh' in c for c in calls))


@unittest.skipUnless(BASH, 'bash required')
class RemoteMergeTests(unittest.TestCase):
    def simulate(self, response, flow='xtls-rprx-vision', missing=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).as_posix()
            Path(folder, 'node-credentials.json').write_text(json.dumps(dict(
                reality_uuid='uuid', hy2_auth='auth', tuic_password='password')))
            inbounds = [dict(id=1, protocol='vless', port=443),
                        dict(id=2, protocol='hysteria', port=443),
                        dict(id=3, protocol='tuic', port=8443),
                        dict(id=4, protocol='vless', port=9999)]
            if missing:
                inbounds.pop(2)
            listing = shlex.quote(json.dumps(dict(success=True, obj=inbounds)))
            readback = shlex.quote(json.dumps(dict(success=True, obj=dict(flow=flow))))
            prelude = ssh_run.env_exports(CFG) + '\n' + f'''
python3() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}
cp() {{ :; }}
systemctl() {{ echo systemctl >> {shlex.quote(root + '/actions')}; }}
sleep() {{ :; }}
curl() {{
  printf '%s\\n' "$*" >> {shlex.quote(root + '/actions')}
  case "$*" in
    */inbounds/list*) printf '%s' {listing} ;;
    */clients/add*) printf '%s' {shlex.quote(response)} ;;
    */clients/get/*) printf '%s' {readback} ;;
    */inbounds/allLinks*) printf '%s' '{{"success":true,"obj":[]}}' ;;
    *-o*) printf '%s' 'dmxlc3M6Ly90ZXN0' > {shlex.quote(root + '/sub.txt')} ;;
    *) printf '%s' '{{"success":true}}' ;;
  esac
}}
'''
            script = merge_subscription.REMOTE_SCRIPT.replace('/root/.xui-skill', root)
            result = subprocess.run([BASH, '-c', prelude + script], capture_output=True,
                                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=30)
            actions = Path(folder, 'actions').read_text()
            payload = Path(folder, 'merge.json')
            body = json.loads(payload.read_text()) if payload.exists() else None
            return result, actions, body

    def test_failed_or_invalid_add_does_not_restart_or_delete(self):
        for response in ('{"success":false,"msg":"duplicate"}', '<html>bad gateway</html>'):
            with self.subTest(response=response):
                result, actions, _ = self.simulate(response)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('/clients/del/', actions)
                self.assertNotIn('systemctl', actions)
                self.assertNotIn('MERGE DONE', result.stdout.decode('utf-8'))

    def test_missing_flow_stops_before_restart(self):
        result, actions, _ = self.simulate('{"success": true}', flow='')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('systemctl', actions)

    def test_missing_target_stops_before_add(self):
        result, actions, _ = self.simulate('{"success": true}', missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('/clients/add', actions)

    def test_success_accepts_json_whitespace_preserves_clients_and_limits_targets(self):
        result, actions, body = self.simulate('{"success": true}')
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        self.assertNotIn('/clients/del/', actions)
        self.assertEqual(body['inboundIds'], [1, 2, 3])
        self.assertEqual(body['client']['flow'], 'xtls-rprx-vision')

    def test_all_shell_scripts_parse(self):
        for script in (deploy_nodes.REMOTE_SCRIPT, merge_subscription.REMOTE_SCRIPT):
            result = subprocess.run([BASH, '-n'], input=script.encode(), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
