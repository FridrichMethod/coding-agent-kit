'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
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

function usageColor(percentage) {
  if (!Number.isFinite(percentage) || percentage < 0) return 0;
  return percentage >= 90 ? 31 : percentage >= 70 ? 33 : 32;
}

// A token count as Codex writes one: three significant digits with K, M, B or T,
// trailing zeros dropped (950, 1.23K, 75.2K, 148K, 1M, 13.7M).
function tokenCount(value) {
  const count = Math.max(0, Math.round(value));
  if (count < 1000) return String(count);
  const [scale, unit] = count >= 999.5e9 ? [1e12, 'T'] : count >= 999.5e6 ? [1e9, 'B']
    : count >= 999.5e3 ? [1e6, 'M'] : [1e3, 'K'];
  const scaled = count / scale;
  return `${Number(scaled.toFixed(scaled < 10 ? 2 : scaled < 100 ? 1 : 0))}${unit}`;
}

// TOKENS is what sessionTokens returns: { total, partial } or null.
function formatStatus(input, branch = '', colors = false, tokens = null) {
  const data = input && typeof input === 'object' ? input : {};
  const cwd = data.workspace?.current_dir || data.cwd;
  const directory = typeof cwd === 'string' ? text(path.basename(cwd.replace(/\\/g, '/'))) : '';
  const model = text(data.model?.display_name, text(data.model?.id, 'Claude'));
  const effort = text(data.effort?.level);
  const used = data.context_window?.used_percentage;
  const cost = data.cost?.total_cost_usd;
  const percentage = Number.isFinite(used) && used >= 0 ? `${Math.round(used)}%` : '?';
  const dollars = Number.isFinite(cost) && cost >= 0 ? `~$${cost.toFixed(2)}` : '~$?';
  // The share of each limit left, as Codex shows it; the colour follows the share used.
  const limit = (label, window) => (window ? `${label} ${100 - Math.round(window.used_percentage)}% left` : '');
  const hourly = usageWindow(data.rate_limits?.five_hour);
  const weekly = usageWindow(data.rate_limits?.seven_day);
  // The session's tokens as Codex shows its thread's, "+" while still catching up.
  const spent = Number.isFinite(tokens?.total) && tokens.total > 0
    ? `${tokenCount(tokens.total)}${tokens.partial ? '+' : ''} used` : '';
  const location = [directory, branch ? `(${text(branch)})` : ''].filter(Boolean).join(' ');
  const paint = (value, color) => colors && value && color ? `\x1b[${color}m${value}\x1b[0m` : value;
  return [paint(model, 36), paint(effort, 35), paint(location, 34),
    paint(`ctx ${percentage}`, usageColor(used)), spent, paint(dollars, 33),
    paint(limit('5h', hourly), usageColor(hourly?.used_percentage)),
    paint(limit('weekly', weekly), usageColor(weekly?.used_percentage))].filter(Boolean).join(' | ');
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
  replacePrivate(file, `${JSON.stringify({ ...record, seen_at: seen })}\n`, now);
  return true;
}

// Replaces FILE with CONTENTS through a 0600 temporary file beside it and a rename.
function replacePrivate(file, contents, now) {
  const folder = path.dirname(file);
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
    fs.writeSync(fd, contents);
  } finally {
    fs.closeSync(fd);
  }
  try {
    fs.renameSync(temporary, file);
  } catch (error) {
    fs.rmSync(temporary, { force: true });
    throw error;
  }
}

// The tokens a session has used, counted as Codex counts its thread's
// (blended_total): input not read from the cache (input_tokens plus
// cache_creation_input_tokens) and output, over every response that the main
// transcript and its subagents' transcripts record. Claude Code gives the status
// line no such total, and reading a long session's transcripts in full takes
// over a second, so a private cache file per session keeps, for each transcript,
// how far it has been read and its sums. Each run reads only what was appended
// since, at most READ_BUDGET bytes in all, and marks a total that has not caught
// up yet. Claude Code writes one response as adjacent lines that repeat its
// usage, so a line with the message id of the line before replaces that usage.
const READ_BUDGET = 16 * 1024 * 1024;
const SUBAGENT_DEPTH = 3; // subagents/*.jsonl down to subagents/*/*/*.jsonl
const SUBAGENT_FILES = 1000;
const HEAD = 64; // bytes of a transcript's start that tell a rewritten file from the one read
const CACHE_FILE = /^[0-9a-f]{32}\.json$/;
const CACHE_AGE = 30 * 24 * 3600 * 1000;
const NONE = [0, 0, 0];

