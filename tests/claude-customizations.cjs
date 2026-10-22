'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { after, test } = require('node:test');

// Git exports repository overrides while running commit hooks. Isolate this
// test process before calling the in-process gitBranch helper as well as any
// fixture subprocess: otherwise git -C can still target the caller's checkout.
for (const key of Object.keys(process.env)) {
  if (key.startsWith('GIT_')) delete process.env[key];
}
// claude-as hands its status line these; a caller's session must not let a
// fixture run record into the caller's own usage file.
for (const key of Object.keys(process.env)) {
  if (key.startsWith('CODING_AGENT_KIT_') || key === 'CLAUDE_CODE_OAUTH_TOKEN' || key === 'XDG_STATE_HOME') {
    delete process.env[key];
  }
}
// Git for Windows rejects Node's device path (\\.\nul) as a config file.
// A regular empty file and directory work consistently on every platform.
const gitEnvironment = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-git-env-'));
after(() => fs.rmSync(gitEnvironment, { recursive: true, force: true }));
process.env.GIT_CONFIG_GLOBAL = path.join(gitEnvironment, 'empty.gitconfig');
fs.writeFileSync(process.env.GIT_CONFIG_GLOBAL, '');
process.env.GIT_CONFIG_NOSYSTEM = '1';
process.env.GIT_TEMPLATE_DIR = path.join(gitEnvironment, 'empty-template');
fs.mkdirSync(process.env.GIT_TEMPLATE_DIR);

const scripts = path.resolve(__dirname, '../helpers');
const { formatStatus, gitBranch, recordUsage } = require(path.join(scripts, 'statusline.cjs'));
const { notification } = require(path.join(scripts, 'notify.cjs'));
const { bypassReason } = require(path.join(scripts, 'check-git-hooks.cjs'));
const settings = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/claude-settings.json'), 'utf8'));

function run(script, input) {
  return spawnSync(process.execPath, [path.join(scripts, script)], {
    input: typeof input === 'string' ? input : JSON.stringify(input),
    encoding: 'utf8',
    timeout: 5000,
  });
}

test('Git fixtures use ordinary empty config and template paths', () => {
  assert.ok(fs.lstatSync(process.env.GIT_CONFIG_GLOBAL).isFile());
  assert.equal(fs.readFileSync(process.env.GIT_CONFIG_GLOBAL, 'utf8'), '');
  assert.ok(fs.lstatSync(process.env.GIT_TEMPLATE_DIR).isDirectory());
  assert.deepEqual(fs.readdirSync(process.env.GIT_TEMPLATE_DIR), []);
});

test('status line handles real values, absent data and zero without inventing usage', () => {
  assert.equal(formatStatus({
    model: { display_name: 'Opus' }, effort: { level: 'xhigh' },
    workspace: { current_dir: '/work/my project' },
    context_window: { used_percentage: 37.6 }, cost: { total_cost_usd: 1.234 },
  }, 'feature/ui'), 'Opus | xhigh | my project (feature/ui) | ctx 38% | ~$1.23');
  assert.equal(formatStatus(null), 'Claude | ctx ? | ~$?');
  assert.equal(formatStatus({ context_window: { used_percentage: 0 }, cost: { total_cost_usd: 0 } }),
    'Claude | ctx 0% | ~$0.00');
  assert.equal(formatStatus({ model: { display_name: '\x1bOpus\n' }, cwd: 'C:\\work\\project' }),
    'Opus | project | ctx ? | ~$?');
  assert.equal(run('statusline.cjs', '{invalid').status, 0);
});

