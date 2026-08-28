# Foregent — Architecture

Foregent runs a fleet of coding agents unattended on a dedicated machine per
project, driven through Linear and GitHub. This document describes the system
as it is. Planned work lives in Linear; where a part is unbuilt and a reader
would assume otherwise, it says so.

## 1. Design decisions

### 1.1 The machine is the security boundary

Agents run with every permission prompt disabled
(`--permission-mode bypassPermissions`). Nothing stops an agent before it
runs a command, writes a file, or calls a network service. This is what makes
unattended operation possible: an agent that stops to ask is an agent that
has stopped, and nobody is watching to answer.

**Foregent therefore runs inside a security sandbox** — one dedicated,
disposable VM or container per project. The sandbox is the containment
boundary, not the agent and not the harness. Four things follow, and are
designed for:

- **One project per box.** Every agent on a box inherits the same
  machine-level configuration and reaches every credential on it. Separation
  between projects is separation between machines.
- **Credentials belong to the box.** `LINEAR_API_KEY` and `GITHUB_TOKEN` live
  in the herdr server's environment and expand per session (§6.3). An agent
  cannot be given a narrower set than its box has.
- **Nothing on the box is irreplaceable.** Repositories are clones and state
  is rebuilt at boot (§5.4). The box is rebuilt, not repaired.
- **The operator watches and does not interact** (§8.2).

The cost: a confused or hostile agent can do anything its box can. That is
bounded by keeping the box narrow in reach and cheap to destroy. Do not run
foregent on a workstation.

### 1.2 Build on herdr

herdr owns terminals, panes, processes and agent state, and reports agent
death as an event rather than something to probe for. Foregent reimplements
none of it. It launches, prompts and reaps agents through herdr, and spends
its own effort on dispatch, event delivery and provisioning.

The dependency is real: herdr is young and solo-maintained. A protocol
mismatch stops the bridge at startup rather than surfacing mid-dispatch.

### 1.3 Claude Code is a harness, not a foundation

Claude Code is the only harness today, and the design keeps it from being the
only one possible. Everything harness-specific — the argv, the status
mapping, the socket calls — sits behind `AgentManager` (§7). Nothing above
that seam knows what an agent is running.

### 1.4 Linear and herdr hold the state

The bridge keeps no persistent store. Linear holds issue truth and ownership;
herdr holds the live agents. The bridge's own issue map is an in-memory
cache, and a restart rebuilds it from those two.

Unbuilt: durable per-issue metadata. The conversation id and workspace path
have nowhere to live across a reboot, so a restart recovers which issues are
running but not enough to resume them (§5.4).

### 1.5 One agent owns one issue, end to end

There is no supervisor and no worker hierarchy. One agent reads the issue,
drives its Linear status, writes the code, and closes it out. How it
decomposes the work, including whether it spawns its own subagents, is its
business.

### 1.6 The blocker is a note, not a key

A parked agent reports what it is waiting for in its own words. The bridge
records the text for the operator and never matches on it. Routing is by
issue: an event reaches the agent that owns the issue the event is about.

### 1.7 A blocked agent parks alive

Nothing is terminated when an agent blocks. The process stays up and idle in
its workspace, holding its context and its capacity slot, until the event it
needs arrives. Waking it is a prompt, not a relaunch.

### 1.8 Events are foregent's own shape

A provider payload is mapped to a foregent `Event` at the edge, and
everything downstream matches on that. A transport is a source feeding one
matcher, which is what lets GitHub become a second source rather than a
second pipeline.

## 2. Shape

One machine per project. Three layers.

