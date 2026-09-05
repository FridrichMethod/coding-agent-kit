'use strict';

const fs = require('node:fs');
const path = require('node:path');

// Return terminalSequence through Claude's hook API: hooks have no /dev/tty.
// The terminal displays the notification locally, including over SSH/WSL.
function notification(input, env = process.env) {
  if (input?.hook_event_name !== 'Notification') return null;
  const messages = {
    permission_prompt: 'Approval needed',
    idle_prompt: 'Ready for your input',
  };
  if (!Object.hasOwn(messages, input.notification_type)) return null;
  const message = messages[input.notification_type];
  const cwd = typeof input.cwd === 'string' ? input.cwd : '';
  const project = path.basename(cwd.replace(/\\/g, '/'))
    .replace(/[\x00-\x1f\x7f-\x9f;]/g, '').slice(0, 60);
  const title = project ? `Claude Code (${project})` : 'Claude Code';
  // Use fixed messages so prompts and command contents never reach the desktop.
  const terminal = `${env.TERM_PROGRAM || ''} ${env.TERM || ''}`.toLowerCase();
  let sequence;
  if (env.KITTY_WINDOW_ID || terminal.includes('kitty')) {
    sequence = `\x1b]99;;${title}: ${message}\x1b\\`;
  } else if (/ghostty|warp|rxvt/.test(terminal)) {
    sequence = `\x1b]777;notify;${title};${message}\x07`;
  } else {
    // iTerm2, WezTerm and Windows Terminal. Other terminals may ignore OSC 9.
    sequence = `\x1b]9;${title}: ${message}\x07`;
  }
  return { terminalSequence: sequence };
}

function main() {
  try {
    const output = notification(JSON.parse(fs.readFileSync(0, 'utf8')));
    if (output) process.stdout.write(`${JSON.stringify(output)}\n`);
  } catch {
    // A malformed event should never interrupt a Claude session.
  }
}

module.exports = { notification, main };
if (require.main === module) main();