function cacheFolder(env = process.env) {
  const base = env.XDG_CACHE_HOME && path.isAbsolute(env.XDG_CACHE_HOME)
    ? env.XDG_CACHE_HOME : path.join(os.homedir(), '.cache');
  return path.join(base, 'coding-agent-kit', 'statusline');
}

// The lstat of FILE when it is a regular file of this user's, never a link; else null.
function ownFile(file) {
  const info = fs.lstatSync(file, { throwIfNoEntry: false });
  return info?.isFile() && info.uid === process.getuid() ? info : null;
}

// { files: [key, path] of the main transcript and of each subagent transcript
// beside it, truncated: whether more than SUBAGENT_FILES subagent ones were left out }.
function transcriptFiles(transcript) {
  const files = [['main', transcript]];
  let truncated = false;
  const walk = (folder, key, depth) => {
    let entries;
    try {
      entries = fs.readdirSync(folder, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
      if (truncated) return;
      const next = path.join(folder, entry.name);
      if (entry.isDirectory() && depth > 1) walk(next, `${key}/${entry.name}`, depth - 1);
      else if (entry.isFile() && entry.name.endsWith('.jsonl')) {
        if (files.length > SUBAGENT_FILES) truncated = true;
        else files.push([`${key}/${entry.name}`, next]);
      }
    }
  };
  walk(path.join(transcript.slice(0, -'.jsonl'.length), 'subagents'), 'subagents', SUBAGENT_DEPTH);
  return { files, truncated };
}

// [message id, [input, cache writes, output]] of a line with a response's usage, or null.
function responseUsage(line) {
  if (!line.includes('"usage"')) return null;
  let entry;
  try {
    entry = JSON.parse(line);
  } catch {
    return null;
  }
  const message = entry?.type === 'assistant' ? entry.message : null;
  const usage = message?.usage;
  if (!usage || typeof usage !== 'object') return null;
  const count = value => (Number.isSafeInteger(value) && value > 0 ? value : 0);
  return [typeof message.id === 'string' ? message.id : '',
    [count(usage.input_tokens), count(usage.cache_creation_input_tokens), count(usage.output_tokens)]];
}

function validRecord(value) {
  const counts = list => Array.isArray(list) && list.length === 3 && list.every(n => Number.isSafeInteger(n) && n >= 0);
  return value && typeof value === 'object' && typeof value.ino === 'string' && value.ino.length <= 40
    && typeof value.head === 'string' && /^(?:[0-9a-f]{2}){0,64}$/.test(value.head)
    && Number.isSafeInteger(value.offset) && value.offset >= 0 && typeof value.skip === 'boolean'
    && typeof value.id === 'string' && value.id.length <= 200 && counts(value.last) && counts(value.sum) ? value : null;
}

// Reads what was appended to FILE after RECORD, at most LIMIT bytes, and returns
// [the advanced record, the bytes read, whether the limit left lines unread]. A
// file that is shorter, on another inode, or starts otherwise than its first
// HEAD bytes did is read again from the start; a line longer than a whole
// BUDGET is skipped, since no line with a response's usage is that long.
function advance(record, file, info, limit, budget) {
  const ino = String(info.ino);
  const fresh = { ino, head: '', offset: 0, skip: false, id: '', last: NONE, sum: NONE };
  const kept = record && record.ino === ino && record.offset <= info.size ? record : fresh;
  if (limit <= 0 || info.size <= kept.offset) return [kept, 0, info.size > kept.offset];
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    const bytes = (length, position) => {
      const buffer = Buffer.alloc(length);
      return buffer.subarray(0, fs.readSync(fd, buffer, 0, length, position));
    };
    const start = kept.head && bytes(kept.head.length / 2, 0).toString('hex') !== kept.head ? fresh : kept;
    const chunk = bytes(Math.min(info.size - start.offset, limit), start.offset);
    if (!chunk.length) return [start, 0, false];
    const remaining = start.offset + chunk.length < info.size;
    const end = chunk.lastIndexOf(10);
    let next;
    if (end >= 0) {
      let { id, last, sum } = start;
      const from = start.skip ? chunk.indexOf(10) + 1 : 0;
      for (const line of chunk.toString('utf8', from, end).split('\n')) {
        const found = responseUsage(line);
        if (!found) continue;
        const [lineId, tokens] = found;
        const base = lineId && lineId === id ? sum.map((n, i) => n - last[i]) : sum;
        sum = base.map((n, i) => n + tokens[i]);
        [id, last] = [lineId, tokens];
      }
      next = { ...start, offset: start.offset + end + 1, skip: false, id, last, sum };
    } else if (chunk.length >= budget) {
      next = { ...start, offset: start.offset + chunk.length, skip: true };
    } else {
      return [start, chunk.length, remaining]; // No complete line yet: wait for the rest of it.
    }
    const want = Math.min(HEAD, next.offset);
    const head = next.head.length / 2 < want ? bytes(want, 0).toString('hex') : next.head;
    return [{ ...next, head }, chunk.length, remaining];
  } finally {
    fs.closeSync(fd);
  }
}