```
            Linear / GitHub  (cloud)
                 │ webhooks            ▲ comments, PRs, issue updates
                 ▼ (HTTPS ingress)     │ (written by agents via MCP)
  ┌───────────────────────────────────┴─────────────┐
  │ foregent bridge (Python / FastAPI, stateless)   │
  │  • event ingest, authentication, matching       │
  │  • delivery queue + drainer                     │
  │  • dispatch and capacity                        │
  │  • AgentManager (herdr + Claude Code)           │
  │  • foregent MCP server (mounted at /mcp)        │
  │  • skill and MCP provisioning                   │
  │  • per-issue jj workspaces                      │
  └───────────────┬─────────────────────────────────┘
                  │ unix socket (newline-delimited JSON)
                  ▼
  ┌─────────────────────────────────────────────────┐
  │ herdr server (headless, systemd, one session)   │
  │   workspaces • panes • agent state • events     │
  └───────────────┬─────────────────────────────────┘
                  ▼
        Claude Code sessions, one per issue
```

Agents talk to the world through the Linear and GitHub MCP servers, and to
the bridge through the foregent MCP server.

## 3. Modules

| Module | Responsibility |
|---|---|
| `server.py` | The bridge: HTTP routes, the webhook endpoint, dispatch, the delivery queue and drainer, the harness-event watcher, the mounted MCP server. |
| `store.py` | `IssueStore`, the in-memory issue map, and what counts as in-flight. |
| `models.py` | `Issue` and `IssueStatus`. |
| `events.py` | `Event`, `EventKind`, and the pure `wakes()` and `delivery_message()`. No transport, no server. |
| `linear.py` | Linear GraphQL client: claim an issue, resolve foregent's account, authenticate a webhook, map a payload to an `Event`. |
| `herdr.py` | The herdr socket client: newline-delimited JSON, session resolution, protocol check. |
| `agents/base.py` | The `AgentManager` protocol and its types. Harness-agnostic. |
| `agents/herdr_claude.py` | The one implementation: renders a `LaunchSpec` to `claude` flags, drives `workspace.create` → `agent.start` → `agent.prompt`, translates herdr's events. |
| `workspaces.py` | Per-issue jj workspaces: create at dispatch, remove at completion, and record the path as trusted for Claude Code. |
| `mcp_servers.py` | Installs Linear and GitHub MCP into the machine's user-level Claude Code config. |
| `skills/` | The packaged `foregent-worker` skill and its installer. |
| `cli.py` | `status`, `queue`, `setup`, `serve`. A thin HTTP client of the bridge, except `setup`. |
| `config.py` | Environment-overridable settings. |

## 4. Flows

### 4.1 Dispatch

`foregent queue JIM-42 --directory <path>` records the issue as Queued, then:

1. **Capacity.** One concurrent agent. An In Progress or parked Blocked issue
   holds the slot; anything else waits.
2. **Skills.** Any packaged skill the machine lacks is written first. Claude
   Code picks up live edits to a skill directory, but only one that existed
   when the session started, so this must finish before launch.
3. **Claim.** Assignee and In Progress are set in Linear in one step. Nothing
   is dispatched without a durable ownership record.
4. **Workspace.** A fresh jj workspace is built from the queued repo, named
   for the issue key (§6.5). Before the launch, because it is the agent's
   cwd.
5. **Launch.** A herdr workspace opens at that directory and Claude Code
   starts in it, with a conversation id foregent generates rather than
   scrapes.
6. **Brief.** The agent is prompted with `/foregent-worker JIM-42`, so the
   lifecycle has one definition — the skill.

Dispatch is not atomic. The deterministic agent label `fg-jim-42` is what
makes that survivable: a retry after a failed brief adopts the running agent
instead of starting a second one for the same issue.

### 4.2 Delivery

Linear posts to `POST /webhooks/linear`. The body is authenticated by
HMAC-SHA256 against `LINEAR_WEBHOOK_SECRET` and mapped to an `Event`. A
periodic tick also asks Linear what changed on tracked issues, cursored on
the last comment served; it feeds the same queue and is being removed.

1. **Match.** `wakes(event, viewer)` returns the issue key, or nothing. A
   lookup, not a scan — the event names its own issue.
2. **Drop foregent's own writes.** Claiming an issue, and every comment an
   agent posts through the Linear MCP, come back under foregent's account.
   They are dropped by actor identity. A wake that causes a write is a loop.
