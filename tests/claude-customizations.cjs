'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { after, test } = require('node:test');
const { stripVTControlCharacters } = require('node:util');

// Git exports repository overrides while running commit hooks. Isolate this
// test process before calling the in-process gitBranch helper as well as any
// fixture subprocess: otherwise git -C can still target the caller's checkout.
for (const key of Object.keys(process.env)) {
  if (key.startsWith('GIT_')) delete process.env[key];
}
// claude-as hands its status line these; a caller's session must not let a
// fixture run record into the caller's own usage file or token cache.
for (const key of Object.keys(process.env)) {
  if (key.startsWith('CODING_AGENT_KIT_') || ['CLAUDE_CODE_OAUTH_TOKEN', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'].includes(key)) {
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
const { formatStatus, gitBranch, recordUsage, sessionTokens } = require(path.join(scripts, 'statusline.cjs'));
const { notification } = require(path.join(scripts, 'notify.cjs'));
const { bypassReason } = require(path.join(scripts, 'check-git-hooks.cjs'));
const settings = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/claude-settings.json'), 'utf8'));

function run(script, input, env = {}) {
  return spawnSync(process.execPath, [path.join(scripts, script)], {
    input: typeof input === 'string' ? input : JSON.stringify(input),
    env: { ...process.env, ...env },
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

test('status line shows the session tokens as Codex writes a count', () => {
  const spent = tokens => formatStatus({}, '', false, tokens);
  for (const [total, shown] of [
    [1, '1'], [950, '950'], [999.6, '1K'], [1234, '1.23K'], [9996, '10K'], [75240, '75.2K'],
    [148230, '148K'], [999499, '999K'], [999500, '1M'], [13712345, '13.7M'], [2.5e9, '2.5B'], [1.2e12, '1.2T'],
  ]) {
    assert.equal(spent({ total, partial: false }), `Claude | ctx ? | ${shown} used | ~$?`);
  }
  assert.equal(spent({ total: 1530, partial: true }), 'Claude | ctx ? | 1.53K+ used | ~$?');
  for (const tokens of [null, undefined, {}, { total: 0 }, { total: -1 }, { total: Number.NaN },
    { total: Number.POSITIVE_INFINITY }, { total: '1530' }]) {
    assert.equal(spent(tokens), 'Claude | ctx ? | ~$?');
  }
});

test('status line shows the native 5-hour and weekly limits left, including zero and the full limit', () => {
  const window = used => ({ used_percentage: used, resets_at: 1767873600 });
  for (const [used, left] of [[0, 100], [41.6, 58], [99.6, 0], [100, 0]]) {
    assert.equal(formatStatus({ rate_limits: { seven_day: window(used) } }), `Claude | ctx ? | ~$? | weekly ${left}% left`);
    assert.equal(formatStatus({ rate_limits: { five_hour: window(used) } }), `Claude | ctx ? | ~$? | 5h ${left}% left`);
  }
  const result = run('statusline.cjs', {
    model: { display_name: 'Opus' }, effort: { level: 'xhigh' },
    context_window: { used_percentage: 37.6 }, cost: { total_cost_usd: 1.234 },
    rate_limits: { five_hour: window(12.4), seven_day: window(41.6) },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, 'Opus | xhigh | ctx 38% | ~$1.23 | 5h 88% left | weekly 58% left\n');
});

test('status line omits absent and invalid 5-hour and weekly windows', () => {
  const window = { used_percentage: 41, resets_at: 1767873600 };
  for (const invalid of [undefined, null, {},
    ...[-1, 101, Number.NaN, Number.POSITIVE_INFINITY, '41', null].map(used => ({ ...window, used_percentage: used })),
    ...[undefined, 0, -1, 17.5, 253402300800, '1767873600'].map(resets => ({ ...window, resets_at: resets }))]) {
    assert.equal(formatStatus({ rate_limits: { five_hour: invalid, seven_day: invalid } }), 'Claude | ctx ? | ~$?');
    assert.equal(formatStatus({ rate_limits: { five_hour: window, seven_day: invalid } }),
      'Claude | ctx ? | ~$? | 5h 59% left');
    assert.equal(formatStatus({ rate_limits: { five_hour: invalid, seven_day: window } }),
      'Claude | ctx ? | ~$? | weekly 59% left');
  }
});

test('status line ANSI colors preserve every plain field and keep separators plain', () => {
  const input = {
    model: { display_name: 'Opus' }, effort: { level: 'xhigh' },
    workspace: { current_dir: '/work/project' },
    context_window: { used_percentage: 38 }, cost: { total_cost_usd: 1.23 },
    rate_limits: { five_hour: { used_percentage: 92, resets_at: 1767873600 },
      seven_day: { used_percentage: 75, resets_at: 1767873600 } },
  };
  const tokens = { total: 13712345, partial: false };
  const styled = formatStatus(input, 'main', true, tokens);
  assert.equal(styled, '\x1b[36mOpus\x1b[0m | \x1b[35mxhigh\x1b[0m | \x1b[34mproject (main)\x1b[0m'
    + ' | \x1b[32mctx 38%\x1b[0m | 13.7M used | \x1b[33m~$1.23\x1b[0m | \x1b[31m5h 8% left\x1b[0m'
    + ' | \x1b[33mweekly 25% left\x1b[0m');
  assert.equal(stripVTControlCharacters(styled), formatStatus(input, 'main', false, tokens));
  assert.equal(formatStatus(input, 'main', false), formatStatus(input, 'main'));
});

test('status line usage colors follow exact thresholds and preserve zero and unknown values', () => {
  for (const [used, color] of [[0, 32], [69.9, 32], [70, 33], [89.9, 33], [90, 31], [100, 31]]) {
    const window = { used_percentage: used, resets_at: 1767873600 };
    const input = { context_window: { used_percentage: used }, rate_limits: { five_hour: window, seven_day: window } };
    const styled = formatStatus(input, '', true);
    assert.ok(styled.includes(`\x1b[${color}mctx ${Math.round(used)}%\x1b[0m`));
    for (const label of ['5h', 'weekly']) {
      assert.ok(styled.includes(`\x1b[${color}m${label} ${100 - Math.round(used)}% left\x1b[0m`));
    }
    assert.equal(stripVTControlCharacters(styled), formatStatus(input));
  }
  for (const used of [undefined, null, -1, Number.NaN, Number.POSITIVE_INFINITY, '0']) {
    const input = { context_window: { used_percentage: used } };
    const styled = formatStatus(input, '', true);
    assert.ok(styled.includes(' | ctx ? | '));
    assert.equal(stripVTControlCharacters(styled), formatStatus(input));
  }
});

test('status line main honors color overrides with captured stdout', () => {
  const input = { context_window: { used_percentage: 0 }, cost: { total_cost_usd: 0 } };
  for (const [mode, noColor, term, styled] of [
    ['', '', 'xterm', false], ['auto', '', 'xterm', false], ['invalid', '', 'xterm', false],
    ['always', '', 'xterm', true], ['never', '', 'xterm', false],
    ['always', '1', 'xterm', false], ['always', '0', 'xterm', false], ['always', '', 'dumb', false],
  ]) {
    const result = run('statusline.cjs', input, {
      CODING_AGENT_KIT_COLOR: mode, NO_COLOR: noColor, TERM: term,
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, `${formatStatus(input, '', styled)}\n`);
  }
  assert.equal(run('statusline.cjs', input).stdout, `${formatStatus(input)}\n`);
});

test('status line auto colors use stdout TTY state and still honor opt-outs', () => {
  for (const [mode, noColor, term, styled] of [
    ['auto', '', 'xterm', true], ['invalid', '', 'xterm', true], ['never', '', 'xterm', false],
    ['auto', '1', 'xterm', false], ['auto', '', 'dumb', false],
  ]) {
    const result = spawnSync(process.execPath, ['-e',
      'process.stdout.isTTY = true; require(process.argv[1]).main()', path.join(scripts, 'statusline.cjs')], {
      input: '{}', encoding: 'utf8', timeout: 5000,
      env: { ...process.env, CODING_AGENT_KIT_COLOR: mode, NO_COLOR: noColor, TERM: term },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, `${formatStatus({}, '', styled)}\n`);
  }
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
    // Each command names its install path under the home directory (the
    // fixture, as README.md describes, uses ~/.claude/coding-agent-kit);
    // install the helper it names there.
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

// A transcript line with one response's usage, as Claude Code writes it.
const reply = (id, input, written, read, output, type = 'assistant') => `${JSON.stringify({
  type, isSidechain: false, message: { id, role: 'assistant', content: [{ type: 'text', text: 'ok' }],
    usage: { input_tokens: input, cache_creation_input_tokens: written, cache_read_input_tokens: read, output_tokens: output } },
})}\n`;

function sessionFixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-tokens-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const project = path.join(root, 'projects', 'p');
  const transcript = path.join(project, 'session.jsonl');
  const subagents = path.join(project, 'session', 'subagents');
  fs.mkdirSync(path.join(subagents, 'workflows', 'wf_1'), { recursive: true });
  const folder = path.join(root, 'cache', 'coding-agent-kit', 'statusline');
  const count = (options = {}) => sessionTokens({ transcript_path: transcript }, { folder, ...options });
  return { root, transcript, subagents, folder, count };
}

test('status line adds up the tokens of a session and its subagents as Codex counts a thread', posixOnly, (t) => {
  const { transcript, subagents, folder, count } = sessionFixture(t);
  fs.writeFileSync(transcript, [
    '{"type":"user","message":{"role":"user","content":"what is the usage?"}}\n',
    // One response written as two lines that repeat its usage; cache reads never count.
    reply('msg_1', 10, 1000, 50000, 200), reply('msg_1', 10, 1000, 50000, 200),
    reply('msg_2', 5, 2000, 90000, 300),
    '{"type":"assistant","message":{"usage":\n',
    reply('msg_3', 7, 7, 7, 7, 'user'),
    `${JSON.stringify({ type: 'assistant', message: { id: 'msg_4',
      usage: { input_tokens: '9', cache_creation_input_tokens: 1.5, output_tokens: -4 } } })}\n`,
  ].join(''));
  fs.writeFileSync(path.join(subagents, 'agent-a.jsonl'), reply('msg_5', 1, 500, 0, 40));
  fs.writeFileSync(path.join(subagents, 'workflows', 'wf_1', 'agent-b.jsonl'), reply('msg_6', 2, 700, 10, 60));
  // Not transcripts: metadata, a link, and a file deeper than a workflow's agents.
  fs.writeFileSync(path.join(subagents, 'agent-a.meta.json'), reply('msg_7', 9, 9, 9, 9));
  fs.symlinkSync(path.join(subagents, 'agent-a.jsonl'), path.join(subagents, 'agent-link.jsonl'));
  fs.mkdirSync(path.join(subagents, 'a', 'b', 'c'), { recursive: true });
  fs.writeFileSync(path.join(subagents, 'a', 'b', 'c', 'agent-deep.jsonl'), reply('msg_8', 9, 9, 9, 9));
  const total = (10 + 1000 + 200) + (5 + 2000 + 300) + (1 + 500 + 40) + (2 + 700 + 60);
  assert.deepEqual(count(), { total, partial: false });
  assert.equal(fs.statSync(folder).mode & 0o777, 0o700);
  const names = fs.readdirSync(folder);
  assert.equal(names.length, 1, `a temporary file was left: ${names}`);
  assert.match(names[0], /^[0-9a-f]{32}\.json$/);
  assert.equal(fs.statSync(path.join(folder, names[0])).mode & 0o777, 0o600);
  assert.ok(!fs.readFileSync(path.join(folder, names[0]), 'utf8').includes('what is the usage'));
  assert.deepEqual(count(), { total, partial: false });
  assert.equal(formatStatus({}, '', false, count()), 'Claude | ctx ? | 4.82K used | ~$?');
});

test('status line token totals follow a growing, rewritten or replaced transcript', posixOnly, (t) => {
  const { transcript, subagents, folder, count } = sessionFixture(t);
  fs.writeFileSync(transcript, reply('msg_1', 1, 100, 5, 10));
  assert.equal(count().total, 111);
  // The rest of one response, read in a later run, replaces its usage instead of adding to it.
  fs.appendFileSync(transcript, reply('msg_1', 1, 100, 5, 10));
  assert.equal(count().total, 111);
  // A line still being written counts once it is complete.
  const next = reply('msg_2', 2, 200, 5, 20);
  fs.appendFileSync(transcript, next.slice(0, 30));
  assert.deepEqual(count(), { total: 111, partial: false });
  fs.appendFileSync(transcript, next.slice(30));
  assert.equal(count().total, 333);
  assert.equal(sessionTokens({ transcript_path: transcript }, { folder: `${folder}-fresh` }).total, 333);
  // A shorter transcript, or another file in its place, is counted again from the start.
  fs.writeFileSync(transcript, reply('msg_9', 3, 30, 5, 3));
  assert.equal(count().total, 36);
  fs.writeFileSync(`${transcript}.new`, reply('msg_8', 4, 40, 5, 4) + reply('msg_9', 3, 30, 5, 3));
  fs.renameSync(`${transcript}.new`, transcript);
  assert.equal(count().total, 84);
  // So is one rewritten in place on the same inode that did not get shorter.
  const inode = fs.statSync(transcript).ino;
  fs.writeFileSync(transcript, `{"type":"user","note":"rewritten"}\n${reply('msg_7', 5, 50, 5, 5)}`
    + reply('msg_8', 4, 40, 5, 4) + reply('msg_9', 3, 30, 5, 3));
  assert.equal(fs.statSync(transcript).ino, inode);
  assert.equal(count().total, 144);
  // A subagent transcript that is gone keeps the tokens it recorded.
  fs.writeFileSync(path.join(subagents, 'agent-z.jsonl'), reply('msg_z', 1, 10, 5, 1));
  assert.equal(count().total, 156);
  fs.rmSync(path.join(subagents, 'agent-z.jsonl'));
  assert.equal(count().total, 156);
});

test('status line keeps counting when one transcript cannot be read or there are too many', posixOnly, (t) => {
  const { transcript, subagents, count } = sessionFixture(t);
  fs.writeFileSync(transcript, reply('msg_1', 1, 100, 5, 10));
  const locked = path.join(subagents, 'agent-locked.jsonl');
  fs.writeFileSync(locked, reply('msg_2', 2, 200, 5, 20));
  assert.deepEqual(count(), { total: 333, partial: false });
  // An unreadable transcript keeps what it counted before, and the total is marked as a floor.
  fs.appendFileSync(locked, reply('msg_3', 3, 300, 5, 30));
  fs.chmodSync(locked, 0);
  if (process.getuid() !== 0) assert.deepEqual(count(), { total: 333, partial: true });
  fs.chmodSync(locked, 0o600);
  assert.deepEqual(count(), { total: 666, partial: false });
  // Past 1000 subagent transcripts, in name order, the rest are left out and the total is a floor.
  const many = path.join(subagents, 'many');
  fs.mkdirSync(many);
  for (let i = 0; i < 1000; i++) {
    fs.writeFileSync(path.join(many, `agent-${String(i).padStart(4, '0')}.jsonl`), reply(`m${i}`, 0, 1, 0, 0));
  }
  assert.deepEqual(count(), { total: 666 + 999, partial: true });
  // One fewer fits; the removed one keeps its token.
  fs.rmSync(path.join(many, 'agent-0000.jsonl'));
  assert.deepEqual(count(), { total: 666 + 1000, partial: false });
});

test('status line catches up on a long transcript one budget at a time', posixOnly, (t) => {
  const { transcript, count } = sessionFixture(t);
  const lines = Array.from({ length: 20 }, (_, i) => reply(`msg_${i}`, 1, 10, 100, 1));
  fs.writeFileSync(transcript, lines.join(''));
  const budget = lines[0].length * 3 + 5;
  const seen = [];
  for (let result = { partial: true }; result.partial && seen.length < 50;) seen.push(result = count({ budget }));
  assert.deepEqual(seen.at(-1), { total: 20 * 12, partial: false });
  assert.equal(seen.length, 7);
  assert.ok(seen.slice(0, -1).every((result, i) => result.partial && result.total === (i + 1) * 36));
  assert.equal(formatStatus({}, '', false, seen[0]), 'Claude | ctx ? | 36+ used | ~$?');
  // A line longer than a whole budget is skipped; the lines after it count.
  fs.appendFileSync(transcript, `{"type":"user","message":{"content":"${'x'.repeat(budget * 3)}"}}\n`);
  fs.appendFileSync(transcript, reply('msg_last', 1, 10, 100, 1));
  let result = { partial: true };
  for (let runs = 0; result.partial && runs < 50; runs++) result = count({ budget });
  assert.deepEqual(result, { total: 21 * 12, partial: false });
});

test('status line counts no tokens without a usable transcript or a private cache folder', posixOnly, (t) => {
  const { root, transcript, folder, count } = sessionFixture(t);
  fs.writeFileSync(transcript, reply('msg_1', 1, 100, 5, 10));
  fs.writeFileSync(`${transcript}.txt`, reply('msg_1', 1, 100, 5, 10));
  fs.symlinkSync(transcript, path.join(root, 'link.jsonl'));
  assert.equal(sessionTokens(null, { folder }), null);
  for (const transcriptPath of [undefined, 42, 'projects/p/session.jsonl', path.join(root, 'missing.jsonl'),
    `${transcript}.txt`, path.join(root, 'link.jsonl'), path.join(root, 'projects')]) {
    assert.equal(sessionTokens({ transcript_path: transcriptPath }, { folder }), null, String(transcriptPath));
  }
  assert.ok(!fs.existsSync(folder), 'a cache folder was made without a transcript');
  // Only an owned 0700 folder, never a link to one.
  fs.mkdirSync(folder, { recursive: true });
  fs.chmodSync(folder, 0o755);
  assert.equal(count(), null);
  assert.deepEqual(fs.readdirSync(folder), []);
  fs.chmodSync(folder, 0o700);
  fs.symlinkSync(folder, `${folder}-link`);
  assert.equal(count({ folder: `${folder}-link` }), null);
  assert.deepEqual(fs.readdirSync(folder), []);
  // A cache that is unreadable, names another transcript or holds bad records is counted again.
  assert.equal(count().total, 111);
  const [name] = fs.readdirSync(folder);
  const cache = path.join(folder, name);
  const good = fs.readFileSync(cache, 'utf8');
  const record = JSON.parse(good).files.main;
  for (const bad of ['{not json', JSON.stringify({ transcript: '/other.jsonl', files: { main: record } }),
    ...[{ sum: [1e6, 0, 0], offset: 1e9 }, { sum: [-1, 0, 0] }, { offset: -1 }, { ino: 7 }, { skip: 'no' },
      { last: [0, 0] }, { head: 'zz' }, { head: 'abc' }, { head: '00'.repeat(8), offset: 0 }]
      .map(change => JSON.stringify({ transcript, files: { main: { ...record, ...change, sum: change.sum ?? [1e6, 0, 0] } } }))]) {
    fs.writeFileSync(cache, bad);
    assert.equal(count().total, 111, bad);
  }
});

test('status line drops token caches no session has changed for 30 days', posixOnly, (t) => {
  const { transcript, folder, count } = sessionFixture(t);
  fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
  fs.chmodSync(folder, 0o700);
  const now = Date.now();
  const stale = path.join(folder, `${'a'.repeat(32)}.json`);
  const recent = path.join(folder, `${'b'.repeat(32)}.json`);
  const other = path.join(folder, 'notes.json');
  const old = (now - 31 * 24 * 3600 * 1000) / 1000;
  for (const file of [stale, recent, other]) fs.writeFileSync(file, '{}');
  for (const file of [stale, other]) fs.utimesSync(file, old, old);
  fs.writeFileSync(transcript, reply('msg_1', 1, 100, 5, 10));
  assert.equal(count({ now }).total, 111);
  assert.ok(!fs.existsSync(stale));
  assert.ok(fs.existsSync(recent));
  assert.ok(fs.existsSync(other));
});

test('status line main shows the session tokens through the XDG cache folder', posixOnly, (t) => {
  const { root, transcript } = sessionFixture(t);
  fs.writeFileSync(transcript, reply('msg_1', 10, 1000, 50000, 200));
  const input = { context_window: { used_percentage: 12 }, transcript_path: transcript };
  const home = path.join(root, 'home');
  fs.mkdirSync(home);
  const xdg = path.join(root, 'xdg-cache');
  let result = run('statusline.cjs', input, { XDG_CACHE_HOME: xdg, HOME: home });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, 'Claude | ctx 12% | 1.21K used | ~$?\n');
  assert.equal(fs.readdirSync(path.join(xdg, 'coding-agent-kit', 'statusline')).length, 1);
  // A relative XDG_CACHE_HOME is ignored, as the XDG specification asks.
  result = run('statusline.cjs', input, { XDG_CACHE_HOME: 'relative', HOME: home });
  assert.equal(result.stdout, 'Claude | ctx 12% | 1.21K used | ~$?\n');
  assert.equal(fs.readdirSync(path.join(home, '.cache', 'coding-agent-kit', 'statusline')).length, 1);
  // A cache it cannot use leaves the rest of the footer as it was.
  fs.chmodSync(path.join(xdg, 'coding-agent-kit', 'statusline'), 0o755);
  result = run('statusline.cjs', input, { XDG_CACHE_HOME: xdg, HOME: home });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, 'Claude | ctx 12% | ~$?\n');
});

test('status line never counts session tokens on Windows', (t) => {
  const { transcript, folder } = sessionFixture(t);
  fs.writeFileSync(transcript, reply('msg_1', 1, 100, 5, 10));
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'win32' });
  try {
    assert.equal(sessionTokens({ transcript_path: transcript }, { folder }), null);
  } finally {
    Object.defineProperty(process, 'platform', platform);
  }
  assert.ok(!fs.existsSync(folder));
});
