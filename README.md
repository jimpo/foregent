# Foregent

An autonomous multi-agent software development system: agents work Linear
issues and GitHub PRs unattended on a dedicated machine per project, built on
[herdr](https://herdr.dev/) as the terminal and agent-state substrate, with
[Claude Code](https://claude.com/claude-code) as the agent harness.

**`docs/PLAN.md` is the source of truth** for design, decisions, and phase
status — read it first.

This is the second-generation architecture; the first generation (Bun/TS,
self-built agent runtime) lives in the `foregent` repo and is retired. Design
carried over, code not.

## Development model

Foregent is developed by itself as early as possible: phase 0 provisions a
devbox VM with herdr + Claude Code, this repo is pushed into it, and
foregent-launched agents develop foregent from inside the box.

- `devbox create` — provision the sandbox (see `.devbox/`, not committed).
- `devbox ssh`, then `herdr --session foregent` — observe agents. From a
  laptop: `herdr --remote <ssh-target> --session foregent`. Attaching is
  read-only by convention; watch, don't type.
- Hand-drive until the bridge exists — one agent per issue, in its own
  workspace:

  ```sh
  herdr --session foregent workspace create --cwd ~/ws/JIM-52 --label JIM-52
  herdr --session foregent agent start fg-jim-52 --kind claude --pane <pane_id> -- \
      --session-id "$(uuidgen)" --permission-mode bypassPermissions --model opus
  herdr --session foregent agent prompt fg-jim-52 "You own JIM-52. Use the foregent-worker skill."
  ```

  The agent owns the issue end to end; if it wants to fan work out to
  subagents, that is its call, not foregent's (`docs/PLAN.md` §5.2).
- Agent workflow lives in the `foregent-worker` skill, installed into
  `~/.claude/skills/` at provision time (§5.8) — not in a profile file.