3. **Enqueue and answer.** The route checks in memory that there is an agent,
   enqueues, and returns. It never waits on an agent: Linear retries any
   delivery the bridge is slow to answer.
4. **Drain.** One daemon thread sends one message at a time, in the order
   written. Nothing is coalesced — merging two people's comments into one
   prompt loses who said what.
5. **Send, then unblock.** The prompt is submitted straight away, whatever
   the agent is doing, and offered again until it lands or the harness
   reports the agent gone. Unblocking happens only after the send succeeds,
   so a failure leaves the issue Blocked with nothing to roll back.

A live agent is reachable whether or not it is parked, and is prompted the
same way either way. Its status decides only what happens after the send: a
parked agent is unblocked, a working one is left as it was.

Delivery is ungated (`when_idle=False`), which is the whole of a worker
seeing activity on its own issue as it happens: the harness queues a prompt
behind the turn in progress, so a working agent reads it at the end of that
turn. Waiting for an idle agent first — what `send` does by default, and what
dispatch's brief wants — reaches a worker only if it ever falls idle, and one
whose turn ends in `complete_task` never does (JIM-144).

Response codes carry meaning. A delivery foregent does nothing with is
answered 200 — most of what Linear sends concerns issues no agent here is
working, and a failure code buys three pointless retries. A delivery arriving
before foregent knows its own account id is answered 503, the one delivery
not accepted, because matching without that id wakes an agent with its own
comment.

### 4.3 Completion and blocking

The agent calls one of two MCP tools the bridge serves at `/mcp`:

- **`report_blocked(issue_key, blocker)`** records the note and marks the
  issue Blocked. Nothing is terminated and capacity does not change.
- **`complete_task(issue_key)`** marks the issue Done, dispatches the next
  queued issue, stops the calling agent, and removes its jj workspace — in
  that order, because removing a live agent's own cwd is worse than leaking a
  directory. Neither teardown can fail the tool, since the issue is Done
  either way. They are not equally quiet: the agent stop is best-effort, while
  a workspace that cannot be removed is logged as an error and reported in the
  tool's result, because forgetting it is what exports the agent's work to git
  (§6.5).

The tools are mounted in the bridge's own process, so they mutate the store
directly instead of looping back over HTTP.

**The bridge writes to Linear once per issue: the claim.** Status, comments
and the close-out are the agent's own, through the Linear MCP. The bridge
reads Linear and reacts to it; it does not narrate the work.

### 4.4 Boot

The bridge logs the herdr session it resolved, refuses to start on a protocol
mismatch, warns if the machine's MCP servers or their credentials are absent,
rebuilds the issue store, then starts three daemon threads: the harness-event
watcher, the delivery drainer, and the poll tick.

## 5. State

### 5.1 Issue lifecycle

`Todo → Queued → In Progress → (Blocked ⇄ In Progress) → In Review → Done`,
with `Orphaned` for an in-flight issue whose agent is gone.

In Progress, In Review and Blocked are the in-flight set: each means a live
agent holds the capacity slot. `Queued` and `Orphaned` are foregent's own;
the rest mirror Linear states.

### 5.2 Capacity

One concurrent agent, hardcoded. A parked agent holds the slot for the whole
block. Three simplifications rest on this and become real work when it
changes: one delivery queue for the fleet, no workspace pool — workspaces are
built per dispatch rather than acquired from one (§6.5) — and no per-agent
ordering.

### 5.3 The agent binding

Each agent is launched with the herdr agent name `fg-<issue-key-lowercased>`,
in a workspace labeled with the uppercase key. The name is the binding: it is
unique among live agents and the issue key parses back out of it, so nothing
about a running agent needs persisting to find it again.

### 5.4 What a restart recovers

One `agent.list` against herdr rebuilds the issue-to-agent map from the
labels, finding every live agent including parked ones.

