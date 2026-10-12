"""Real launchers with fabricated login subprocesses and isolated controlling TTYs."""

import json
import importlib.util
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import time
import unittest
from io import StringIO

from test_codex_as import CodexAs, FAKE, ROOT, SHELLS

CLAUDE_TOKEN = 'sk-ant-oat01-' + 'fabricated-sensitive-claude-' * 4


class Enrollment(unittest.TestCase):
    _base_setup = CodexAs.setUp
    snapshot = CodexAs.snapshot
    write_credential = CodexAs.write_credential
    run_as = CodexAs.run_as

    def setUp(self):
        self._base_setup()
        self.seed = self.base / "login-seed.json"
        self.seed.write_bytes((self.secret / "codex-B.json").read_bytes())
        fake = self.bin / "codex"
        fake.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
if sys.argv[1:] == ['-c', 'cli_auth_credentials_store="file"', 'login', '--device-auth']:
    dest = pathlib.Path(os.environ['CODEX_HOME'])
    assert dest != pathlib.Path(os.environ['HOME']) / '.codex'
    assert not (dest / 'auth.json').exists()
    assert all(os.isatty(n) for n in (0, 1, 2))
    print('FAKE device login', flush=True)
    if os.environ.get('LOGIN_FAIL'):
        sys.exit(1)
    (dest / 'auth.json').write_bytes(pathlib.Path(os.environ['FAKE_LOGIN_SEED']).read_bytes())
    (dest / 'auth.json').chmod(0o600)
    sys.exit(0)
''' + FAKE)
        self.env['FAKE_LOGIN_SEED'] = str(self.seed)
        claude_source = ROOT / 'common/claude/.local'
        shutil.copy2(claude_source / 'bin/claude-as', self.command_bin / 'claude-as')
        if (claude_source / 'lib/claude-as').exists():
            shutil.copytree(claude_source / 'lib/claude-as', self.home / '.local/lib/claude-as')
        claude = self.bin / 'claude'
        claude.write_text(f'#!{sys.executable}\n' + r'''
import json, os, pathlib, sys, time, termios
if sys.argv[1:] == ['--version']:
    print(os.environ.get('CLAUDE_FAKE_VERSION', '2.1.296 (Claude Code)'))
    sys.exit(0)
if sys.argv[1:] == ['setup-token']:
    dest = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
    assert dest != pathlib.Path(os.environ['HOME']) / '.claude'
    assert os.isatty(0)
    pathlib.Path(os.environ['FAKE_LOGIN_HOME']).write_text(str(dest))
    print('\x1b]8;id=example;https://claude.com/cai/oauth/authorize?state=fixture\x07Sign in\x1b]8;;\x07', flush=True)
    token = os.environ['CLAUDE_FAKE_TOKEN']
    variant = os.environ.get('CLAUDE_VARIANT', 'normal')
    frame = '\x1b[33mYour OAuth token (valid for 1 year):\x1b[0m\n\n' + token + '\n\nStore this token securely. You won\'t be able to see it again.\n'
    if variant == 'truncated':
        frame = frame.split('Store this token securely.')[0]
    if variant == 'different':
        frame += frame.replace(token, token + 'different')
    if variant == 'wrapped':
        frame = frame.replace(token, token[:35] + '\n' + token[35:])
    for i in range(0, len(frame), 7):
        sys.stdout.write(frame[i:i+7]); sys.stdout.flush()
    if variant == 'normal':
        print(frame, flush=True) # Repeated renderer frame must deduplicate.
    if os.environ.get('CLAUDE_DAMAGE_TTY'):
        changed = termios.tcgetattr(0)
        changed[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(0, termios.TCSANOW, changed)
    sys.exit(1 if variant == 'failure' else 0)
fd = os.open('/dev/tty', os.O_RDWR)
flags = termios.tcgetattr(fd)[3]; os.close(fd)
record = {'args': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),
          'selected': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == os.environ['CLAUDE_FAKE_TOKEN'],
          'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'), 'browser': os.environ.get('BROWSER'),
          'tty_flags': [bool(flags & termios.ECHO), bool(flags & termios.ICANON)]}
