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

function usageWindow(value) {
  const used = value?.used_percentage;
  const resets = value?.resets_at;
  if (typeof used !== 'number' || !Number.isFinite(used) || used < 0 || used > 100) return null;
  return Number.isInteger(resets) && resets > 0 && resets < LAST_TIME ? { used_percentage: used, resets_at: resets } : null;
}

// The last 5-hour and weekly rate limits of the claude-as account this session
// runs under, for claude-as --account-info. claude-as names the file and a hash
// of its token; nothing is recorded unless the session still runs under that
// token, on Windows, outside an owned 0700 directory, or when the values are
// unchanged and less than five minutes old. Returns whether it wrote.
function recordUsage(data, env = process.env, now = Date.now()) {
  if (process.platform === 'win32') return false;
  const file = env.CODING_AGENT_KIT_USAGE_FILE;
  const token = env.CLAUDE_CODE_OAUTH_TOKEN;
  if (!file || !token || !env.CODING_AGENT_KIT_TOKEN_ID) return false;
  if (crypto.createHash('sha256').update(token).digest('hex').slice(0, 16) !== env.CODING_AGENT_KIT_TOKEN_ID) return false;
  if (!path.isAbsolute(file) || !USAGE_FILE.test(path.basename(file))) return false;
  const record = {
    five_hour: usageWindow(data?.rate_limits?.five_hour),
    seven_day: usageWindow(data?.rate_limits?.seven_day),
  };
  if (!record.five_hour && !record.seven_day) return false;
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