It recovers no more than that. Every agent returns as In Progress, because
the label does not record that it was blocked, and titles, blockers and
conversation ids are lost. So is the repo each workspace was built from: the
agent's cwd comes back, but not what it was made from, so a restarted bridge
cannot tear that workspace down (§6.5).

Unbuilt: orphan reconciliation — querying Linear on boot for owned in-flight
issues, moving the ones whose agents are gone to Orphaned, and re-dispatching
an orphan by resuming its conversation. Until it lands, a reboot costs a
fresh dispatch.

## 6. The agent contract

### 6.1 The launch spec

`LaunchSpec` is the structure foregent owns; the manager renders it to
`claude` flags. It carries the label, cwd, environment, model and effort, an
appended system prompt, tool allow and deny lists, MCP servers, a
conversation id, and whether to resume it.

`--permission-mode bypassPermissions` is not a spec field. It is a property
of how foregent runs agents at all (§1.1), so the manager always sets it.

### 6.2 The workflow lives in a skill

`foregent-worker` tells the agent its lifecycle: reading its assignment, the
mode rules, when to report blocked, when to call `complete_task`, and the
rebase requirement. The brief is one line, so the lifecycle has one
definition.

Skills ship inside the installed package, so they travel with a
`uv tool install`. Two paths put them on disk, both through
`skills/__init__.py`: `foregent setup` installs and updates every packaged
skill, and the server fills any gap before a launch. Each file is staged in
its destination directory and renamed into place, so a concurrent dispatch
never loads a half-written `SKILL.md`.

### 6.3 MCP servers are split by lifetime

**Foregent's own server is per-run.** Its URL is this bridge's, so the launch
spec declares it with `--mcp-config`.

**Linear and GitHub are per-machine.** `foregent setup` writes them at Claude
Code's *user* scope — the only scope that applies in a fresh workspace — and
every agent inherits them. They go in through `claude mcp add-json -s user`
rather than by editing the config file, because every running session
rewrites that file.

`--strict-mcp-config` stays off, so one configuration serves agents and the
operator's own sessions alike. The machine is already the isolation boundary
(§1.1).

Credentials never reach disk: the stored header is the literal
`${LINEAR_API_KEY}` or `${GITHUB_TOKEN}`, expanded per session from the herdr
server's environment.

### 6.4 Project modes

- **bootstrap** — no GitHub surface. The agent rebases onto `main` and
  fast-forwards `main` locally. This is how foregent develops itself.
- **full** — the agent pushes a branch and opens a pull request through the
  GitHub MCP. Unbuilt.

Rebase, never merge: bootstrap mode must produce history clean enough to
graduate a repository to full mode.

### 6.5 Workspaces

Each agent works in its own **jj workspace**, named for the issue key and
built from the repo the issue was queued against. `foregent queue -d` names
that repo; the workspace is the agent's cwd. The root is
`FOREGENT_WORKSPACE_ROOT`, `~/.foregent/workspaces` by default, deliberately
outside any repo — a workspace is disposable and has no business living inside
the checkout it was made from.

The bridge owns the lifecycle. It creates the workspace before launch and
removes it on `complete_task`; neither the agent nor the harness is trusted
with the job, because an agent that dies mid-issue leaks a workspace nobody
owns. One fresh workspace per dispatch is the point: no agent inherits the
previous one's dirty working copy, which is worth more today than parallelism.

Creation is `jj workspace forget <key>`, which tolerates absence, then
`jj workspace add`. Forgetting first is the whole leak story — a crashed
agent's stale workspace is reclaimed by the next dispatch that wants the name,
so there is no reaper and no registry to keep honest. A repo that is not a jj
repo is used as the cwd as it stands, so a project foregent cannot isolate
still gets its agent.

Three behaviors of jj shape this, all established by driving jj 0.43 directly:

- **`add` needs an explicit revision.** Given none, jj gives the new working
  copy the parents of the *current* workspace's working-copy commit, so the
  agent would start from whatever commit the operator's own checkout was
  sitting on. Foregent passes `main` — the branch bootstrap mode and the
  worker skill already name (§6.4), so a second name for it would only be a
  third place to disagree.
