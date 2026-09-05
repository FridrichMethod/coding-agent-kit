'use strict';

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

function main() {
  let data = {};
  try {
    data = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch {
    // Optional UI data can be absent during startup; keep the footer quiet.
  }
  process.stdout.write(`${formatStatus(data, gitBranch(data?.workspace?.current_dir || data?.cwd))}\n`);
}

module.exports = { formatStatus, gitBranch, main };
if (require.main === module) main();
