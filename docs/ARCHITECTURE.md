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
  │  • delivery queues + drainers, one per issue    │
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
| `server.py` | The bridge: HTTP routes, the webhook endpoint, dispatch, the per-issue delivery queues and their drainers, the harness-event watcher, the mounted MCP server. |
| `store.py` | `IssueStore`, the in-memory issue map, and what counts as in-flight. |
| `models.py` | `Issue` and `IssueStatus`. |
| `events.py` | `Event`, `EventKind`, and the pure `wakes()` and `delivery_message()`. No transport, no server. |
| `linear.py` | Linear GraphQL client: claim an issue, resolve foregent's account, authenticate a webhook, map a payload to an `Event`. |
| `github.py` | The inbound half of GitHub: authenticate a webhook delivery, map a payload to an `Event`, and read the issue key out of a branch name. Agents reach GitHub through the MCP server, so the bridge needs no API client. |
| `herdr.py` | The herdr socket client: newline-delimited JSON, session resolution, protocol check. |
| `agents/base.py` | The `AgentManager` protocol and its types. Harness-agnostic. |
| `agents/herdr_claude.py` | The one implementation: renders a `LaunchSpec` to `claude` flags, drives `workspace.create` → `agent.start` → `agent.prompt`, translates herdr's events. |
| `workspaces.py` | Per-issue jj workspaces: create at dispatch, carry the `.worktreeinclude` files in, remove at completion, and record the path as trusted for Claude Code. |
| `mcp_servers.py` | Installs Linear and GitHub MCP into the machine's user-level Claude Code config. |
| `skills/` | The packaged `foregent-worker` skill and its installer. |
| `cli.py` | `status`, `queue`, `setup`, `serve`. A thin HTTP client of the bridge, except `setup`. |
| `config.py` | Environment-overridable settings. |

## 4. Flows

### 4.1 Dispatch

`foregent queue JIM-42 --directory <path>` records the issue as Queued, then:

1. **Capacity.** Whether there is room for this issue (§5.2). One agent at a
   time in bootstrap mode, up to `FOREGENT_MAX_AGENTS` in pull request mode.
   Every in-flight issue holds a slot; anything else waits.
2. **Skills.** Any packaged skill the machine lacks is written first. Claude
   Code picks up live edits to a skill directory, but only one that existed
   when the session started, so this must finish before launch.
3. **Claim.** Assignee and In Progress are set in Linear in one step. Nothing
   is dispatched without a durable ownership record.
4. **Workspace.** A fresh jj workspace is built from the queued repo, named
   for the issue key, and the repo's `.worktreeinclude` files are copied into
   it (§6.5). Before the launch, because it is the agent's cwd.
5. **Launch.** A herdr workspace opens at that directory and Claude Code
   starts in it, with a conversation id foregent generates rather than
   scrapes.
6. **Brief.** The agent is prompted with `/foregent-worker JIM-42 bootstrap`
   or `… pull-request`, so the lifecycle has one definition — the skill — and
   the mode is told to the agent rather than looked up by it (§6.4).

One call launches until the queue is empty or the next issue does not fit, so
a completion can start more than one agent where the queue has been waiting on
capacity. The queue is strictly FIFO: an issue that does not fit stalls the
ones behind it rather than being skipped, which keeps queue order from becoming
a scheduling policy with a starvation question attached.

Dispatch is not atomic. The deterministic agent label `fg-jim-42` is what
makes that survivable: a retry after a failed brief adopts the running agent
instead of starting a second one for the same issue. **A lock holds the whole
of one dispatch**, because the capacity check and the write that satisfies it
are several harness calls apart: two callers arriving together would both read
a store neither has written yet and launch the same issue twice. Launches are
therefore serial even when several slots are free.

### 4.2 Delivery

Linear posts to `POST /webhooks/linear`. The body is authenticated by
HMAC-SHA256 against `LINEAR_WEBHOOK_SECRET` and mapped to an `Event`. Push is
the whole of foregent's inbound path: nothing asks Linear what changed.

When an authentic delivery last arrived is recorded and served from
`GET /health`, which `foregent status` prints above its table. Push is the
only thing that wakes an agent, so a hook that has stopped delivering leaves a
fleet that looks merely idle; the timestamp is what tells the two apart.

