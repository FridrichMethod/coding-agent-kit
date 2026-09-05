'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const scripts = path.resolve(__dirname, '../common/claude/.claude/dotfiles');
const { formatStatus, gitBranch } = require(path.join(scripts, 'statusline.cjs'));
const { notification } = require(path.join(scripts, 'notify.cjs'));
const { bypassReason } = require(path.join(scripts, 'check-git-hooks.cjs'));
const settings = JSON.parse(fs.readFileSync(path.join(scripts, '../settings.json'), 'utf8'));

function run(script, input) {
  return spawnSync(process.execPath, [path.join(scripts, script)], {
    input: typeof input === 'string' ? input : JSON.stringify(input),
    encoding: 'utf8',
    timeout: 5000,
  });
}

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
    const target = path.join(temporary, '.claude', 'dotfiles');
    fs.mkdirSync(target, { recursive: true });
    for (const name of fs.readdirSync(scripts)) fs.copyFileSync(path.join(scripts, name), path.join(target, name));
    const execute = (command, input) => {
      const match = command.match(/^node -e "([^"]+)"$/);
      assert.ok(match, command);
      return spawnSync(process.execPath, ['-e',
        'require("os").homedir = () => process.env.CLAUDE_DOTFILES_TEST_HOME; ' + match[1]], {
        env: { ...process.env, CLAUDE_DOTFILES_TEST_HOME: temporary },
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