- **A secondary workspace has no `.git`.** Raw `git`, `gh`, and the harness's
  git integration are blind inside one. The write paths do not need them:
  `jj git push` reaches the shared git backend, and GitHub mode opens its pull
  request through the GitHub MCP. What degrades is agent-side git
  convenience — a quality cost, not a correctness one.
- **A bookmark moved inside one is invisible to git** until a mutating jj
  command runs at the colocated root. Bootstrap mode advances `main` from
  inside the workspace, so **teardown is what publishes the work to git**, and
  `forget` runs even when the workspace directory is already gone. This is why
  a failed teardown is reported rather than passed over (§4.3).

Unbuilt: teardown after a restart. The agent's cwd is recovered from herdr but
the repo it was made from is not (§5.4), so a workspace whose bridge restarted
is left for the next dispatch of that key to reclaim.

The pool is deliberately absent. Capacity is one concurrent agent (§5.2), so N
slots with issue-keyed acquisition would be structure for a number that never
changes; it arrives with capacity.

## 7. The AgentManager seam

Foregent owns what an agent is for. The manager owns how a harness is driven.

```python
class AgentManager(Protocol):
    def launch(self, spec: LaunchSpec) -> AgentRef
    def send(self, ref, text, *, when_idle: bool = True) -> None
    def status(self, ref) -> AgentStatus
    def wait(self, ref, until: Collection[AgentStatus], timeout: float) -> AgentStatus
    def read(self, ref, lines: int) -> str
    def stop(self, ref) -> None
    def list_agents(self) -> list[AgentRecord]
    def events(self) -> Iterator[AgentEvent]
```

Calls are synchronous and may block for as long as an agent takes; the API
server runs them in a threadpool. Every harness failure surfaces as one
`AgentError`, so the bridge never catches one runtime's socket errors and
another's HTTP errors.

`AgentStatus` is `IDLE | WORKING | BLOCKED | DONE | UNKNOWN | GONE`. `GONE`
is explicit rather than inferred, and `events()` may be a polling loop, so a
harness with weaker eventing still fits.

### 7.1 Harness behavior the implementation depends on

Established by driving herdr and Claude Code directly. Each is load-bearing
and none is obvious from either tool's documentation.

- **`idle` is not "ready for input".** An agent reads as idle seconds before
  its TUI accepts a prompt, and prompting early is refused. `launch()` polls
  for `interactive_ready` rather than sleeping a guessed amount.
- **Every prompt carries a `wait` block.** herdr runs its delivery check only
  for prompts sent with one. A bare prompt reports success even when a modal
  swallowed the text, leaving the message unsent while the agent still reads
  as idle. Neither the screen nor the state counter distinguishes that case.
- **A stall is not a timeout.** A stall means the agent never saw the
  message, so a resend cannot double up. A timeout means it reacted but did
  not reach the watched state, and counts as delivered.
- **A working agent takes a prompt.** herdr accepts it and the agent reads it
  when its turn ends; only a `blocked` agent is refused outright. The
  delivery check comes with the caveat that herdr runs it only when the
  submission starts from a non-working state, so a prompt to a working agent
  is accepted on herdr's word alone.
- **Scrollback needs an idle agent.** `agent.read` captures history by
  scrolling the pane, which herdr refuses (`agent_not_idle`) while an agent
  is working; only the visible screen can be read then. Every read is for a
  human to look at, and the ones that matter most quote a failure against a
  working agent, so the manager falls back rather than propagating.
- **Status is a per-pane subscription.** The global `pane.updated` carries an
  `agent_status` that lags, reporting an agent idle while it works. A quiet
  subscription re-checks the fleet periodically, because an agent started
  since the subscription opened is invisible until re-subscribed.
- **Stopping an agent emits only `workspace_closed`.** Closing a workspace
  kills its panes with no pane event, so watching pane events alone misses
  every deliberate teardown.