A signature proves Linear sent these bytes, not that it sent them just now,
so the delivery's own `webhookTimestamp` is checked against a one-minute
window beside it and a delivery from outside that window is refused 400. Both
halves are needed: without the second, a captured delivery replays at the
bridge forever.

1. **Drop a repeat.** Linear retries a delivery it believes failed, and a
   retried comment must not prompt a worker twice. The signatures of the last
   few hundred deliveries are held, newest last; one already there is answered
   200 and goes no further. The signature is the key because the payload
   carries no per-delivery id — `webhookId` names the webhook — while the
   signature is a digest of the exact bytes sent.
2. **Match.** `wakes(event, viewer)` returns the issue key, or nothing. A
   lookup, not a scan — the event names its own issue.
3. **Drop foregent's own writes.** Claiming an issue, and every comment an
   agent posts through the Linear MCP, come back under foregent's account.
   They are dropped by actor identity. A wake that causes a write is a loop.
4. **Enqueue and answer.** The route checks in memory that there is an agent,
   enqueues, and returns. It never waits on an agent: Linear retries any
   delivery the bridge is slow to answer.
5. **Drain.** Each issue has its own queue and its own daemon thread, started
   on the first delivery to it, so one agent's messages reach it one at a time
   in the order written and no agent waits behind another. A send is offered
   again until it lands or the agent is gone, so a fleet-wide queue would let
   one unreachable agent silence the rest. Nothing is coalesced — merging two
   people's comments into one prompt loses who said what. A drainer ends when
   its issue completes or its agent dies.
6. **Send, then unblock.** The prompt is submitted straight away, whatever
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

GitHub posts to `POST /webhooks/github`, the second inbound path, for what
happens to the pull requests agents open in Pull Request mode. Authentication
is the same shape — HMAC-SHA256 over the exact bytes received, against
`GITHUB_WEBHOOK_SECRET`, compared against the `sha256=` prefixed digest GitHub
sends in `X-Hub-Signature-256` — and so are the answers: 401 for a signature
that does not prove the delivery, 503 when the bridge holds no secret, 400 for
a body that is not a JSON object, 200 for everything else. Only the header
`X-GitHub-Event` says what a delivery is about; the body names the repository
and the pull request.

A review being submitted and an inline review comment being written map to a
`PR_REVIEW` event; every other event and every other action of those two maps
to nothing, an organization webhook carrying far more than foregent has a use
for. From there the path is the Linear one, joined at `queue_event`: match,
enqueue, drain, send. The two guards ahead of that join stay Linear's own —
both key on what Linear signs and stamps — so a GitHub delivery is checked
against no freshness window, and a retry of one GitHub believes failed reaches
the agent a second time.

**The pull request is resolved back to its issue through its head branch.**
Linear names an agent's branch after the issue and links a pull request opened
from it to that issue, so the key is in the branch and reading it there is the
whole of following the link — no GitHub call, and no pull request number a
worker has to report to be findable.

**Foregent's own writes are dropped by comparing the delivery's sender to the
pull request's author.** The agent opened the pull request, so a review comment
it writes there comes back as an event about its own issue, and a wake that
causes a write is a loop. That is what `viewer` does on the Linear side, except
that the payload names both sides of this comparison, so a GitHub delivery is
matched without an account id and without a Linear call. The cost is that a
person who opens a pull request by hand does not wake the agent by commenting
on it themselves; anyone else reviewing it does.

Two things a worker could be told about are not delivered. A comment in the
pull request's conversation tab arrives as `issue_comment`, whose payload
carries no head branch, so resolving it would need the GitHub API client the
bridge does not have. And GitHub sends nothing when a pull request stops
merging cleanly, so `PR_CONFLICT` has no source and stays unused.

### 4.3 Completion and blocking

The agent calls one of two MCP tools the bridge serves at `/mcp`:

- **`report_blocked(issue_key, blocker)`** records the note and marks the
  issue Blocked. Nothing is terminated and capacity does not change.