test('status line reads branches, unborn branches and detached HEAD in a temporary repository', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-status-'));
  try {
    const git = (...args) => {
      const result = spawnSync('git', ['-C', temporary, ...args], { encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      return result.stdout.trim();
    };
    assert.equal(gitBranch(temporary), '');
    git('init', '--quiet');
    git('symbolic-ref', 'HEAD', 'refs/heads/status-fixture');
    assert.equal(gitBranch(temporary), 'status-fixture');
    const tree = git('mktree');
    const commit = git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
      'commit-tree', tree, '-m', 'fixture');
    git('update-ref', 'HEAD', commit);
    git('update-ref', '--no-deref', 'HEAD', commit);
    assert.equal(gitBranch(temporary), `detached:${git('rev-parse', '--short', 'HEAD')}`);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test('notifications choose terminal protocols and omit prompt contents', () => {
  const event = { hook_event_name: 'Notification', notification_type: 'permission_prompt',
    cwd: '/work/project', message: 'private command contents' };
  assert.deepEqual(notification(event, { TERM: 'xterm-kitty' }),
    { terminalSequence: '\x1b]99;;Claude Code (project): Approval needed\x1b\\' });
  assert.deepEqual(notification(event, { TERM_PROGRAM: 'ghostty' }),
    { terminalSequence: '\x1b]777;notify;Claude Code (project);Approval needed\x07' });
  for (const terminal of ['WezTerm', 'iTerm.app', 'Windows_Terminal']) {
    assert.deepEqual(notification(event, { TERM_PROGRAM: terminal }),
      { terminalSequence: '\x1b]9;Claude Code (project): Approval needed\x07' });
  }
  const idle = notification({ ...event, notification_type: 'idle_prompt', cwd: 'C:\\work\\project' }, {});
  assert.match(idle.terminalSequence, /project.*Ready for your input/);
  assert.ok(!idle.terminalSequence.includes(event.message));
  assert.equal(notification({ ...event, notification_type: 'auth_success' }), null);
  assert.equal(notification({ ...event, hook_event_name: 'Stop' }), null);
  assert.equal(notification(null), null);
  assert.equal(run('notify.cjs', '{invalid').stdout, '');
});

test('notification input cannot inject additional terminal sequences', () => {
  const output = notification({ hook_event_name: 'Notification', notification_type: 'idle_prompt',
    cwd: '/tmp/bad;\x07\x1b]52;c;payload\x9c\n' }, { TERM_PROGRAM: 'ghostty' });
  assert.equal((output.terminalSequence.match(/\x1b/g) || []).length, 1);
  assert.equal((output.terminalSequence.match(/\x07/g) || []).length, 1);
  assert.equal(output.terminalSequence.split(';').length, 4);
});

test('Git guard preserves common bypass blocks without running submitted commands', () => {
  for (const command of [
    'git commit --no-verify -m test', 'git push --no-verify', 'git merge --no-verify topic',
    'git rebase --no-verify main', 'git cherry-pick --no-verify HEAD', 'git am --no-verify patch',
    'git commit -n', 'git commit -nam test',
    'git -c core.hooksPath=/dev/null commit -m test',
    'git -c "core.hooksPath=" push', "git -c 'core.hooksPath=/tmp/hooks' merge branch",
    'git -C "/work/my project" commit --no-verify',
    '/usr/bin/git commit --no-verify', '"C:\\Program Files\\Git\\cmd\\git.exe" commit -n',
    'git.EXE commit --no-verify', 'git.Exe commit -n',
    '"C:\\Program Files\\Git\\cmd\\git.EXE" commit -n',
    'git status && git commit --no-verify', 'git status; git push --no-verify',
    'sh -c "git commit --no-verify"',
  ]) assert.ok(bypassReason(command), command);

  const blocked = run('check-git-hooks.cjs', { tool_input: { command: 'git commit --no-verify' } });
  assert.equal(blocked.status, 2);
  assert.match(blocked.stderr, /Git hooks must run/);
  assert.equal(blocked.stdout, '');
});

test('Git guard permits ordinary commands and non-bypass -n flags', () => {
  for (const command of [
    'git commit -m test', 'git push -n', 'git merge -n topic',
    'git status', 'printf hello', 'git log -n 5',
    'rg -- --no-verify README.md', 'widget commit --no-verify',
    '# git commit --no-verify\nprintf hello',
  ]) assert.equal(bypassReason(command), '', command);
  const permitted = run('check-git-hooks.cjs', { tool_input: { command: 'git status' } });
  assert.equal(permitted.status, 0);
  assert.equal(permitted.stdout, '');
  assert.equal(permitted.stderr, '');
  for (const invalid of ['{invalid', '{}', 'null']) {
    assert.equal(run('check-git-hooks.cjs', invalid).status, 2);
  }
});

test('portable launchers resolve stowed helpers through a home path containing spaces', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'claude home '));
  try {
    // Each command names its install path under the home directory (dotfiles
    // uses ~/.claude/dotfiles); install the helper it names there.
    const execute = (command, input) => {
      const match = command.match(/^node -e "([^"]+)"$/);
      assert.ok(match, command);
      const installed = match[1].match(/^require\(require\('os'\)\.homedir\(\) \+ '\/([^']+\/)?([^'/]+\.cjs)'\)\.main\(\)$/);
      assert.ok(installed && !(installed[1] || '').split('/').includes('..'), command);
      const target = path.join(temporary, ...(installed[1] || '').split('/'));
      fs.mkdirSync(target, { recursive: true });
      fs.copyFileSync(path.join(scripts, installed[2]), path.join(target, installed[2]));
      return spawnSync(process.execPath, ['-e',
        'require("os").homedir = () => process.env.KIT_HELPER_TEST_HOME; ' + match[1]], {
        env: { ...process.env, KIT_HELPER_TEST_HOME: temporary },
        input: JSON.stringify(input), encoding: 'utf8', timeout: 5000,
      });
    };
    assert.equal(execute(settings.statusLine.command, {}).stdout.trim(), 'Claude | ctx ? | ~$?');
    const notify = settings.hooks.Notification[0].hooks[0];
    const output = execute(notify.command, { hook_event_name: 'Notification', notification_type: 'idle_prompt' });
    assert.equal(output.status, 0, output.stderr);
    assert.ok(JSON.parse(output.stdout).terminalSequence);
    const guard = execute(settings.hooks.PreToolUse[0].hooks[0].command,
      { tool_input: { command: 'git commit --no-verify' } });
    assert.equal(guard.status, 2, guard.stderr);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

const posixOnly = { skip: process.platform === 'win32' && 'claude-as and its usage file are POSIX-only' };
const fabricatedToken = 'sk-ant-oat01-' + 'fabricated-sensitive-statusline-'.repeat(3);
const usageInput = (five, seven) => ({ rate_limits: {
  five_hour: { used_percentage: five, resets_at: 1767276000 },
  seven_day: { used_percentage: seven, resets_at: 1767600000 },
} });

function usageFixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-usage-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const folder = path.join(root, 'accounts');
  fs.mkdirSync(folder, { mode: 0o700 });
  fs.chmodSync(folder, 0o700);
  const file = path.join(folder, 'claude-work.usage.json');
  // As Claude Code runs the status line: under the claude process claude-as became (for
  // these in-process calls the test's parent), and without CLAUDE_CODE_OAUTH_TOKEN, which
  // Claude Code passes to none of the commands it starts.
  const env = {
    CODING_AGENT_KIT_USAGE_FILE: file,
    CODING_AGENT_KIT_TOKEN_ID: crypto.createHash('sha256').update(fabricatedToken).digest('hex').slice(0, 16),
    CODING_AGENT_KIT_CLAUDE_PID: String(process.ppid),
  };
  return { root, folder, file, env };
}

test('status line records the last rate limits for the claude-as account it runs under', posixOnly, (t) => {
  const { folder, file, env } = usageFixture(t);
  assert.equal(recordUsage(usageInput(23.5, 41), env, 1767268800000), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), {
    five_hour: { used_percentage: 23.5, resets_at: 1767276000 },
    seven_day: { used_percentage: 41, resets_at: 1767600000 },
    seen_at: 1767268800,
  });
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.ok(!fs.readFileSync(file, 'utf8').includes('fabricated'), 'the token reached the usage file');
  // Unchanged values are written again only after five minutes; changed ones at once.
  assert.equal(recordUsage(usageInput(23.5, 41), env, 1767268800000 + 299000), false);
  assert.equal(recordUsage(usageInput(23.5, 41), env, 1767268800000 + 301000), true);
  assert.equal(recordUsage(usageInput(24, 41), env, 1767268800000 + 302000), true);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).five_hour.used_percentage, 24);
  assert.deepEqual(fs.readdirSync(folder), ['claude-work.usage.json'], 'a temporary file was left');
  // Any account name claude-as accepts, however long.
  const long = path.join(folder, `claude-${'n'.repeat(120)}.usage.json`);
  assert.equal(recordUsage(usageInput(5, 6), { ...env, CODING_AGENT_KIT_USAGE_FILE: long }, 1767268800000), true);
  // Where a token is visible after all, it must be the account's.
  assert.equal(recordUsage(usageInput(6, 6), { ...env, CLAUDE_CODE_OAUTH_TOKEN: fabricatedToken }, 1767268800000), true);
});