// Drops the cache files of sessions whose total has not changed for CACHE_AGE.
function pruneCache(folder, now) {
  for (const name of fs.readdirSync(folder)) {
    if (!CACHE_FILE.test(name)) continue;
    const stale = path.join(folder, name);
    const info = fs.lstatSync(stale, { throwIfNoEntry: false });
    if (info?.isFile() && now - info.mtimeMs > CACHE_AGE) fs.rmSync(stale, { force: true });
  }
}

// { total, partial } for the session whose transcript_path DATA names, or null:
// nothing is counted on Windows, without an absolute .jsonl transcript that is a
// regular file of this user's, or outside an owned 0700 cache folder.
function sessionTokens(data, { folder = cacheFolder(), budget = READ_BUDGET, now = Date.now() } = {}) {
  if (process.platform === 'win32') return null;
  const transcript = data?.transcript_path;
  if (typeof transcript !== 'string' || !path.isAbsolute(transcript) || !transcript.endsWith('.jsonl')
    || !ownFile(transcript)) return null;
  fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(folder);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o700) return null;
  const file = path.join(folder, `${crypto.createHash('sha256').update(transcript).digest('hex').slice(0, 32)}.json`);
  let old = null;
  try {
    if (ownFile(file)) old = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    // An unreadable cache is counted again from the start.
  }
  const known = old?.transcript === transcript && old.files && typeof old.files === 'object' ? old.files : {};
  const files = new Map();
  const { files: sources, truncated } = transcriptFiles(transcript);
  let left = budget;
  let partial = truncated;
  for (const [key, source] of sources) {
    try {
      const stat = ownFile(source);
      if (!stat) continue;
      const [record, read, remaining] = advance(Object.hasOwn(known, key) ? validRecord(known[key]) : null,
        source, stat, left, budget);
      files.set(key, record);
      left -= read;
      partial ||= remaining;
    } catch {
      // A transcript that cannot be read now keeps what it counted before; the total is a floor.
      partial = true;
    }
  }
  // A transcript that is gone, or unreadable now, keeps the tokens it recorded.
  for (const [key, record] of Object.entries(known)) {
    if (!files.has(key) && validRecord(record)) files.set(key, record);
  }
  const recorded = Object.fromEntries(files);
  if (old?.transcript !== transcript || JSON.stringify(recorded) !== JSON.stringify(known)) {
    if (!old) pruneCache(folder, now);
    replacePrivate(file, `${JSON.stringify({ transcript, files: recorded })}\n`, now);
  }
  let total = 0;
  for (const record of files.values()) total += record.sum[0] + record.sum[1] + record.sum[2];
  return { total, partial };
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
  let tokens = null;
  try {
    tokens = sessionTokens(data);
  } catch {
    // The token total is optional; the rest of the footer never depends on it.
  }
  const colors = !process.env.NO_COLOR && process.env.TERM !== 'dumb'
    && (process.env.CODING_AGENT_KIT_COLOR === 'always'
      || (process.env.CODING_AGENT_KIT_COLOR !== 'never' && Boolean(process.stdout.isTTY)));
  process.stdout.write(`${formatStatus(data, gitBranch(data?.workspace?.current_dir || data?.cwd), colors, tokens)}\n`);
}

module.exports = { formatStatus, gitBranch, main, recordUsage, sessionTokens };
if (require.main === module) main();
