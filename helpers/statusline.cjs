'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function text(value, fallback = '') {
  if (typeof value !== 'string') return fallback;
  return value.replace(/[\x00-\x1f\x7f-\x9f]/g, '').trim().slice(0, 100) || fallback;
}

function gitBranch(cwd) {
  if (typeof cwd !== 'string' || !cwd) return '';
  const options = {
    encoding: 'utf8',
    timeout: 1000,
    stdio: ['ignore', 'pipe', 'ignore'],
    env: { ...process.env, GIT_OPTIONAL_LOCKS: '0' },
  };
  const branch = spawnSync('git', ['-C', cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD'], options);
  if (branch.status === 0) return text(branch.stdout);
  const head = spawnSync('git', ['-C', cwd, 'rev-parse', '--short', 'HEAD'], options);
  return head.status === 0 ? `detached:${text(head.stdout)}` : '';
}

function formatStatus(input, branch = '') {
  const data = input && typeof input === 'object' ? input : {};
  const cwd = data.workspace?.current_dir || data.cwd;
  const directory = typeof cwd === 'string' ? text(path.basename(cwd.replace(/\\/g, '/'))) : '';
  const model = text(data.model?.display_name, text(data.model?.id, 'Claude'));
  const effort = text(data.effort?.level);
  const used = data.context_window?.used_percentage;
  const cost = data.cost?.total_cost_usd;
  const percentage = Number.isFinite(used) && used >= 0 ? `${Math.round(used)}%` : '?';
  const dollars = Number.isFinite(cost) && cost >= 0 ? `~$${cost.toFixed(2)}` : '~$?';
  const location = [directory, branch ? `(${text(branch)})` : ''].filter(Boolean).join(' ');
  return [model, effort, location, `ctx ${percentage}`, dollars].filter(Boolean).join(' | ');
}

const USAGE_FILE = /^claude-[A-Za-z0-9_][A-Za-z0-9_-]*\.usage\.json$/;
const RECORD_AGAIN = 300;
const LAST_TIME = 253402300800; // 10000-01-01 UTC: no calendar time from here on
const SHELLS = new Set(['sh', 'bash', 'zsh', 'dash', 'ksh', 'fish']);

function usageWindow(value) {
  const used = value?.used_percentage;
  const resets = value?.resets_at;
  if (typeof used !== 'number' || !Number.isFinite(used) || used < 0 || used > 100) return null;
  return Number.isInteger(resets) && resets > 0 && resets < LAST_TIME ? { used_percentage: used, resets_at: resets } : null;
}

// [parent pid, command name] of PID, or null: /proc on Linux, ps elsewhere.
function processInfo(pid) {
  try {
    if (process.platform === 'linux') {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
      const close = stat.lastIndexOf(')');
      return [Number(stat.slice(close + 2).split(' ')[1]), stat.slice(stat.indexOf('(') + 1, close)];
    }
    const result = spawnSync('ps', ['-o', 'ppid=,comm=', '-p', String(pid)],
      { encoding: 'utf8', timeout: 1000, stdio: ['ignore', 'pipe', 'ignore'] });
    const match = result.status === 0 && /^\s*(\d+)\s+(.+?)\s*$/.exec(result.stdout);
    return match ? [Number(match[1]), match[2]] : null;
  } catch {
    return null;
  }
}

// Whether the claude process with PID runs this status line: as its parent, or behind
// the one shell Claude Code may start the command with. A claude started inside that
// session is a process of its own, so it never counts.
function underLauncher(pid, parent = process.ppid) {
  if (parent === pid) return true;
  const info = processInfo(parent);
  return Boolean(info) && info[0] === pid && SHELLS.has(path.basename(info[1]).replace(/^-/, ''));
}

// The last 5-hour and weekly rate limits of the claude-as account this session
// runs under, for claude-as --account-info. claude-as names the file, a hash of
// its token and its own pid, which stays the claude session's because it execs
// claude. Claude Code passes CLAUDE_CODE_OAUTH_TOKEN to none of the commands it
// starts, so the session is recognised by that pid; a token that is visible all
// the same must hash to the identifier. Nothing is recorded on Windows, outside an
// owned 0700 directory, or when the values are unchanged and less than five
// minutes old. Returns whether it wrote.
function recordUsage(data, env = process.env, now = Date.now()) {
  if (process.platform === 'win32') return false;
  const file = env.CODING_AGENT_KIT_USAGE_FILE;
  const launcher = /^[0-9]+$/.test(env.CODING_AGENT_KIT_CLAUDE_PID || '') ? Number(env.CODING_AGENT_KIT_CLAUDE_PID) : 0;
  if (!file || !env.CODING_AGENT_KIT_TOKEN_ID || launcher <= 1) return false;
  const token = env.CLAUDE_CODE_OAUTH_TOKEN;
  if (token && crypto.createHash('sha256').update(token).digest('hex').slice(0, 16) !== env.CODING_AGENT_KIT_TOKEN_ID) return false;
  if (!path.isAbsolute(file) || !USAGE_FILE.test(path.basename(file))) return false;
  const record = {
    five_hour: usageWindow(data?.rate_limits?.five_hour),
    seven_day: usageWindow(data?.rate_limits?.seven_day),
  };
  if (!record.five_hour && !record.seven_day) return false;
  if (!underLauncher(launcher)) return false;
  const folder = path.dirname(file);
  const info = fs.lstatSync(folder);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) return false;
  const seen = Math.floor(now / 1000);
  let old = null;
  try {
    if (fs.lstatSync(file).isFile()) old = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    // No usable earlier record: write a fresh one.
  }
  if (old && JSON.stringify([old.five_hour, old.seven_day]) === JSON.stringify([record.five_hour, record.seven_day])
    && Number.isInteger(old.seen_at) && seen - old.seen_at < RECORD_AGAIN) return false;
  // A status line run that Claude Code cancelled may have left its temporary file.
  const prefix = `.${path.basename(file)}.`;
  for (const name of fs.readdirSync(folder)) {
    if (name.startsWith(prefix) && name.endsWith('.tmp')) {
      const stale = path.join(folder, name);
      if (now - fs.lstatSync(stale).mtimeMs > 60000) fs.rmSync(stale, { force: true });
    }
  }
  const temporary = path.join(folder, `${prefix}${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try {
    fs.fchmodSync(fd, 0o600);
    fs.writeSync(fd, `${JSON.stringify({ ...record, seen_at: seen })}\n`);
  } finally {
    fs.closeSync(fd);
  }
  try {
    fs.renameSync(temporary, file);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
  return true;
}

function main() {
  let data = {};
  try {
    data = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch {
    // Optional UI data can be absent during startup; keep the footer quiet.
  }
  try {
    recordUsage(data);
  } catch {
    // Recording is a side note; the footer never depends on it.
  }
  process.stdout.write(`${formatStatus(data, gitBranch(data?.workspace?.current_dir || data?.cwd))}\n`);
}

module.exports = { formatStatus, gitBranch, main, recordUsage };
if (require.main === module) main();
