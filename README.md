# Foregent

An autonomous multi-agent software development system: agents work Linear
issues and GitHub PRs unattended on a dedicated machine per project, built on
[herdr](https://herdr.dev/) as the terminal and agent-state substrate, with
[Claude Code](https://claude.com/claude-code) as the agent harness.

Foregent is not itself a harness. It provides solutions for:

- Multi-agent orchestration and worktree isolation, and
- Delivery of webhook notifications from Linear and GitHub to workers.

**`docs/ARCHITECTURE.md` describes the system** — read it first. Planned work
lives in the Linear project *Foregent*.

## How it works

`foregent serve` runs a small FastAPI service — the *bridge*. You give it a
Linear issue key and a directory; it claims the issue in Linear, opens a herdr
workspace on that directory, starts a Claude Code agent in it, and briefs the
agent with the `foregent-worker` skill. The agent owns the issue end to end.

Linear pushes what happens next. A comment or a field change on an agent's own
issue arrives at `POST /webhooks/linear`, is authenticated against the
workspace's signing secret, and is delivered to that agent as a prompt —
whether it is working or parked on a blocker. Deliveries are queued, so the
route answers Linear at once instead of waiting on a busy agent. The agent
reports back through two MCP tools the bridge serves: `report_blocked` and
`complete_task`.

> **Migration in progress.** A 30-second poll of Linear still runs beside the
> webhook route, and both feed the same delivery queue, so an agent can be
> told about the same comment twice. That is harmless — it re-reads its issue
> and carries on — and JIM-134 ends it by deleting the tick and
> `FOREGENT_POLL_INTERVAL` with it.

The bridge keeps no database. Its issue → agent map is in memory and is rebuilt
from the live herdr agents at startup.

## Requirements

Install these on the machine that runs the agents:

- **[uv](https://docs.astral.sh/uv/)** — Python 3.13+ toolchain and runner.
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **[herdr](https://herdr.dev/)** — terminal and agent-state server.
  `curl -fsSL https://herdr.dev/install.sh | sh`
- **[Claude Code](https://claude.com/claude-code)** — the agent harness. The
  `claude` binary must be on `PATH`; `foregent setup` calls it.
- **[Jujutsu](https://jj-vcs.github.io/jj/) (`jj`)** — the version control the
  worker skill tells agents to use. Colocated with git.
- **[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)**
  — the public HTTPS front for the webhook endpoint. The bridge listens on
  localhost and Linear will not deliver to it directly.
- A **Linear API key** and, for GitHub mode, a **GitHub token**.

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
export GITHUB_TOKEN=ghp_...            # agents, GitHub mode
```

The Linear key identifies foregent itself: the bridge assigns claimed issues to
that account, and ignores events written by it, so agents never wake on their
own comments. Create one in Linear under
[Settings → API](https://linear.app/docs/api-and-webhooks).

These belong in the environment of the **herdr server**, because every pane
herdr opens inherits it. A herdr server started before the variables were
exported gives its agents nothing.

### 2. Provision Claude Code

```sh
foregent setup
```

This copies every skill foregent ships into `~/.claude/skills/` and adds the
Linear and GitHub MCP servers to the machine's user-level Claude Code config,
where agents and your own sessions both read them. The config stores
`${LINEAR_API_KEY}`, never the token itself.

Run it once per machine, and **again after every foregent upgrade** — it is the
only thing that updates a stale skill. The bridge writes missing skills before a
launch, but never overwrites.

`setup` warns when a credential is unset. Fix that before dispatching: an
unauthenticated agent discovers the problem only once it is working an issue.

### 3. Install the herdr Claude integration

```sh
herdr integration install claude
```

This lets Claude Code report its session identity back to herdr.

### 4. Pre-accept the workspace trust dialog

Claude Code asks `Yes, I trust this folder` in a directory it has not seen, and
answers nothing until it is told. herdr reads that dialog as `blocked`, so
**dispatch into an untrusted directory fails**.

Every agent runs in a fresh per-issue workspace, so trust the directory those
are built under — once, and every workspace under it is covered:

```json
{ "projects": { "/home/you/.foregent/workspaces": { "hasTrustDialogAccepted": true } } }
```

Foregent falls back to writing the entry for each workspace it creates if it
finds one untrusted, so a box that skips this still dispatches. Doing it here
is better: `~/.claude.json` is rewritten by every running Claude Code session,
and the entry above means foregent never has to touch it.

## Webhook ingress (Cloudflare tunnel)

Linear delivers to a public HTTPS URL, and the bridge listens on localhost, so
something has to front it. `cloudflared` does, and a quick tunnel needs no
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
foregent status                         # what is tracked, and its state
```

`queue` records the issue and dispatches it if there is capacity — **one running
agent at a time**. Dispatch assigns the issue to the foregent account in Linear
and moves it to `In Progress`, so the team must have a state with exactly that
name. A queued issue waits until the running agent finishes.

`-d` is the **repository**, not the agent's working directory. Dispatch builds
the agent a jj workspace of its own from it — `~/.foregent/workspaces/JIM-42`
by default, `FOREGENT_WORKSPACE_ROOT` elsewhere — created fresh on `main`, so
no agent inherits the last one's dirty working copy. Completion removes it. A
directory that is not a jj repo is used as the agent's cwd as it stands.

Inside a workspace there is no `.git`, so raw `git` and `gh` do not work there;
`jj` does, and reaches the same repository.

What the agent does next is the `foregent-worker` skill
(`src/foregent/skills/foregent-worker/SKILL.md`): read the issue, do the work,
keep Linear current, rebase onto `main` and fast-forward it (bootstrap mode),
then call `complete_task`. Completion tears the agent down and dispatches the
next queued issue.

An agent that hits an external dependency calls `report_blocked` and **stays
alive** in its workspace with its context intact. It keeps holding the capacity
slot. Comment on the issue in Linear; the comment reaches the agent as a
prompt, and delivering it unblocks the issue.

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
| `LINEAR_API_KEY` | The bridge polls and claims with it; agents authenticate the Linear MCP with it. Required. |
| `GITHUB_TOKEN` | Agents authenticate the GitHub MCP with it. |
| `LINEAR_WEBHOOK_SECRET` | The signing secret of the Linear webhook, checked against every delivery. Required: without it `POST /webhooks/linear` answers 503. |
| `FOREGENT_HERDR_SESSION` | The herdr session to run agents in. Falls back to the session the bridge process runs in, then herdr's default. |
| `FOREGENT_API_URL` | Base URL of the bridge (default `http://127.0.0.1:8577`). `serve` binds the host and port from it; the CLI and the agents' MCP config both address it. |
| `FOREGENT_POLL_INTERVAL` | Seconds between Linear polls (default 30). Transitional — it goes away with the tick (JIM-134). |
| `FOREGENT_WORKSPACE_ROOT` | Where per-issue workspaces are built (default `~/.foregent/workspaces`). |
| `CLAUDE_CONFIG_DIR` | Relocates `~/.claude`, honored by `foregent setup`. |

## Development

```sh
uv run python -m unittest discover -s tests -t .   # 272 unit tests, ~4s
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