pathlib.Path(os.environ['FAKE_RECORD']).write_text(json.dumps(record))
print('stdout sentinel'); print('stderr sentinel', file=sys.stderr)
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
''')
        claude.chmod(0o700)
        self.env.update(CLAUDE_FAKE_TOKEN=CLAUDE_TOKEN, FAKE_LOGIN_HOME=str(self.base / 'login-home'))
        for tool in ('codex', 'claude'):
            package = ROOT / f'common/{tool}'
            for subdir in ('.oh-my-zsh/custom/completions', '.local/share/bash-completion/completions'):
                if (package / subdir).exists():
                    shutil.copytree(package / subdir, self.home / subdir, dirs_exist_ok=True)
        for name in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CONFIG_DIR',
                     'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
            self.env.pop(name, None)

    def terminal(self, args, answers, env=None, tool='codex-as', input='original stdin\n', tty_output=False):
        master, slave = pty.openpty()
        stdout, stderr = self.base / 'stdout', self.base / 'stderr'
        incoming = self.base / 'stdin'
        incoming.write_text(input)
        # Child gets a controlling terminal, but original command I/O stays on files.
        command = r'''
import os, sys
os.setsid()
fd = os.open(sys.argv[1], os.O_RDWR)
os.close(fd)
os.execv(sys.argv[2], sys.argv[2:])
'''
        with incoming.open('rb') as inf, stdout.open('wb') as outf, stderr.open('wb') as errf:
            process = subprocess.Popen([sys.executable, '-c', command, os.ttyname(slave),
                                        str(self.command_bin / tool), *args], cwd=self.cwd,
                                       env=self.env | (env or {}), stdin=inf,
                                       stdout=slave if tty_output else outf, stderr=slave if tty_output else errf)
            output = bytearray()
            transcript = bytearray()
            remaining = list(answers)
            deadline = time.monotonic() + 12
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            chunk = os.read(master, 4096)
                        except OSError:
                            break
                        output.extend(chunk)
                        transcript.extend(chunk)
                        if remaining and remaining[0][0].encode() in output:
                            _, answer = remaining.pop(0)
                            os.write(master, answer.encode() + b'\n')
                            output.clear()
                code = process.wait(timeout=2)
                while select.select([master], [], [], 0)[0]:
                    try:
                        chunk = os.read(master, 4096)
                        transcript.extend(chunk)
                    except OSError:
                        break
                return code, stdout.read_text(), stderr.read_text(), transcript.decode(errors='replace')
            finally:
                os.close(master)
                os.close(slave)
                if process.poll() is None:
                    process.kill()  # Only the isolated test launcher, never a real client.
                    process.wait()

    def test_prompt_colors_respect_preferences_and_preserve_client_io(self):
        # Removing destination-aware color or coloring native client streams
        # would break terminal readability, NO_COLOR, or machine-readable I/O.
        cases = [
            ({}, True),
            ({'DOTFILES_COLOR': 'always'}, True),
            ({'DOTFILES_COLOR': 'never'}, False),
            ({'NO_COLOR': '1'}, False),
            ({'NO_COLOR': '1', 'DOTFILES_COLOR': 'always'}, False),
            ({'TERM': 'dumb'}, False),
            ({'TERM': 'dumb', 'DOTFILES_COLOR': 'always'}, False),
        ]
        baseline = {'TERM': 'xterm-256color', 'NO_COLOR': '', 'DOTFILES_COLOR': 'auto'}
        for tool in ('codex-as', 'claude-as'):
            for preferences, colored in cases:
                with self.subTest(tool=tool, preferences=preferences):
                    env = baseline | preferences
                    code, out, err, tty = self.terminal(['color-account'], [('[y/N]', 'n')], env, tool=tool)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(out, '')
                    self.assertEqual('\x1b[' in tty, colored)
                    forced = env['DOTFILES_COLOR'] == 'always' and not env['NO_COLOR'] and env['TERM'] != 'dumb'
                    self.assertEqual('\x1b[' in err, forced)
                    self.assertFalse((self.secret / ('codex-color-account.json' if tool == 'codex-as'
                                                    else 'claude-color-account')).exists())
            code, out, err, tty = self.terminal(['color-account'], [('[y/N]', 'y')],
                                               baseline | {'FAKE_EXIT': '17'}, tool=tool)
            self.assertEqual(code, 17, err)
            self.assertEqual(out, 'stdout sentinel\n')
            self.assertEqual(err, 'stderr sentinel\n')
            self.assertRegex(tty, r'\x1b\[(?:1;)?33m')
            self.assertRegex(tty, r'\x1b\[(?:1;)?32m')
            self.assertNotIn(CLAUDE_TOKEN, tty + out + err)

    def test_help_and_errors_color_only_their_output_destination(self):
        baseline = {'TERM': 'xterm-256color', 'NO_COLOR': '', 'DOTFILES_COLOR': 'auto'}
        for tool in ('codex-as', 'claude-as'):
            for args, style, expected_code in [(['--help'], '36', 0), (['invalid/name'], '31', 2 if tool == 'codex-as' else 1)]:
                for attached, preferences, colored in [
                    (False, {}, False), (True, {}, True),
                    (False, {'DOTFILES_COLOR': 'always'}, True),
                    (True, {'DOTFILES_COLOR': 'never'}, False),
                    (True, {'NO_COLOR': '1', 'DOTFILES_COLOR': 'always'}, False),
                    (True, {'TERM': 'dumb', 'DOTFILES_COLOR': 'always'}, False),
                ]:
                    with self.subTest(tool=tool, args=args, attached=attached, preferences=preferences):
                        code, out, err, tty = self.terminal(args, [], baseline | preferences,
                                                           tool=tool, tty_output=attached)
                        self.assertEqual(code, expected_code)
                        shown = tty if attached else out + err
                        self.assertEqual('\x1b[' in shown, colored)
                        if colored:
                            self.assertRegex(shown, r'\x1b\[(?:1;)?' + style + 'm')
                            self.assertIn('\x1b[0m', shown)
                        if attached:
                            self.assertEqual(out + err, '')
                        elif args == ['--help']:
                            self.assertEqual(err, '')
                        else:
                            self.assertEqual(out, '')
            # Account-list output feeds completion even when color is forced.
            helper = self.home / '.local/lib' / tool / ('check.py' if tool == 'codex-as' else 'launch.py')
            query = 'accounts' if tool == 'codex-as' else '--list-accounts'
            result = subprocess.run([sys.executable, '-I', '-B', str(helper), query],
                                    env=self.env | baseline | {'DOTFILES_COLOR': 'always'},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('\x1b', result.stdout + result.stderr)

    def test_codex_enroll_continues_original_command_and_stdin(self):
        code, out, err, tty = self.terminal(['work', 'exec', '--json', 'login "two words"'],
                                          [('[y/N]', 'y')], {'FAKE_EXIT': '23'})
        self.assertEqual(code, 23, err)
        self.assertEqual(out, 'stdout sentinel\n')
        self.assertEqual(err, 'stderr sentinel\n')
        self.assertIn('FAKE device login', tty)
        target = self.secret / 'codex-work.json'
        self.assertTrue(target.read_bytes() == self.seed.read_bytes(), 'complete JSON changed')
        self.assertEqual(os.readlink(self.auth), str(target))
        record = json.loads(self.record.read_text())
        self.assertEqual(record['stdin'], 'original stdin\n')
        self.assertEqual(record['args'][-3:], ['exec', '--json', 'login "two words"'])

    def test_missing_decline_and_login_failure_do_not_change_auth(self):
        old = os.readlink(self.auth)
        for answers, env in [([('[y/N]', '')], {}), ([('[y/N]', 'y')], {'LOGIN_FAIL': '1'})]:
            code, out, _, _ = self.terminal(['work'], answers, env)
            self.assertNotEqual(code, 0)
            self.assertEqual(out, '')
            self.assertFalse((self.secret / 'codex-work.json').exists())
            self.assertEqual(os.readlink(self.auth), old)
            self.assertFalse(self.record.exists())

    def test_missing_without_tty_and_help_do_not_login(self):
        for shell in SHELLS:
            result = self.run_as(shell, ['work'])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('terminal', result.stderr.lower())
            result = self.run_as(shell, ['work', '--help'])
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.secret / 'codex-work.json').exists())

    def test_initial_adoption_is_explicit_and_later_replacement_is_not_adopted(self):
        self.auth.unlink()
        self.auth.write_bytes((self.secret / 'codex-A.json').read_bytes())
        self.auth.chmod(0o600)
        (self.codex / 'codex-as.initialized').unlink(missing_ok=True)
        self.codex.chmod(0o775)
        original = self.auth.read_bytes()
        code, _, err, _ = self.terminal(['A', 'exec', 'prompt'], [('[y/N]', 'y')])
        self.assertEqual(code, 0, err)
        self.assertTrue(self.auth.is_symlink())
        self.assertTrue((self.codex / 'codex-as.initialized').is_file())
        self.assertEqual(self.codex.stat().st_mode & 0o777, 0o700)
        backups = list(self.secret.glob('.codex-as-backup-*'))
        self.assertTrue(any(p.read_bytes() == original for p in backups))
        self.auth.unlink()
        self.auth.write_bytes(original)
        self.auth.chmod(0o600)
        code, _, _, _ = self.terminal(['work'], [])
        self.assertNotEqual(code, 0)
        self.assertTrue(self.auth.read_bytes() == original)
        self.assertFalse((self.secret / 'codex-work.json').exists())

    def test_claude_enroll_capture_and_continue_without_token_output(self):
        code, out, err, tty = self.terminal(['work', '-p', 'setup-token "quoted"'],
                                          [('[y/N]', 'y')], {'FAKE_EXIT': '19', 'BROWSER': 'original-browser'},
                                          tool='claude-as')
        self.assertEqual(code, 19, err)
        self.assertEqual(out, 'stdout sentinel\n')
        self.assertEqual(err, 'stderr sentinel\n')
        self.assertNotIn(CLAUDE_TOKEN, tty + out + err)
        self.assertIn('https://claude.com/cai/oauth/authorize', tty)
        target = self.secret / 'claude-work'
        self.assertTrue(target.read_text().strip() == CLAUDE_TOKEN)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        record = json.loads(self.record.read_text())
        self.assertTrue(record['selected'])
        self.assertEqual(record['args'], ['-p', 'setup-token "quoted"'])
        self.assertEqual(record['stdin'], 'original stdin\n')
        self.assertEqual(record['browser'], 'original-browser')
        self.assertIsNone(record['config_dir'])
        self.assertFalse(Path((self.base / 'login-home').read_text()).exists())

    def test_claude_capture_failure_and_manual_fallback_decline(self):
        for variant in ('truncated', 'different', 'failure'):
            code, out, _, tty = self.terminal(['work'], [('[y/N]', 'y'), ('[y/N]', 'n')],
                                             {'CLAUDE_VARIANT': variant}, tool='claude-as')
            self.assertNotEqual(code, 0)
            self.assertEqual(out, '')
            self.assertFalse((self.secret / 'claude-work').exists())
            self.assertNotIn(CLAUDE_TOKEN, tty)

    def test_claude_existing_token_conflicts_and_safety(self):
        target = self.secret / 'claude-work'
        target.write_text(CLAUDE_TOKEN + '\n'); target.chmod(0o600)
        for env in ({'ANTHROPIC_API_KEY': 'fabricated-secret'}, {'CLAUDE_CODE_OAUTH_TOKEN': 'fabricated-secret'}):
            code, _, err, _ = self.terminal(['work'], [], env, tool='claude-as')
            self.assertNotEqual(code, 0)
            self.assertNotIn('fabricated-secret', err)
            self.assertFalse(self.record.exists())
        for args in (['work', 'setup-token'], ['work', '--bare'], ['../work'],
                     ['work', '-p', 'auth', 'logout'], ['work', '-c', 'auth', 'logout'],
                     ['work', '--', 'auth', 'logout'], ['work', '--debug-file', 'file', '-p', 'auth', 'logout'],
                     ['work', '--allowedTools', 'Read', 'Write', '-p', 'auth', 'logout'],
                     ['work', '--allowedTools=Read', 'auth', 'logout'],
                     ['work', '--resume', '-', 'auth', 'logout']):
            self.record.unlink(missing_ok=True)
            code, _, _, _ = self.terminal(args, [], tool='claude-as')
            self.assertNotEqual(code, 0)
            self.assertFalse(self.record.exists())
        hard = self.base / 'hard'; os.link(target, hard)
        code, _, _, _ = self.terminal(['work'], [], tool='claude-as')
        self.assertNotEqual(code, 0)

    def test_claude_wrapped_capture_policy_and_manual_fallback(self):
        code, _, err, tty = self.terminal(['work'], [('[y/N]', 'y')],
                                         {'CLAUDE_VARIANT': 'wrapped'}, tool='claude-as')
        self.assertEqual(code, 0, err)
        self.assertNotIn(CLAUDE_TOKEN, tty)
        (self.secret / 'claude-work').unlink()
        cache = self.home / '.claude/remote-settings.json'
        cache.parent.mkdir(mode=0o700)
        cache.write_text('{"policy": "fixture"}')
        code, _, err, _ = self.terminal(['work'], [], tool='claude-as')
        self.assertNotEqual(code, 0)
        self.assertIn('managed policy', err)
        cache.write_text('{}')
        code, _, err, _ = self.terminal(['work'], [('[y/N]', 'y'), ('[y/N]', 'y'), ('input hidden', CLAUDE_TOKEN)],
                                       {'CLAUDE_FAKE_VERSION': '99.0.0'}, tool='claude-as')
        self.assertEqual(code, 0, err)
        self.assertTrue((self.secret / 'claude-work').read_text().strip() == CLAUDE_TOKEN)

    def test_completion_lists_safe_names_without_opening_secrets_or_cli(self):
        (self.secret / 'claude-good').write_text('not a token')
        (self.secret / 'claude-good').chmod(0o600)
        (self.secret / 'codex-C.json').write_text('not JSON')
        (self.secret / 'codex-C.json').chmod(0o600)
        (self.secret / 'codex-linked.json').symlink_to(self.secret / 'codex-A.json')
        hard = self.secret / 'claude-hard'
        os.link(self.secret / 'claude-good', hard)
        # Both hardlinked names are excluded, even though only metadata is read.
        bash = shutil.which('bash')
        if bash:
            command = 'source "$HOME/.local/share/bash-completion/completions/codex-as"; COMP_WORDS=(codex-as ""); COMP_CWORD=1; _codex_as; printf "%s\\n" "${COMPREPLY[@]}"'
            result = subprocess.run([bash, '--noprofile', '--norc', '-c', command], env=self.env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['A', 'B', 'C'])
        zsh = shutil.which('zsh')
        if zsh:
            command = '_describe() { shift 2; shift; print -rl -- "${(@P)1}"; }; CURRENT=2; words=(codex-as ""); source "$HOME/.oh-my-zsh/custom/completions/_codex-as"'
            result = subprocess.run([zsh, '-f', '-c', command], env=self.env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['A', 'B', 'C'])
        self.assertFalse(self.record.exists(), 'completion launched the CLI')

    def test_capture_never_displays_incomplete_authorization_url(self):
        path = ROOT / 'common/claude/.local/lib/claude-as/launch.py'
        spec = importlib.util.spec_from_file_location('claude_capture', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tty = StringIO()
        capture = module.Capture(tty)
        capture.feed(b'https://claude.com/cai/oauth/authorize?sta')
        self.assertEqual(tty.getvalue(), '')
        capture.feed(b'te=fixture\n\n')
        self.assertIn('?state=fixture', tty.getvalue())

    def test_fzf_preview_never_reads_secret_file(self):
        zsh = shutil.which('zsh')
        if not zsh:
            self.skipTest('zsh not installed')
        preview = ROOT / 'common/zsh/.oh-my-zsh/custom/fzf-tab.zsh'
        command = 'source "$1"; zstyle -s :fzf-tab:complete:codex-as:accounts fzf-preview preview; word=A; realpath="$HOME/.secrets/codex-A.json"; my-fzf-preview.sh() { print "LEAK"; }; eval "$preview"'
        result = subprocess.run([zsh, '-f', '-c', command, 'preview', str(preview)], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'A\n')

    def test_codex_import_preserves_crlf_and_unknown_fields_byte_for_byte(self):
        self.seed.write_bytes(self.seed.read_bytes().replace(b', ', b',\r\n'))
        code, _, err, _ = self.terminal(['work', 'exec', 'prompt'], [('[y/N]', 'y')])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.secret / 'codex-work.json').read_bytes() == self.seed.read_bytes(), 'JSON bytes changed')

    def test_additional_provider_and_endpoint_selectors_are_refused(self):
        target = self.secret / 'claude-work'
        target.write_text(CLAUDE_TOKEN + '\n'); target.chmod(0o600)
        names = ['CLAUDE_CODE_USE_ANTHROPIC_AWS', 'CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD',
                 'CLAUDE_CODE_USE_MANTLE', 'CLAUDE_CODE_USE_GATEWAY', 'CLAUDE_CODE_CUSTOM_OAUTH_URL',
                 'CLAUDE_CODE_API_BASE_URL', 'CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST',
                 'CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR', 'CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR',
                 'ANTHROPIC_UNIX_SOCKET', 'ANTHROPIC_CUSTOM_HEADERS', 'CLAUDE_CODE_OAUTH_CLIENT_ID',
                 'CLAUDE_CODE_HOST_AUTH_ENV_VAR', 'CLAUDE_CODE_HOST_CREDS_FILE',
                 'CLAUDE_SESSION_INGRESS_TOKEN_FILE', 'CLAUDE_BG_AUTH_SNAPSHOT_PATH']
        for name in names:
            with self.subTest(name=name):
                self.record.unlink(missing_ok=True)
                code, _, err, _ = self.terminal(['work'], [], {name: 'fabricated-conflict-secret'}, tool='claude-as')
                self.assertNotEqual(code, 0)
                self.assertIn(name, err)
                self.assertNotIn('fabricated-conflict-secret', err)
                self.assertFalse(self.record.exists())
        config = self.home / '.claude/settings.json'
        config.parent.mkdir(mode=0o700, exist_ok=True)
        for data in ({'forceLoginGatewayUrl': ''}, {'env': {'CLAUDE_CODE_USE_MANTLE': 'fixture'}}):
            config.write_text(json.dumps(data))
            code, _, _, _ = self.terminal(['work'], [], tool='claude-as')
            self.assertNotEqual(code, 0)

    def test_codex_auth_feature_overrides_are_refused_before_login(self):
        for flag in ('--enable', '--disable'):
            for shell in SHELLS:
                result = self.run_as(shell, ['work', flag, 'secret_auth_storage'])
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.secret / 'codex-work.json').exists())
                self.assertIn('auth', result.stderr.lower())

    def test_unknown_source_flags_and_unsupported_services_refuse_before_enrollment(self):
        target = self.secret / 'claude-work'
        target.write_text(CLAUDE_TOKEN + '\n'); target.chmod(0o600)
        for flag in ('--managed-settings', '--project-config-root', '--sdk-url', '--client-data-url',
                     '--remote', '--cloud', '--teleport', '--remote-control', '--rc', '--unknown-new-flag'):
            code, out, err, tty = self.terminal(['work', flag, 'fixture'], [], tool='claude-as')
            self.assertNotEqual(code, 0)
            self.assertEqual(out, '')
            self.assertEqual(tty, '')
            self.assertFalse((self.base / 'login-home').exists())
        for command in ('app', 'cloud-tasks', 'tcp-tunnel', 'execpolicy', 'responses-api-proxy', 'stdio-to-uds'):
            for shell in SHELLS:
                result = self.run_as(shell, ['work', command])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unsupported subcommand', result.stderr)

    def test_claude_optional_and_variadic_values_are_not_commands(self):
        target = self.secret / 'claude-work'
        target.write_text(CLAUDE_TOKEN + '\n'); target.chmod(0o600)
        args = ['work', '--allowedTools', 'Read', 'auth', '--model', 'setup-token', '-p', 'auth is a prompt']
        code, _, err, _ = self.terminal(args, [], tool='claude-as')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.record.read_text())['args'], args[1:])
        code, _, err, _ = self.terminal(['work', '--resume', 'auth', '-p', 'normal prompt'], [], tool='claude-as')
        self.assertEqual(code, 0, err)

    def test_real_stow_installs_underscore_completions_and_ignores_appledouble(self):
        stow = shutil.which('stow')
        if not stow:
            self.skipTest('GNU Stow not installed')
        packages = self.base / 'packages'
        target = self.base / 'stowed home'
        target.mkdir(mode=0o700)
        for tool in ('codex', 'claude'):
            shutil.copytree(ROOT / f'common/{tool}', packages / tool)
        apple = packages / 'codex/.local/bin/._fake-appledouble'
        apple.write_text('fixture')
        result = subprocess.run([stow, f'--dir={packages}', f'--target={target}', '--no-folding', 'codex', 'claude'],
                                cwd=ROOT, env=self.env | {'HOME': str(target)}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for tool in ('codex', 'claude'):
            self.assertTrue((target / f'.oh-my-zsh/custom/completions/_{tool}-as').is_symlink(), 'Stow dropped zsh completion')
        self.assertFalse((target / '.local/bin/._fake-appledouble').exists())

    def test_config_directory_empty_and_literal_tilde_match_native_loading(self):
        target = self.secret / 'claude-work'
        target.write_text(CLAUDE_TOKEN + '\n'); target.chmod(0o600)
        for env, folder in [({'CLAUDE_CONFIG_DIR': ''}, self.home / '.claude'),
                            ({'CLAUDE_CONFIG_DIR': '~/.claude'}, self.cwd / '~/.claude')]:
            folder.mkdir(parents=True, mode=0o700, exist_ok=True)
            settings = folder / 'settings.json'
            settings.write_text('{"forceLoginGatewayUrl": "https://example.invalid"}')
            code, _, _, _ = self.terminal(['work'], [], env, tool='claude-as')
            self.assertNotEqual(code, 0)
            self.assertFalse(self.record.exists())
            settings.unlink()

    def test_native_authorization_tty_settings_do_not_leak_into_final_client(self):
        code, _, err, _ = self.terminal(['work'], [('[y/N]', 'y')],
                                       {'CLAUDE_DAMAGE_TTY': '1'}, tool='claude-as')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.record.read_text())['tty_flags'], [True, True])


del CodexAs  # unittest should collect only this module's enrollment cases.

if __name__ == '__main__':
    unittest.main()
