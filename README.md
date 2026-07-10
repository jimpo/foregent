# Foregent

An autonomous multi-agent software development system: agents work Linear
issues and GitHub PRs unattended on a dedicated machine per project, built on
[cli-agent-orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator)
as the agent runtime, with [herdr](https://herdr.dev/) as the terminal
backend.

**`docs/PLAN.md` is the source of truth** for design, decisions, and phase
status — read it first.

This is the second-generation architecture; the first generation (Bun/TS,
self-built agent runtime) lives in the `foregent` repo and is retired. Design
carried over, code not.

## Development model

Foregent is developed by itself as early as possible: phase 0 provisions a
devbox VM with CAO + herdr + Claude Code, this repo is pushed into it, and
CAO-launched agents develop foregent from inside the box.

- `devbox create` — provision the sandbox (see `.devbox/`, not committed).
- `devbox ssh`, then `herdr --session cao` — observe agents.
- `scripts/install-profiles.sh` — install/refresh foregent's agent profiles
  (`profiles/*.md`) into CAO's store; rerun after editing a profile.
- `cao launch --agents developer --auto-approve` — hand-drive until the
  bridge exists. The profile carries `provider: claude_code` and
  `allowedTools: ["*"]`, so no `--provider`/`--yolo` flags are needed;
  `--auto-approve` only skips the CLI's interactive confirm (the REST API the
  bridge will use has no such prompt).
