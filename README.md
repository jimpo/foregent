# Foregent

An autonomous multi-agent software development system: agents work Linear
issues and GitHub PRs unattended on a dedicated machine per project, built on
[herdr](https://herdr.dev/) as the terminal and agent-state substrate, with
[Claude Code](https://claude.com/claude-code) and
[Codex](https://developers.openai.com/codex/cli) as the agent harnesses.

Foregent is not itself a harness. It provides solutions for:

- Multi-agent orchestration and worktree isolation, and
- Delivery of webhook notifications from Linear and GitHub to workers.

**`docs/ARCHITECTURE.md` describes the system** — read it first. Planned work
lives in the Linear project *Foregent*.

## How it works

`foregent serve` runs a small FastAPI service — the *bridge*. You give it a
Linear issue key and a directory; it claims the issue in Linear, opens a herdr
workspace on that directory, starts an agent in it, and briefs it with the
`foregent-worker` skill. The agent owns the issue end to end. Which harness it
runs is yours to name — `--provider claude` or `--provider codex`, Claude Code
by default — and nothing else about the dispatch changes with it.

Linear pushes what happens next. A comment or a field change on an agent's own
issue arrives at `POST /webhooks/linear`, is authenticated against the
workspace's signing secret, and is delivered to that agent as a prompt —
whether it is working or parked on a blocker. Deliveries are queued, so the
route answers Linear at once instead of waiting on a busy agent. GitHub pushes
the other half: a review or a review comment on an agent's pull request arrives
at `POST /webhooks/github` and reaches the same agent, matched by the issue its
branch names. The agent reports back through two MCP tools the bridge serves:
`report_blocked` and `complete_task`.

The bridge keeps no database. Its issue → agent map is in memory and is rebuilt
from the live herdr agents at startup.

## Requirements

Install these on the machine that runs the agents:

- **[uv](https://docs.astral.sh/uv/)** — Python 3.13+ toolchain and runner.
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **[herdr](https://herdr.dev/)** — terminal and agent-state server.
  `curl -fsSL https://herdr.dev/install.sh | sh`
- **An agent harness**, one or both. Each binary must be on `PATH`, because
  `foregent setup` calls it, and each must be signed in — an unauthenticated
  harness opens a login screen that herdr reads as a stuck agent.
  - **[Claude Code](https://claude.com/claude-code)** — the default.
  - **[Codex](https://developers.openai.com/codex/cli)** — `codex login`.
- **[Jujutsu](https://jj-vcs.github.io/jj/) (`jj`)** — the version control the
  worker skill tells agents to use. Colocated with git.
- **[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)**
  — the public HTTPS front for the webhook endpoint. The bridge listens on
  localhost and Linear will not deliver to it directly.
- A **Linear API key** and, for Pull Request mode, a **GitHub token**.

## Install

From a checkout, for development:

```sh
uv sync
uv run foregent --version
```

Or install the CLI on the box:

```sh
uv tool install .
```

The rest of this document writes `foregent`; from a checkout, prefix it with
`uv run`.

## One-time machine setup

### 1. Put the credentials in the environment

```sh
export LINEAR_API_KEY=lin_api_...      # bridge and agents
export GITHUB_TOKEN=ghp_...            # agents, Pull Request mode
```

The Linear key identifies foregent itself: the bridge assigns claimed issues to
that account, and ignores events written by it, so agents never wake on their
own comments. Create one in Linear under
[Settings → API](https://linear.app/docs/api-and-webhooks).

These belong in the environment of the **herdr server**, because every pane
herdr opens inherits it. A herdr server started before the variables were
exported gives its agents nothing.

### 2. Provision the harnesses

```sh
foregent setup
```

For every harness foregent can run agents on, this copies the skills foregent
ships into that harness's own skill directory — `~/.claude/skills/` and
`~/.codex/skills/`, which read the same `SKILL.md` — and adds the Linear and
GitHub MCP servers to its machine-level config, where agents and your own
sessions both read them. Both configs store the *name* of the variable holding
each token, never the token itself.

Every harness is provisioned whether or not you will queue it: the files are
small, and one provisioned first-use-first is one whose first dispatch is
where you find out it was not.

Run it once per machine, and again after every foregent upgrade. The bridge
also rewrites the packaged skills before every launch, so an agent is always
briefed from the version foregent ships — a hand-edited skill in a harness's
skill directory does not survive a dispatch.

`setup` warns when a credential is unset. Fix that before dispatching: an
unauthenticated agent discovers the problem only once it is working an issue.

### 3. Install the herdr integrations

```sh
herdr integration install claude
herdr integration install codex
```

This lets each harness report its session identity back to herdr.

### 4. Pre-accept the workspace trust dialog

Both harnesses ask whether they may work in a directory they have not seen, and
answer nothing until told. herdr reads that dialog as `blocked`, so **dispatch
into an untrusted directory fails**.

Every agent runs in a fresh per-issue workspace, so for Claude Code trust the
directory those are built under — once, and every workspace under it is
covered, in `~/.claude.json`:

```json
{ "projects": { "/home/you/.foregent/workspaces": { "hasTrustDialogAccepted": true } } }
```

Foregent falls back to writing the entry for each workspace it creates if it
finds one untrusted, so a box that skips this still dispatches. Doing it here
is better: `~/.claude.json` is rewritten by every running Claude Code session,
and the entry above means foregent never has to touch it.

Codex inherits nothing from a parent directory — it resolves trust to a git
repository's root, and a jj workspace has no `.git` — and its
`--dangerously-bypass-approvals-and-sandbox` does not skip the dialog either.
So foregent appends an entry per workspace to `~/.codex/config.toml`:

```toml
[projects."/home/you/.foregent/workspaces/JIM-42"]
trust_level = "trusted"
```

There is nothing to pre-accept for Codex, and that file grows one entry per
issue.

## Webhook ingress (Cloudflare tunnel)

Linear and GitHub deliver to a public HTTPS URL, and the bridge listens on
localhost, so something has to front it. `cloudflared` does, and a quick tunnel needs no
account and no domain:

```sh
cloudflared tunnel --url http://127.0.0.1:8577
```

It prints a `https://<random>.trycloudflare.com` URL. That URL changes every
time the process restarts, which is fine while you are trying this out and no
good for a box that stays up — for that, create a
[named tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/)
against a domain you own, and run it as a service.

Then, in Linear, open **Settings → Administration → API → New webhook** (admin
permission required) and point it at `<tunnel URL>/webhooks/linear`. Subscribe
it to issues and comments. Copy the signing secret Linear shows you into the
bridge's environment, next to the other credentials:

```sh
export LINEAR_WEBHOOK_SECRET=lin_wh_...
```

Restart the bridge and comment on a tracked issue. The bridge answers 401 for a
delivery whose signature does not match, and 503 when it holds no secret to
check one against. See Linear's
[webhook documentation](https://linear.app/developers/webhooks) for the payload
shapes and the retry schedule — a delivery is retried three times, after one
minute, one hour, and six hours, so a bridge that is down for a working day
loses the event.

An authenticated delivery about an issue no agent here is working — which is
most of them — is answered 200 and dropped, so Linear is never told a delivery
failed for being none of foregent's business. The bridge logs the deliveries
it queues for an agent and stays quiet about the rest, so to confirm the
webhook is wired up, comment on an issue an agent is actually working.

### The GitHub webhook

In Pull Request mode, the same tunnel carries what GitHub says about the pull
requests agents open. Create an
[organization webhook](https://docs.github.com/en/webhooks/using-webhooks/creating-webhooks#creating-an-organization-webhook)
— one hook covers every repository foregent works — pointed at
`<tunnel URL>/webhooks/github`, with **content type `application/json`**;
foregent does not read the form-encoded delivery and answers 400 for one.
Subscribe it to pull request reviews and review comments. Put the secret
GitHub asks you to invent in the bridge's environment:

```sh
export GITHUB_WEBHOOK_SECRET=...
```

The same answers as the Linear endpoint: 401 for a signature that does not
match, 503 when the bridge holds no secret to check one against, and 200 for
everything it accepts. GitHub keeps every attempt and the code it got back
under **Recent Deliveries** on the hook's own page, so the `ping` it sends on
creating the hook is the quickest confirmation the endpoint is reachable.

## Running the bridge

Agents run inside a herdr session. Start or attach to the one foregent should
use:

```sh
herdr --session foregent
```

Then run the bridge in a pane of that session — herdr injects
`HERDR_SOCKET_PATH` into every pane it owns, so the bridge finds its own
session:

```sh
foregent serve          # add --dev to restart on source changes
```

Outside a pane (a systemd unit, a plain SSH shell), name the session
explicitly, or the bridge lands in herdr's *default* session:

```sh
FOREGENT_HERDR_SESSION=foregent foregent serve
```

Startup logs the session and socket it resolved to, and a warning for any MCP
server or credential the machine is missing.

> **Do not start the herdr server from inside a Claude Code session.** Every
> pane inherits `CLAUDECODE=1`, which silently disables transcript saving in
> the agents and breaks resume.

## Working an issue

```sh
foregent queue JIM-42 -d ~/src/myrepo   # queue an issue against a repo
foregent queue JIM-42 -d ~/src/myrepo --provider codex   # …on Codex instead
foregent status                         # what is tracked, and its state
```

`status` heads its table with when Linear last delivered. Agents are woken by
webhook and nothing else, so a hook that has stopped looks exactly like a
quiet morning; a timestamp hours old is what tells the two apart.

`queue` records the issue and dispatches it if there is capacity. **How many
agents run at once is the project's mode**: bootstrap mode is one at a time,
because the bridge advances `main` onto each agent's work and the next
workspace is built from it; Pull Request mode runs up to `FOREGENT_MAX_AGENTS`
(default 3), each in its own workspace with its own branch. Dispatch assigns
the issue to the foregent account in Linear and moves it to `In Progress`, and
completion moves it to `Done` unless the agent already closed or cancelled it,
so the team must have states with exactly those two names. A queued issue
waits its turn, in the order it was queued.

`-d` is the **repository**, not the agent's working directory. Dispatch builds
the agent a jj workspace of its own from it — `~/.foregent/workspaces/JIM-42`
by default, `FOREGENT_WORKSPACE_ROOT` elsewhere — created fresh on `main`, so
no agent inherits the last one's dirty working copy. Completion removes it. A
directory that is not a jj repo is used as the agent's cwd as it stands.

Inside a workspace there is no `.git`, so raw `git` and `gh` do not work there;
`jj` does, and reaches the same repository.

A fresh workspace holds only what version control tracks, so an untracked
`.env` or local settings file is not in it. List those in a
**`.worktreeinclude`** file at the repository root and dispatch copies them
into every workspace it builds. The file is
[Claude Code's convention](https://code.claude.com/docs/en/worktrees#copy-gitignored-files-into-worktrees)
and uses `.gitignore` syntax; a path is copied when it matches the file *and*
is itself gitignored, so tracked files are never duplicated. A symlink is
recreated as a symlink to the file it originally named, not followed and
copied.

```text
.env
.env.local
config/secrets.json
```

What the agent does next is the `foregent-worker` skill
(`src/foregent/skills/foregent-worker/SKILL.md`): read the issue, do the work,
keep Linear current, land the change in the mode foregent read off the repo's
git remotes and named in the brief — for foregent itself, as a pull request —
then call `complete_task`.
Completion tears the agent down and dispatches the next queued issue.

An agent that hits an external dependency calls `report_blocked` and **stays
alive** in its workspace with its context intact. It keeps holding the capacity
slot, so `FOREGENT_MAX_AGENTS` is in practice how many pull requests may be
open and waiting for review at once. Comment on the issue in Linear, or review
the agent's pull request on GitHub; either reaches the agent as a prompt, and
delivering it unblocks the issue. A review is matched to the agent by the
branch it is on, so nothing has to be told which pull request is whose.

Observe by attaching, from the box or from a laptop:

```sh
herdr --session foregent
herdr --remote <ssh-target> --session foregent
```

Attaching is read-only by convention. Watch; do not type.

## FOREGENT.md

A managed repo can put a `FOREGENT.md` at its root. It is the first thing an
agent reads, it holds the project's own rules — what the issue labels mean, how
work is scoped — and it **wins over the worker skill wherever the two differ**.
This repo's own copy is a working example: it maps `bug`, `feature`,
`refactor`, `design`, and `investigation` onto what the agent should produce for
each.

A project with no `FOREGENT.md` has no project-specific rules, which is a valid
state.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `LINEAR_API_KEY` | The bridge claims issues with it; agents authenticate the Linear MCP with it. Required. |
| `GITHUB_TOKEN` | Agents authenticate the GitHub MCP with it. |
| `LINEAR_WEBHOOK_SECRET` | The signing secret of the Linear webhook, checked against every delivery. Required: without it `POST /webhooks/linear` answers 503. |
| `GITHUB_WEBHOOK_SECRET` | The secret of the GitHub webhook, checked against every delivery. Without it `POST /webhooks/github` answers 503. |
| `FOREGENT_HERDR_SESSION` | The herdr session to run agents in. Falls back to the session the bridge process runs in, then herdr's default. |
| `FOREGENT_API_URL` | Base URL of the bridge (default `http://127.0.0.1:8577`). `serve` binds the host and port from it; the CLI and the agents' MCP config both address it. |
| `FOREGENT_WORKSPACE_ROOT` | Where per-issue workspaces are built (default `~/.foregent/workspaces`). |
| `FOREGENT_LOG_LEVEL` | What level `serve` logs at (default `info`), for uvicorn's loggers and foregent's own. `--log-level` overrides it. |
| `FOREGENT_MAX_AGENTS` | How many agents run at once in Pull Request mode (default 3). Bootstrap mode is always one. |
| `CLAUDE_CONFIG_DIR` | Relocates `~/.claude`, honored by `foregent setup`. |
| `CODEX_HOME` | Relocates `~/.codex`, the same. |

## Development

```sh
uv run python -m unittest discover -s tests -t .   # 358 unit tests, ~7s
uv run ty check                                    # type check
```

The unit tests touch no network and no herdr. Two integration suites are
skipped unless you opt in:

```sh
FOREGENT_HERDR_AGENT_TESTS=1 uv run python -m unittest tests.test_herdr_integration
```

runs real Claude Code agents in the herdr session your shell is in — slow, and
it costs tokens.

```sh
LINEAR_API_KEY=... LINEAR_TEST_ISSUE_ID=JIM-1 uv run python -m unittest tests.test_linear_integration
```

talks to the real Linear API and **mutates that issue** (assignee and state).
Use a scratch issue.

Foregent is developed by itself: issues in the Linear project *Foregent* are
worked by foregent-launched agents inside a devbox. Keep
`docs/ARCHITECTURE.md` correct in the same commit as any change that makes
it wrong.
