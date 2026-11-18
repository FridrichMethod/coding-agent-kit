# Claude Code helpers

Three Node helpers in `helpers/` extend Claude Code: a status line
(`statusline.cjs`), terminal notifications (`notify.cjs`) and a Git hook
guard (`check-git-hooks.cjs`). They need Node.js 22 or newer and nothing from
npm; Git is optional for the status line.
[Wiring the helpers](#wiring-the-helpers) shows how they go into Claude
Code's `settings.json`.

## Status line

Shows the active model, reported effort, project and Git branch (or detached
commit), context usage, the tokens the session has used, estimated session
cost, and the 5-hour and weekly rate limits left. For example:

```text
Opus | xhigh | coding-agent-kit (main) | ctx 38% | 13.7M used | ~$1.23 | 5h 88% left | weekly 58% left
```

The limit segments use Claude's native `rate_limits.five_hour` and
`rate_limits.seven_day` data and show 100 minus the rounded consumed
percentage, as Codex shows its limits; each is omitted when its window is
absent or invalid, and a valid full limit shows `0% left`. Missing
context/cost data shows `?`; cost is Claude's estimate, not an invoice. It
performs only bounded Git ref reads, without scanning the worktree or
contacting the network. See the
[status-line reference](https://code.claude.com/docs/en/statusline).

Usage reserves do not need it: a `claude-as` launch with a reserve makes the
reserve guard its status line, which records the account's usage itself,
without Node, and then runs the status line command your settings name (this
helper, if they name it) for the footer
([usage reserves](account-launchers.md#what-the-guard-checks)).

### Session tokens

The `used` segment counts as Codex's `used-tokens` does: input not read from
the prompt cache (`input_tokens` plus `cache_creation_input_tokens`) and
output, summed over every response in the session's transcript
(`transcript_path`) and its subagents' transcripts under
`SESSION/subagents/`, workflow agents included; cache reads never count.

Claude Code reports no such total, and reading a long session's transcripts
in full can take over a second, so the helper keeps how far it has read each
transcript, and the sums so far, in one private cache file per session under
`${XDG_CACHE_HOME:-~/.cache}/coding-agent-kit/statusline/` (folder 0700,
files 0600, holding the transcript path, the subagent transcript names, read
offsets, a fingerprint of each transcript's first 64 bytes, the last message
id and the counts, never what was said). Each run reads only what was
appended since, at most 16 MiB, so a long session seen for the first time
catches up over a few refreshes and shows `13.7M+ used` meanwhile; `+` also
marks a total that leaves out a transcript it cannot read now, or subagent
transcripts past the first 1000. A transcript that is shorter, replaced, or
starts otherwise than when it was read is counted again from the start. A
cache unchanged for 30 days is removed when another session starts one.

The transcript format is Claude Code's internal one: the segment is omitted
when the transcript or the cache folder cannot be used, and responses the
transcripts do not record are not counted, so the count can trail the cost
estimate.

## Notifications

`permission_prompt` and `idle_prompt` events emit
[Claude's `terminalSequence`](https://code.claude.com/docs/en/hooks#emit-terminal-notifications),
using OSC 99 for Kitty, OSC 777 for Ghostty/Warp/rxvt, or OSC 9 for
iTerm2/WezTerm/Windows Terminal. The terminal handles desktop delivery,
including remote sessions when terminal identification is available. Enable
notifications for the terminal in OS settings; unsupported terminals and
non-interactive sessions may ignore them. The notice contains only the
project name and a fixed status, never prompt or command contents. Other
events and notification types produce nothing.

## Git hook checks

A local `PreToolUse` helper does the common bypass checks of
`block-no-verify@1.1.2` in process, without npm startup or downloads: it
blocks the usual `--no-verify`, commit `-n`, and `-c core.hooksPath=...`
forms. This is a conservative text check, not a shell parser; quoted text can
trigger it, and indirect bypasses are outside its scope. Successful checks do
not grant permission or skip the classifier.

## Wiring the helpers

A [checkout install](install.md#from-a-checkout) prints the helper commands
with the checkout's absolute path. In Claude Code's `settings.json`, the
helpers go in as:

```json
{
  "statusLine": {
    "type": "command",
    "command": "node \"/home/u/src/coding-agent-kit/helpers/statusline.cjs\""
  },
  "hooks": {
    "Notification": [
      {
        "matcher": "permission_prompt|idle_prompt",
        "hooks": [{ "type": "command", "command": "node \"/home/u/src/coding-agent-kit/helpers/notify.cjs\"" }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "node \"/home/u/src/coding-agent-kit/helpers/check-git-hooks.cjs\"" }]
      }
    ]
  }
}
```

A `settings.json` shared by machines whose home directories differ can name
the helpers relative to the home directory instead: a
[managed install](install.md#managed-install) puts them in
`~/.claude/coding-agent-kit/` (or copy or link `helpers/*.cjs` there
yourself), and each command takes this form, with `notify.cjs` and
`check-git-hooks.cjs` for the two hooks; a managed install prints all three
when it places a link:

```
node -e "require(require('os').homedir() + '/.claude/coding-agent-kit/statusline.cjs').main()"
```

Each helper exports `main`, which does what running the file does.

## Colour: launchers and status line

The launchers style only their own messages: headings cyan, questions and
warnings yellow, saves green, errors red. Native Codex and Claude output,
completion lists and the output of `--list`, `--labels` and `--show-config`
are never styled. `--info` styles only its own words, under the same policy
for its standard output: the heading cyan, missing data yellow, and each
window's use in the status line's colours (green, yellow from 70%, red from
90%). Stored emails, plans and labels stay plain. Under `auto` piped output
stays plain too, so a preview that pipes `--info`, such as an fzf-tab one,
sets `CODING_AGENT_KIT_COLOR=always` for it.

- `CODING_AGENT_KIT_COLOR=auto` (also when unset or invalid) styles a message
  only when its destination is a terminal, so redirected logs stay plain;
  `always` and `never` force it.
- A non-empty `NO_COLOR` or `TERM=dumb` turns colour off, even with `always`.

The status line follows the same colour policy: model cyan, effort magenta,
location blue, and estimated cost yellow. Context usage and the 5-hour and
weekly limits are green below 70% used, yellow at 70% used or above, and red at
90% used or above, using the reported percentage before rounding, so a limit
segment turns yellow at 30% left or below and red at 10% left or below. Unknown
context usage and the session tokens stay uncoloured.
Claude captures status-line stdout, so `auto` stays plain there. Set
`CODING_AGENT_KIT_COLOR=always` in the status-line command to enable colours in
Claude; `NO_COLOR` and `TERM=dumb` still take precedence. Direct `formatStatus`
calls remain plain unless styling is explicitly requested.