test('status line records nothing outside its launcher, with another token, a bad path or bad numbers', posixOnly, (t) => {
  const { root, folder, file, env } = usageFixture(t);
  const refused = [
    {},
    { ...env, CLAUDE_CODE_OAUTH_TOKEN: fabricatedToken + 'x' },
    { ...env, CODING_AGENT_KIT_TOKEN_ID: undefined },
    { ...env, CODING_AGENT_KIT_CLAUDE_PID: undefined },
    { ...env, CODING_AGENT_KIT_CLAUDE_PID: String(process.pid) },  // not a process this one runs under
    { ...env, CODING_AGENT_KIT_CLAUDE_PID: '1' },
    { ...env, CODING_AGENT_KIT_CLAUDE_PID: `${process.ppid}x` },  // the parent's pid, but not a number
    { ...env, CODING_AGENT_KIT_USAGE_FILE: 'accounts/claude-work.usage.json' },
    { ...env, CODING_AGENT_KIT_USAGE_FILE: path.join(folder, 'codex-work.json') },
    { ...env, CODING_AGENT_KIT_USAGE_FILE: path.join(folder, 'claude-../x.usage.json') },
  ];
  for (const candidate of refused) {
    assert.equal(recordUsage(usageInput(10, 20), candidate, 1767268800000), false, JSON.stringify(Object.keys(candidate)));
  }
  for (const input of [{}, { rate_limits: null }, usageInput(Number.NaN, 101),
    { rate_limits: { five_hour: { used_percentage: 5, resets_at: 17.5 } } },
    { rate_limits: { five_hour: { used_percentage: 5, resets_at: 253402300800 } } },
    { rate_limits: { five_hour: { used_percentage: '5', resets_at: 1767276000 } } }]) {
    assert.equal(recordUsage(input, env, 1767268800000), false, JSON.stringify(input));
  }
  assert.deepEqual(fs.readdirSync(folder), []);
  fs.chmodSync(folder, 0o755);
  assert.equal(recordUsage(usageInput(10, 20), env, 1767268800000), false, 'wrote into a directory others can read');
  fs.chmodSync(folder, 0o700);
  const elsewhere = path.join(root, 'elsewhere');
  fs.mkdirSync(elsewhere, { mode: 0o700 });
  fs.rmdirSync(folder);
  fs.symlinkSync(elsewhere, folder);
  assert.equal(recordUsage(usageInput(10, 20), env, 1767268800000), false, 'followed a linked directory');
  assert.deepEqual(fs.readdirSync(elsewhere), []);
  assert.ok(!fs.existsSync(file) || fs.lstatSync(folder).isSymbolicLink());
});

