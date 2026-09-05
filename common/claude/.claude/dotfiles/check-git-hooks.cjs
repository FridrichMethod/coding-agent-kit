'use strict';

const fs = require('node:fs');

// Local replacement for the common bypass checks in block-no-verify@1.1.2.
// This conservative text check is not a shell parser or a security boundary.
// It never approves a tool call; normal permissions and auto review still run.
function bypassReason(command) {
  const git = /(?:^|[\s;&|$`(<{!"'\]/.~\\])git(?:\.[eE][xX][eE])?(?=[\s"'])/g;
  const verbs = /(?:^|\s)(commit|push|merge|cherry-pick|rebase|am)(?=$|[\s;&#|>)\]}"'])/g;
  // Skip full-line comments, without trying to interpret quotes or shell code.
  const source = command.replace(/^\s*#.*$/gm, '');
  for (const match of source.matchAll(git)) {
    const segment = source.slice(match.index + match[0].length).split(/[;|]/, 1)[0];
    for (const verb of segment.matchAll(verbs)) {
      if (/--no-verify\b/.test(source)) {
        return 'Git hooks must run: remove --no-verify and fix the failing check.';
      }
      if (verb[1] === 'commit' && /\s-n(?:\s|$|[a-zA-Z])/.test(source)) {
        return 'Git commit -n skips hooks: remove it and fix the failing check.';
      }
      if (/-c\s+["']?core\.hooksPath\s*=/.test(source)) {
        return 'Do not override core.hooksPath to skip Git hooks.';
      }
    }
  }
  return '';
}

function main() {
  try {
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    if (typeof input?.tool_input?.command !== 'string') {
      throw new Error('missing tool_input.command');
    }
    const reason = bypassReason(input.tool_input.command);
    if (reason) {
      process.stderr.write(`${reason}\n`);
      process.exitCode = 2;
    }
  } catch {
    process.stderr.write('Cannot inspect Git hook flags: invalid Claude hook input.\n');
    process.exitCode = 2;
  }
}

module.exports = { bypassReason, main };
if (require.main === module) main();