- **`complete_task(issue_key)`** advances `main` onto the issue's work in
  bootstrap mode, marks the issue Done, dispatches the next queued issue,
  stops the calling agent, and removes its jj workspace — in that order.

  **Advancing comes first because the next dispatch builds its workspace on
  `main`**: move the bookmark after that, and the next agent starts from a
  trunk this issue never reached. It also has to precede the teardown, since
  the revision it names lives in the workspace being removed. Pull Request
  mode skips it — the agent has pushed its own branch, and `main` is the
  reviewer's to move.

  Neither teardown can fail the tool, since the issue is Done either way;
  removing a live agent's own cwd is worse than leaking a directory, so the
  stop precedes the removal. The agent stop is quiet and best-effort, while a
  workspace that cannot be removed is logged and reported, because nobody owns
  the leftovers.

  **Both halves run on one thread, in a single call, and that is what makes
  the removal happen at all.** Stopping the agent severs the connection it
  called the tool over, and the bridge serves MCP statelessly, so the request
  handler is cancelled the moment its client disconnects; a thread already
  running is not interrupted, and it is what carries the removal past the
  cancellation (JIM-150).

  **A refusal to advance is the one thing that stops the completion**, before
  anything else has happened. jj declines to move `main` onto work that is not
  descended from it (§6.5), which means an agent that never rebased: its
  commits exist only in the workspace, so tearing that workspace down would
  take them with it. The issue stays in flight, the workspace stays on disk,
  and the tool says so.

The tools are mounted in the bridge's own process, so they mutate the store
directly instead of looping back over HTTP.

**The bridge writes to Linear once per issue: the claim.** Status, comments
and the close-out are the agent's own, through the Linear MCP. The bridge
reads Linear and reacts to it; it does not narrate the work.

### 4.4 Boot

The bridge logs the herdr session it resolved, refuses to start on a protocol
mismatch, warns if the machine's MCP servers or their credentials are absent,
rebuilds the issue store, then starts two daemon threads: the harness-event
watcher and the delivery drainer.

## 5. State

### 5.1 Issue lifecycle

`Todo → Queued → In Progress → (Blocked ⇄ In Progress) → In Review → Done`,
with `Orphaned` for an in-flight issue whose agent is gone.

In Progress, In Review and Blocked are the in-flight set: each means a live
agent holds the capacity slot. `Queued` and `Orphaned` are foregent's own;
the rest mirror Linear states.

### 5.2 Capacity

**How many agents run at once is the project's mode, not a number.**

- **Bootstrap: one at a time**, and the repo rather than policy is what says
  so. A workspace is built at `main` and completion fast-forwards `main` onto
  the agent's tip (§4.3), so two bootstrap agents branch from the same commit
  and the second cannot land what it wrote. Advancing before the next dispatch
  is precisely what gives each agent a base holding the last one's work.
- **Pull request: up to `FOREGENT_MAX_AGENTS`** (default 3). The agent pushes
  its own branch and `main` is the reviewer's to move, so nothing in the repo
  serialises it and the limit is only what one box and one reviewer can carry.

The limit is set by the mode of the issue at the head of the queue, and every
in-flight issue counts against it. A queued bootstrap issue therefore waits
behind agents on any repo — over-strict only on a box hosting two projects,
which §1.1 rules out, and the safe answer everywhere else. The mode is derived
per call from the repo (§6.4), so it needs nothing persisted to survive a
restart; an issue whose repo a restart could not recover reads bootstrap, the
serial answer.

**A parked agent holds its slot for the whole block** (§1.7). In pull request
mode the steady state is therefore N agents all waiting on review, so
`FOREGENT_MAX_AGENTS` is in practice the number of pull requests that may be
open at once, and throughput is bounded by review latency rather than by the
box.

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
conversation ids are lost.

The repo each workspace was built from is not lost with them, because it is
not recovered from the label at all: a secondary workspace's `.jj/repo` names
the repo it belongs to, so the agent's cwd is read for it (§6.5). Teardown
needs that repo to forget the workspace, and a restart between dispatch and
completion is the ordinary case rather than the exception — the operator
merges an agent's pull request and restarts the bridge on the change.

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
definition. Its second word is the mode (§6.4), which is the one thing about
the lifecycle the skill cannot work out for itself.

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

- **bootstrap** — no GitHub surface. The agent rebases onto `main` and commits
  there; the bridge moves the bookmark when the issue completes (§4.3).
- **pull request** — the agent pushes a branch and opens a pull request
  through the GitHub MCP, then reports blocked on the review. This is how
  foregent develops itself.

**A project's mode is derived, not declared.** `jj git remote list` decides it
at dispatch: an `origin` remote on GitHub is where a pull request can be
opened, and everything else — no remotes, an origin hosted elsewhere, a
directory that is not a jj repo — is bootstrap, which needs nothing. The
answer travels to the agent in the brief (§4.1), and the same call answers
again at completion, because it is a pure function of the repo and a stored
copy would be one more thing a restart cannot recover (§5.4).