test('status line records under the claude process claude-as became, never a claude inside it', posixOnly, (t) => {
  const { folder, env } = usageFixture(t);
  const statusline = path.join(scripts, 'statusline.cjs');
  // This test process plays the claude that claude-as became; an intermediate node plays
  // a claude someone started inside that session, which inherits the variables.
  const nested = `require('node:child_process').spawnSync(process.argv[1], process.argv.slice(2), { stdio: 'inherit' })`;
  const cases = [
    ['directly', [process.execPath, statusline], true],
    ['through a shell', ['/bin/sh', '-c', '"$0" "$1"; :', process.execPath, statusline], true],
    ['under a claude inside the session', [process.execPath, '-e', nested, process.execPath, statusline], false],
    ['through a shell under a claude inside it', [process.execPath, '-e', nested, '/bin/sh', '-c', '"$0" "$1"; :',
      process.execPath, statusline], false],
  ];
  for (const [name, command, records] of cases) {
    const file = path.join(folder, `claude-${name.replace(/[^a-z]/g, '')}.usage.json`);
    const result = spawnSync(command[0], command.slice(1), {
      input: JSON.stringify(usageInput(9, 10)), encoding: 'utf8', timeout: 10000,
      env: { ...process.env, ...env, CODING_AGENT_KIT_USAGE_FILE: file, CODING_AGENT_KIT_CLAUDE_PID: String(process.pid) },
    });
    assert.equal(result.status, 0, `${name}: ${result.stderr}`);
    assert.equal(fs.existsSync(file), records, name);
  }
});

test('status line output is unchanged when it records usage', posixOnly, (t) => {
  const { file, env } = usageFixture(t);
  const input = { ...usageInput(7, 8), model: { display_name: 'Opus' } };
  const result = spawnSync(process.execPath, [path.join(scripts, 'statusline.cjs')], {
    input: JSON.stringify(input), encoding: 'utf8', timeout: 5000,
    env: { ...process.env, ...env, CODING_AGENT_KIT_CLAUDE_PID: String(process.pid) },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, `${formatStatus(input)}\n`);
  assert.equal(result.stderr, '');
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).seven_day.used_percentage, 8);
});

test('status line never records usage on Windows', (t) => {
  const { file, env } = usageFixture(t);
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'win32' });
  try {
    assert.equal(recordUsage(usageInput(10, 20), env, 1767268800000), false);
  } finally {
    Object.defineProperty(process, 'platform', platform);
  }
  assert.ok(!fs.existsSync(file));
});