- **Workspace trust is inherited, not matched.** Claude Code opens its trust
  dialog in a directory it has not seen, and herdr reads that dialog as
  `blocked`, so an untrusted cwd fails a dispatch outright (§8.3). The check
  is not an exact path match: it walks up from the directory testing each
  ancestor, so trusting a workspace root once covers every per-issue workspace
  under it. Read off build 2.1.251 and documented nowhere, so foregent treats
  it as an optimization rather than a guarantee — it writes the exact entry
  whenever its own copy of the rule says untrusted, which is the answer a
  stricter harness would give.
- **Detection is screen-scraping underneath.** herdr's detection manifest
  updates on its own schedule, independent of the protocol version, so the
  startup protocol check says nothing about it.

## 8. Deployment

### 8.1 Processes

Per box: the herdr server as a systemd user unit, the foregent bridge beside
it, and `cloudflared` providing the HTTPS ingress that fronts the webhook
endpoint. Linger is enabled so the units survive logout.

### 8.2 Which herdr session

Resolved, not compiled in: `FOREGENT_HERDR_SESSION` first, then the session
the bridge process runs in (herdr injects `HERDR_SOCKET_PATH` into every pane
it owns), then herdr's default session.

The order keeps deployment deterministic. The systemd unit runs outside any
pane, so it must set the variable or land in the default session — the
operator's interactive one — instead of the dedicated session that exists to
be attached to read-only. A development box sets nothing and reaches whatever
session its shell lives in. An inherited socket path that is dead fails
loudly rather than falling back.

Observe by attaching: `herdr --session foregent` over SSH, or
`herdr --remote <ssh-target> --session foregent` from a laptop. Read-only by
convention. For inspection without attaching, herdr offers `agent.list`,
`agent.get`, `agent.read` and `agent.explain`.

### 8.3 Provisioning steps that block dispatch

Each of these, if missed, breaks dispatch on a box that looks correctly
installed.

- **A clean environment for the herdr server.** Every pane inherits it. A
  server started from inside another Claude Code session leaks `CLAUDECODE=1`
  into its agents, which silently disables transcript saving and breaks
  resume. The systemd unit sets an explicit environment.
- **Pre-accepted workspace trust.** A fresh directory makes Claude Code open
  its trust dialog before accepting input, and herdr's detection reads that
  dialog as `blocked` — so the agent never reaches idle and the launch fails.
  Every workspace is a fresh directory, so trust the workspace *root* once and
  every workspace under it inherits it (§7.1). Foregent writes the entry
  itself for any workspace it finds untrusted, so this is a should, not a
  must; doing it by hand keeps foregent out of `~/.claude.json`, which every
  running Claude Code session rewrites.
- **The herdr Claude integration** (`herdr integration install claude`), so
  session identity is reported back to herdr.
- **`LINEAR_API_KEY` and `GITHUB_TOKEN` in the herdr server's environment.**
  The MCP configuration stores the variable name, not the token, so a server
  missing the variable looks installed and fails to authenticate once an
  agent is already working.
- **An HTTPS ingress and `LINEAR_WEBHOOK_SECRET`.** A delivery blocker rather
  than a dispatch blocker: an agent that parks on a box with no ingress never
  wakes.

### 8.4 Environment

| Variable | Read by | Purpose |
|---|---|---|
| `FOREGENT_API_URL` | CLI, agents | Where the bridge is. Default `http://127.0.0.1:8577`. |
| `FOREGENT_HERDR_SESSION` | bridge | Which herdr session agents run in. |
| `FOREGENT_POLL_INTERVAL` | bridge | Seconds between poll ticks. |
| `FOREGENT_WORKSPACE_ROOT` | bridge | Where per-issue workspaces are built. Default `~/.foregent/workspaces`. |
| `LINEAR_API_KEY` | bridge, agents | Linear API and MCP authentication. |
| `LINEAR_WEBHOOK_SECRET` | bridge | Webhook signature verification. |
| `GITHUB_TOKEN` | agents | GitHub MCP authentication. |