Derived rather than declared because the alternative is two places to
disagree. A file saying `pull request` in a repository with no GitHub remote
describes a mode nobody can land work in.

Rebase, never merge: bootstrap mode must produce history clean enough to
graduate a repository to pull request mode.

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
  `jj git push` reaches the shared git backend, and Pull Request mode opens
  its pull request through the GitHub MCP. What degrades is agent-side git
  convenience — a quality cost, not a correctness one.
- **A bookmark moved inside one is invisible to git** until a mutating jj
  command runs at the colocated root, and a workspace's working copy is
  reachable from that root as the revset `<name>@`. Together they are why
  bootstrap mode lands its work with `jj bookmark move main --to <KEY>@-` run
  at the repo root, rather than from inside the workspace where git would not
  see it. `@-` and not `@`: the working-copy commit is jj's scratch space, and
  publishing it would put an empty commit at the head of `main`. An agent that
  committed nothing leaves `@-` on `main`, which jj answers with "No bookmarks
  to update" and a zero exit.

  **`bookmark move` is fast-forward-only** without `--allow-backwards`, so jj
  refuses work that is not descended from `main` and leaves the bookmark where
  it was. The rebase requirement the worker skill states is enforced by jj for
  free, with no ancestry revset of foregent's own to get wrong (§4.3).
- **A secondary workspace names the repo it belongs to.** Its `.jj/repo` is a
  file holding the path of the shared repo directory, so the repo a teardown
  has to run `forget` in is read back out of the agent's cwd instead of
  remembered. That is what makes a restart between dispatch and completion
  survivable (§5.4). A repo's own root answers nothing — there `.jj/repo` is a
  directory — so nothing mistakes a project for a disposable workspace.

A fresh workspace holds only what version control tracks, so the untracked
files a project needs to run — `.env`, a local settings file, a key — are not
in it. Foregent carries them over from a **`.worktreeinclude`** manifest at the
repo root, [Claude Code's own
convention](https://code.claude.com/docs/en/worktrees#copy-gitignored-files-into-worktrees)
for naming them, so a project that already feeds `claude --worktree` gets the
same set here with nothing to configure twice. The file is `.gitignore` syntax,
and a path is carried over when it **matches the manifest and is itself
ignored** — the convention's own rule, and what stops a tracked file becoming
an untracked copy of itself in the workspace.

Both halves are answered by `git ls-files --others --ignored`, once against the
manifest and once against the standard excludes, and the two sets intersected.
Handing the patterns to git rather than matching them in foregent is what makes
the syntax git's own down to the corners, and it costs no dependency. Three
consequences follow from that choice:

- **A symlink is one entry and is never followed**, a directory symlink
  included, so the workspace gets a link rather than a recursive copy of
  everything behind it. A relative target is made absolute against the source's
  directory before the new link is written: a workspace lives under
  `FOREGENT_WORKSPACE_ROOT`, nowhere near the repo, so the link text cannot
  travel unchanged without dangling.
- **A repo that is not colocated with git copies nothing**, logged and not
  raised. The manifest is a convenience; a project that cannot use it must
  still dispatch.
- **A listed file that cannot be copied fails the dispatch.** Launching an
  agent quietly missing the credentials the operator asked to be there is the
  worse failure, and the only one nobody would notice.

The pool is deliberately absent, and concurrency did not bring one. Workspaces
are keyed by issue and built per dispatch, so parallel agents want no shared
resource to acquire; a pool would be structure with nothing to allocate.

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
| `FOREGENT_WORKSPACE_ROOT` | bridge | Where per-issue workspaces are built. Default `~/.foregent/workspaces`. |
| `FOREGENT_MAX_AGENTS` | bridge | Agents at once in pull request mode. Default 3; bootstrap is always one. |
| `FOREGENT_LOG_LEVEL` | CLI | Default of `serve --log-level`. Default `info`. |
| `LINEAR_API_KEY` | bridge, agents | Linear API and MCP authentication. |
| `LINEAR_WEBHOOK_SECRET` | bridge | Webhook signature verification. |
| `GITHUB_TOKEN` | agents | GitHub MCP authentication. |
| `GITHUB_WEBHOOK_SECRET` | bridge | GitHub webhook signature verification. |
