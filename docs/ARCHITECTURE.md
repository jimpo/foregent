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
- **Nothing on the box is irreplaceable.** Repositories are clones, and the
  one file the bridge keeps is reconciled against the live agents at boot and
  can be deleted (§5.4). The box is rebuilt, not repaired.
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

### 1.3 A harness is a choice, not a foundation

Claude Code and Codex both run agents here, and neither is built into
anything above the seam. Everything harness-specific — the argv, the status
mapping, the socket calls — sits behind `AgentManager` (§7). Nothing above
that seam knows what an agent is running.

**Which harness runs an agent is a `Provider`**, carried on the launch spec
and reported back on every live agent. It is not derived the way a project's
mode is (§6.4): which harness works an issue is not a property of the
repository, so there is nothing in it to read the answer off.

**One manager serves every harness**, because herdr is what normalizes them.
herdr owns the detection manifest that reads each agent's state off its
screen, so a pane is prompted, read, waited on and closed by the same call
whatever runs in it. Only starting one differs, in the agent kind and the
argv, and both are looked up from the provider.

### 1.4 Three parties hold the state, split by what each can vouch for

**Linear holds issue truth**: ownership and status as the world sees them.
**herdr holds liveness**: which agents exist and what each is doing. **The
bridge's state file holds foregent's own intent**: queue order, which agent
was bound to which issue, what a parked agent said it was waiting for, and
whatever a scheduler decides later. Nothing else can rebuild that, because
nothing else was told.

The file claims nothing about the world. Every write to the issue store
rewrites it atomically, and a restart reads it back and then checks it
against herdr before trusting a word of it (§5.4). Deleting it puts the
bridge where it was before it had one — every live agent adopted off its
label, the queue gone — which is what keeps the box disposable (§1.1).

The alternative was to keep making the herdr agent label carry the state,
which it cannot: a label holds one issue key, so a Queued issue, with no
agent, had nothing to survive in, and every feature that needed a field had
to be designed to fit a name.

### 1.5 One agent owns one issue, and hands its sub-issues to the queue

One agent reads the issue, drives its Linear status, writes the code, and
closes it out. How it decomposes the work inside its own workspace, including
whether it spawns its own subagents, is its business.

**Sub-issues are the exception, and they go to the queue** (JIM-250, design in
JIM-198). The agent creates them in Linear, hands the keys to
`queue_sub_issues`, and each is dispatched to an agent of its own with its own
workspace and its own pull request. The parent writes no code for them: it
parks with `report_blocked` and is told one line per child that lands, so it
can queue the next wave, park again, or finish. That parked session, holding
everything the parent worked out, is the supervisor — not a scheduler
somewhere with a plan — and the queue is its executor.

The alternative was one agent doing every child itself, as one pull request
with a commit each. It runs a large feature at one-agent throughput however
many slots are free, banks nothing until the last child is written, and
produces the largest possible review, which is the worst shape for the thing
that actually bounds throughput (§5.2).

**The topology foregent keeps is one field**, `parent` on the issue record.
Nothing reads the Linear tree, in either direction: the caller is trusted to
have made the link, and the wake hangs off the stored parent rather than off
what Linear says.

### 1.6 The blocker is a note, not a key

A parked agent reports what it is waiting for in its own words. The bridge
records the text for the operator and never matches on it. Routing is by
issue: an event reaches the agent that owns the issue the event is about.

### 1.7 A blocked agent parks alive

Nothing is terminated when an agent blocks. The process stays up and idle in
its workspace, holding its context and its live slot, until the event it
needs arrives. Waking it is a prompt, not a relaunch.

**Blocking is sleep, and it gives back the run slot** (§5.2, JIM-248). Memory
and disk stay committed to the parked agent — nothing else can be launched
into them — but its CPU sits idle the whole time it waits on a review, and
that is what the run slot measures. Waking is therefore two things where
launching a fresh agent is one: the prompt, and first taking back a run slot,
which a wake gets ahead of any launch waiting on the same slot (§4.1).

**This is also what makes delegation workable** (§1.5, JIM-250). A parent
parks for as long as its children take, holding a live slot the whole time,
so a sub-issue is admitted on the run limit alone (§5.2). Charging children
the live limit their parent filled would leave every parent waiting on work
that cannot start. What the exemption does not survive is the queue's FIFO
rule, which is checked first: see the head-of-queue stall in §5.2.

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
  │ foregent bridge (Python / FastAPI)              │
  │  • event ingest, authentication, matching       │
  │  • delivery queues + drainers, one per issue    │
  │  • dispatch and capacity                        │
  │  • AgentManager (herdr + Claude Code / Codex)   │
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
      Claude Code or Codex sessions, one per issue
```

Agents talk to the world through the Linear and GitHub MCP servers, and to
the bridge through the foregent MCP server.

## 3. Modules

| Module | Responsibility |
|---|---|
| `server.py` | The bridge: HTTP routes, the webhook endpoint, dispatch, the per-issue delivery queues and their drainers, the harness-event watcher, the mounted MCP server. |
| `store.py` | `IssueStore`, the issue map and the state file it is mirrored to, and what counts as in-flight. |
| `models.py` | `Issue` and `IssueStatus`. |
| `events.py` | `Event`, `EventKind`, and the pure `wakes()` and `delivery_message()`. No transport, no server. |
| `linear.py` | Linear GraphQL client: claim an issue, close it, resolve foregent's account, authenticate a webhook, map a payload to an `Event`. |
| `github.py` | The inbound half of GitHub: authenticate a webhook delivery, map a payload to an `Event`, and read the issue key out of a branch name. Agents reach GitHub through the MCP server; the bridge's own reach is one GET, for the one delivery that names no branch. |
| `herdr.py` | The herdr socket client: newline-delimited JSON, session resolution, protocol check. |
| `agents/base.py` | The `AgentManager` protocol and its types, `Provider` among them. Harness-agnostic. |
| `agents/herdr_manager.py` | The one implementation: drives `workspace.create` → `agent.start` → `agent.prompt` and translates herdr's events, for every harness. |
| `agents/harness.py` | Which herdr agent kind a provider names, and which module renders its argv. |
| `agents/claude.py` | Claude Code's own half: the agent kind, the flags a `LaunchSpec` renders to, and the brief. |
| `agents/codex.py` | The same for Codex. |
| `workspaces.py` | Per-issue jj workspaces: create at dispatch, carry the `.worktreeinclude` files in, remove at completion, and record the path as trusted for the harness that will run there. |
| `mcp_servers.py` | Installs Linear and GitHub MCP into the machine's config, once per harness. |
| `skills/` | The packaged `foregent-worker` skill and its installer. |
| `cli.py` | `status`, `queue`, `setup`, `serve`. A thin HTTP client of the bridge, except `setup`. |
| `config.py` | Environment-overridable settings. |

## 4. Flows

### 4.1 Dispatch

`foregent queue JIM-42 --directory <path> [--provider <harness>] [--model
<name>]` records the issue as Queued against that repo, that harness and, if
one is named, that model, then:

1. **Capacity.** Whether there is room for this issue (§5.2): up to
   `FOREGENT_MAX_AGENTS` live and `FOREGENT_MAX_ACTIVE` working at once
   (JIM-248), in either mode. Every in-flight issue holds a live slot, and a
   working one holds a run slot too; a parked one gives its run slot back, and
   a launch never takes one a wake is already waiting on (§1.7). A sub-issue
   answers to the run limit alone (JIM-250).
2. **Skills.** Every packaged skill is written first, over whatever is there,
   into the skill directory of the harness this issue names. Claude Code picks
   up live edits to a skill directory, but only one that existed when the
   session started, so this must finish before launch.
3. **Claim.** Assignee and In Progress are set in Linear in one step. Nothing
   is dispatched without a durable ownership record.
4. **Workspace.** A fresh jj workspace is built from the queued repo, named
   for the issue key, and the repo's `.worktreeinclude` files are copied into
   it (§6.5). Before the launch, because it is the agent's cwd.
5. **Launch.** A herdr workspace opens at that directory and the named
   harness starts in it, with a conversation id foregent generates rather than
   scrapes — for Claude Code, which takes one; Codex records its own, which
   herdr reports back (§6.1). A named model is passed as the harness's own
   `--model`; an unnamed one is no flag at all, so the harness's default
   applies rather than one foregent guessed.
6. **Brief.** The agent is prompted with the skill, the issue and the mode —
   `/foregent-worker JIM-42 bootstrap` for Claude Code, a sentence naming the
   same three for Codex (§6.2) — so the lifecycle has one definition, the
   skill, and the mode is told to the agent rather than looked up by it
   (§6.4).

**The harness and the model are the operator's answers and the mode is the
repository's.** Which harness works an issue, and which model it runs, are not
properties of the repo, so there is nothing in it to derive them from; how
work lands there is, so `--provider` and `--model` are flags and the mode is
not (§1.3, §6.4). The model's name is not checked by foregent: each harness
has its own vocabulary of them, and the harness is what refuses one it does
not know.

One call launches until the queue is empty or the next issue does not fit, so
a completion can start more than one agent where the queue has been waiting on
capacity. The queue is strictly FIFO: an issue that does not fit stalls the
ones behind it rather than being skipped, which keeps queue order from becoming
a scheduling policy with a starvation question attached.

**The queue has a second door.** `queue_sub_issues(issue_key, keys)`, an MCP
tool a worker calls, queues each key at the back of the same queue against
that worker's own repo, harness and model, records `issue_key` as its parent,
and runs this same dispatch once. A key foregent is already running or has
already queued is refused and left where it is, so a worker that calls twice
with one wave changes nothing. What differs from an operator's `queue` is
admission (§5.2) and that the child's completion is delivered to its parent
(§4.3).

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
   on the first delivery to it, so one agent's messages reach it in the order
   written and no agent waits behind another. Each queue waits for ten seconds of quiet after its most
   recent notification, then sends the batch as one prompt with blank lines
   between the original messages (JIM-267). New arrivals reset the timer;
   notifications arriving during a send form the next batch. A send is offered
   again until it lands or the agent is gone, so a fleet-wide queue would let
   one unreachable agent silence the rest. Batching preserves every message,
   including who said what. Completion or agent death ends the quiet-period
   wait immediately; queued messages are still checked against the current
   issue state before sending. A drainer ends when its issue completes or its
   agent dies.
6. **Send, then unblock.** After the quiet period and run-slot admission, the
   batch is submitted whatever the agent is doing, and offered again until it lands or the harness
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

**Every delivery is accounted for at debug level.** One line as it arrives,
naming what the platform called it, and one where it ends: the message an agent
was handed and which agent, or the reason it went nowhere — foregent's own
write coming back, an event naming no issue, a payload the bridge does not
recognize, a push no agent is parked on. `FOREGENT_LOG_LEVEL=debug` is
therefore the whole of accounting for a webhook that reached the bridge and no
worker, and a fleet at `info` prints no more than it did.

GitHub posts to `POST /webhooks/github`, the second inbound path, for what
happens to the pull requests agents open in Pull Request mode. Authentication
is the same shape — HMAC-SHA256 over the exact bytes received, against
`GITHUB_WEBHOOK_SECRET`, compared against the `sha256=` prefixed digest GitHub
sends in `X-Hub-Signature-256` — and so are the answers: 401 for a signature
that does not prove the delivery, 503 when the bridge holds no secret, 400 for
a body that is not a JSON object, 200 for everything else. Only the header
`X-GitHub-Event` says what a delivery is about; the body names the repository
and the pull request.

A review being submitted and a comment being written — inline, or in the pull
request's conversation tab — map to a `PR_REVIEW` event, and a push that leaves
commits on `main` to a `MAIN_ADVANCED` one; every other event and every other
action of those maps to nothing, an organization webhook carrying far more than
foregent has a use for. From there the path is the Linear one, joined at
`queue_event`: match, enqueue, drain, send. The two guards ahead of that join
stay Linear's own — both key on what Linear signs and stamps — so a GitHub
delivery is checked against no freshness window, and a retry of one GitHub
believes failed reaches the agent a second time.

**The pull request is resolved back to its issue through its head branch.**
Linear names an agent's branch after the issue and links a pull request opened
from it to that issue, so the key is in the branch and reading it there is the
whole of following the link — and no pull request number a worker has to report
to be findable.

**One delivery names no branch, and is the only thing the bridge asks GitHub
for.** A comment in the pull request's conversation tab — the ordinary way a
reviewer says something that hangs off no line — arrives as `issue_comment`,
GitHub's one event for a comment on an issue and on a pull request alike, and
its payload carries the branch nowhere. The bridge fetches it: one
authenticated `GET /repos/{owner}/{name}/pulls/{number}`, read for `head.ref`,
with the `GITHUB_TOKEN` the box already holds for the agents' MCP server.
Nothing else of GitHub's API is reached and there is no client — a payload
short of one field is not a reason to own one.

The two cheap drops run ahead of the call, so it is made only for a comment
that would otherwise be delivered. The `pull_request` link inside the `issue`
is what says the comment is on a pull request rather than on a plain issue,
which foregent never opens; and that `issue`'s author is the pull request's, so
foregent's own writes are dropped by the same sender comparison as below. A
missing token or an unreachable API is logged and resolves to no issue — the
comment then reaches nobody, which is what it did before it was handled at all,
and failing the delivery would buy retries into the same wall.

Mapping a delivery can therefore block on a socket, so the route maps on a
worker thread rather than on the event loop. Resolving the branch a level up
instead would put the reading of a GitHub payload into the route, which is the
boundary `github.py` exists to hold.

**Foregent's own writes are dropped by comparing the delivery's sender to the
pull request's author.** The agent opened the pull request, so a review comment
it writes there comes back as an event about its own issue, and a wake that
causes a write is a loop. That is what `viewer` does on the Linear side, except
that the payload names both sides of this comparison, so a GitHub delivery is
matched without an account id and without a Linear call. The cost is that a
person who opens a pull request by hand does not wake the agent by commenting
on it themselves; anyone else reviewing it does.

**A push to `main` is the one delivery that is about a repository rather than
an issue.** It names no branch of foregent's, so it resolves to no issue and
matches to nobody; who it reaches is decided from the issues instead, below.
It is also the only signal there is for a pull request going stale — GitHub
sends nothing when one stops merging cleanly — so it says only that the base
moved, and leaves the agent to find out what that did to its branch. The
pushed commit subjects ride along, which is what lets an agent recognize its
own pull request landing without going to read the repository.

**Who a push reaches is decided from the issues, not from the payload.** Three
things make an issue one of them, and none of it is remembered anywhere:
Blocked, because a working agent is told to check `main` before it pushes and
does not need telling twice; Pull Request mode, because a bootstrap agent has
no pull request to go stale; and the repo that was pushed to, which an issue
names as a local path and the payload as `owner/name`, joined through the
`origin` remote. A repo whose remote will not read is woken anyway — the
failure is unreadable remotes rather than a wrong answer, and a spurious wake
costs one agent turn while a missed one leaves an agent parked forever on a
base that has moved.

**Nothing records which workers have a pull request open, deliberately.** The
rule above is the whole answer, and a record beside it would be a second
thing to keep true. A worker parked on something else is therefore woken too;
it reads one line and parks again.

**A wake un-blocks**, so a worker that handles one and is still waiting has to
report itself blocked again or no later push will reach it. The worker skill
says so, and that sentence is what the Blocked filter rests on.


### 4.3 Completion and blocking

The agent calls the MCP tools the bridge serves at `/mcp`:

- **`queue_sub_issues(issue_key, keys)`** puts the caller's sub-issues on the
  queue and dispatches (§4.1). The caller neither lands nor closes anything
  by it; it is what a delegating parent does instead of writing the code
  (§1.5).
- **`report_blocked(issue_key, blocker)`** records the note and marks the
  issue Blocked. Nothing is terminated and the live slot does not change; the
  run slot does — it is given back, and dispatched against, the same call
  (§5.2, JIM-248).
- **`complete_task(issue_key)`** advances `main` onto the issue's work in
  bootstrap mode, marks the issue Done here and in Linear, dispatches the next
  queued issue, stops the calling agent, and removes its jj workspace — in
  that order.

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
  descended from it (§6.5), which is the ordinary race between concurrent
  bootstrap agents (§5.2): another agent landed since this one last rebased.
  Its commits exist only in its workspace, so tearing that workspace down
  would take them with it. The issue stays in flight, the workspace stays on
  disk, and the tool tells the agent to rebase onto `main`, resolve any
  conflicts, and call it again.

**A completion tells the issue's parent, if it has one** (§1.5, JIM-250):
`<key> is Done` is delivered to the parent agent in whatever state it is in —
a working parent reads it as its next prompt, a parked one is woken by it. It
is on the completion route rather than the tool, so an operator closing a
child by hand wakes the parent too, and it is best-effort like the Linear
close beside it. The child's own run slot is given back after the delivery is
enqueued, which gives the parent's drainer a head start on that slot over the
dispatch that follows — a head start and not a guarantee, since nothing
orders the two, so a queued sibling may take it first and the parent waits
for the next (§5.2).

The tools are mounted in the bridge's own process, so they mutate the store
directly instead of looping back over HTTP.

**They answer to the host `FOREGENT_API_URL` names, and no other.** mcp guards
the transport against DNS rebinding by matching the `Host` header of every
request against what the mount declared, and answers a mismatch with 421. The
bridge declares the host out of that same URL, which is the one dispatch hands
each agent, so a bridge published under a name of its own stays reachable by
the agents it launched.

**The bridge writes to Linear twice per issue: the claim and the close.** They
are the two ends of the same record — the issue foregent moved to In Progress
is the issue foregent moves out of it — and the close is the bridge's because
nothing else makes it in every mode: a merged pull request closes its own
issue through Linear's GitHub integration, and bootstrap mode has no pull
request (JIM-200). An issue already in a completed or canceled state is left
as it is, so an outcome the agent decided — a bug it could not reproduce is
canceled, not done — stands. Everything else Linear shows is the agent's own,
through the Linear MCP: the bridge reads Linear and reacts to it; it does not
narrate the work.

### 4.4 Boot

The bridge logs the herdr session it resolved, refuses to start on a protocol
mismatch, warns if the machine's MCP servers or their credentials are absent,
opens the state file and reconciles it against the live agents (§5.4), starts
the harness-event watcher, then dispatches whatever came back Queued. That
last step is the boot's, because nothing else runs a dispatch until the next
queue or completion; a failure there is logged and leaves the issue Queued.

## 5. State

### 5.1 Issue lifecycle

`Todo → Queued → In Progress → (Blocked ⇄ In Progress) → In Review → Done`,
with `Orphaned` for an in-flight issue whose agent is gone.

In Progress, In Review and Blocked are the in-flight set: each means a live
agent holds a live slot. `Queued` and `Orphaned` are foregent's own; the rest
mirror Linear states. In Progress is also the only one of the three that
holds a run slot (§5.2) — the other two are a live agent not currently being
worked, whether it is under review or parked on one.

### 5.2 Capacity

**How many agents run at once is the box's answer, and it is two numbers**
(JIM-248). Capacity models the process scheduler:
`FOREGENT_MAX_AGENTS` is RAM, bounding every live process and
workspace; `FOREGENT_MAX_ACTIVE` is cores, bounding only the agents actually
being worked right now. **The two are tuned separately and ship at different
defaults on purpose** — 5 and 3 — so a box already holds more pull requests
open for review than it works at once with nothing for an operator to set;
raising `FOREGENT_MAX_AGENTS` to hold still more open does not, on its own,
also raise how many run.

**Both limits apply in both modes** (JIM-252), and every in-flight issue
counts against the live one. In pull request mode nothing in the repo
constrains them: each agent pushes its own branch and `main` is the
reviewer's to move. In bootstrap mode agents branch from the same `main`, and
the landing path is what keeps that safe (§6.5). Completion runs `jj bookmark
move main --to <KEY>@-` under a lock the bridge holds, one advance at a time,
and the move is fast-forward only: the second agent to complete is refused,
with its issue still in flight and its workspace still on disk. Workspaces
share one repo, so rebasing onto the `main` the first agent landed and
completing again is what lands the second. The bridge rebases for nobody — a
conflict needs the agent — and refuses work that carries one rather than
publishing it.

**The lock is not a formality, and jj alone is not enough.** jj is
optimistically concurrent: two `bookmark move` commands that load the same
operation both exit zero, and merging their divergent operation heads leaves
`main` *conflicted*, naming both tips with git exported to whichever won. Both
completions would then report success, both issues would close, and both
workspaces would be destroyed with one agent's work reachable from nothing.
The fast-forward refusal only answers the second agent if the second agent
reads the first one's move, which is what the lock guarantees and jj does not.

**A parked agent holds its live slot for the whole block, but gives back its
run slot** (§1.7). This is what keeps a box busy while several agents wait on
review, by default and not only when an operator raises `FOREGENT_MAX_AGENTS`
past `FOREGENT_MAX_ACTIVE` further: a fresh agent can be launched into the
run slot a parked one gave up, so throughput is bounded by what the box can
compile and test at once rather than by review latency, and only the memory
and disk of a pull request left open bounds how many may be waiting at a
time.

**A sub-issue is gated on the run limit alone** (§1.5, JIM-250). It was
queued by a worker that is about to park on it, and that parent holds a live
slot for as long as its children take, so charging them the live limit their
parent filled would leave it waiting on work that cannot start.
`FOREGENT_MAX_AGENTS` is therefore enforced on operator-queued issues only,
and the live count can exceed it by the number of agents delegation spawned.
That is the trade: nothing here bounds a parent's children, so the parent
bounds what it queues and the operator bounds how many parents run.

**The exemption is reached only at the head of the queue, and that is a real
stall** (JIM-198's accepted gap, restated because delegation sharpens it).
`admits` is asked about the oldest queued issue and nothing else: a head that
does not fit stops the loop rather than being skipped (§4.1). So an operator
issue stuck at the head with the live limit full holds every sub-issue behind
it, however many run slots are free — and since the parent of those
sub-issues is parked *on* them, neither side moves until an operator
intervenes. Skipping the head would fix it and would make queue order a
scheduling policy with a starvation question attached, which is the trade
§4.1 declines. Worth revisiting if it is ever hit in practice.

**Waking a parked agent needs a run slot back, the same as a launch does, and
it is served first** ("wake before fork"): a parked agent already holds the
memory and disk a fresh launch would need built, so finishing its work is
what actually frees the scarce resource, and it is the older work besides. A
dispatch takes a run slot only when no wake is waiting on one, so a burst of
comments across several parked agents cannot starve a review that already has
a slot coming to it. Starvation the other way is theoretical: pending wakes
are bounded by the number of parked agents, and each either lands and frees
its slot back to the pool or re-parks having changed nothing.

### 5.3 The agent binding

Each agent is launched with the herdr agent name `fg-<issue-key-lowercased>`,
in a workspace labeled with the uppercase key. The name is the binding: it is
unique among live agents and the issue key parses back out of it, so a running
agent is found again by name alone, whether or not the state file knew it.

The harness is not in the name and does not need to be: herdr reports the
agent kind it detected, and `Provider`'s values are those kinds.

### 5.4 What a restart recovers

Restarts are the ordinary case: `--dev` reloads on every source change, and
the operator restarts the bridge after merging a pull request. A restart
loses nothing the store held, because the store is a file.

**The write path is one point and one file.** Every `IssueStore` mutation
ends in `add()`, which serializes the whole store under the store's own lock,
writes a sibling temporary file, fsyncs it and renames it over the old one.
The next boot therefore reads either the previous state or this one, never a
torn file. The file is `FOREGENT_STATE_FILE`,
`~/.local/state/foregent/state.json` by default, and carries a `version`
beside the issues; the issues are a list, so queue order is file order. The
version is bumped whenever a record gains a field, since the reader is strict
about the ones it knows. A
save that fails is logged at error level and the store in memory carries on:
the file is what the *next* run reads, and failing the completion or the
webhook that caused the write would trade a stale snapshot for a stuck agent.

**The boot path reads, then reconciles.** A missing file is the first boot; a
file that will not parse, or names a version this code does not write, is
logged and read as empty rather than half-read. Either way the bridge is
where it stood before it had a file, and the reconciliation below rebuilds it
from the live agents alone; what a version bump actually costs is the queue.
There is no migration, on the grounds that starting from the live agents is
already the answer for a file that is not there. Then one `agent.list`
against herdr, and each stored
in-flight issue is checked against it:

- **Its agent is gone → Orphaned.** The bridge was down when the agent
  exited, so nobody freed the slot; this does. Deciding what happens next —
  re-dispatch, defer, escalate — stays the scheduler's, as it is for an
  agent that dies while the bridge is up (§4.3).
- **Its agent is live → the stored record stands**, title, repo, parent,
  conversation id and blocker text included, with whether it is parked taken
  from herdr's
  status: an agent that is not mid-turn has finished one and is waiting, which
  in this system means it parked. A stored working issue whose agent has
  since parked carries a placeholder blocker, because the words it chose
  went to a bridge that was down. A status that says nothing — the state
  could not be read, or the agent is waiting on input rather than on the
  world — leaves the stored status as it was: the record is evidence, and the
  In Progress guess was only ever for having none.
- **A live agent the file does not know is adopted.** The issue key comes out
  of its label (§5.3), the repo out of the `.jj/repo` in its cwd (§6.5), the
  harness out of the agent kind herdr reports — a kind foregent does not
  know reads as the default, which costs nothing, since the provider decides a
  brief, a skill directory and a workspace's trust and this issue was
  dispatched already — and parked or working from its status as above. The
  title is empty and the blocker a placeholder saying it is unknown, which is
  affordable because the blocker is a note and never a key (§1.6). This is
  the whole of a boot with no file. It has no parent, so a sub-issue adopted
  this way lands without telling the issue that delegated it (§4.3) — the
  parent is parked and reachable, so an operator's comment is the repair.

Queued, Done and Orphaned issues have no agent to check and come back as
written. Getting the parked ones right matters beyond the operator's table: a
push to `main` wakes the issues that are Blocked (§4.2), and in Pull Request
mode the steady state is a fleet of agents all waiting on review (§5.2), so a
restart that returned them all as working left that wake with nobody to find.

A herdr that cannot be reached at boot logs and leaves the store as the file
had it, rather than empty: what was written is the honest answer when the
harness cannot say otherwise, and the drainers already cope with an agent
that cannot be reached.

What the file does not hold, and stays lost across a restart: the pending
deliveries in the drainer queues, which neither webhook source re-sends. And
what is held but not yet used: the conversation id, which is what resuming an
orphan would need. Nothing re-dispatches an orphan today.

## 6. The agent contract

### 6.1 The launch spec

`LaunchSpec` is the structure foregent owns; the harness named by its
`provider` renders it to arguments. It carries the label, cwd, environment,
model and effort, an appended system prompt, tool allow and deny lists, MCP
servers, a conversation id, and whether to resume it.

Full permissions are not a spec field — `--permission-mode bypassPermissions`
for Claude Code, `--dangerously-bypass-approvals-and-sandbox` for Codex. It is
a property of how foregent runs agents at all (§1.1), so the harness always
sets it.

**An MCP server is declared in foregent's own spelling** — a URL and the
*name* of the environment variable holding its token — and each harness
renders that: a header holding `${VARIABLE}` for Claude Code, a
`bearer_token_env_var` for Codex. Neither writes a credential (§6.3).

**Three fields Codex cannot express are refused rather than dropped**: an
appended system prompt, tool allow and deny lists, and restricting an agent to
the servers foregent declares — Codex merges `-c` overrides onto the machine's
config and has no argument meaning *only these*. Nothing sets any of them, and
an agent launched quietly without a restriction its caller asked for is the
failure nobody notices.

**A fresh Codex conversation cannot be named in advance.** Codex has no
counterpart of `--session-id`; it records a session of its own and `resume`
takes that id afterwards, so the id foregent generates is unused there and the
one worth keeping is read back off herdr once the agent has started. The
asymmetry costs nothing today, because resuming a conversation is unbuilt
(§5.4); the id foregent records is whichever the manager returned at launch.

### 6.2 The workflow lives in a skill

`foregent-worker` tells the agent its lifecycle: reading its assignment, the
mode rules, when to delegate its sub-issues, when to report blocked, when to
call `complete_task`, and the rebase requirement. The brief is one line, so the lifecycle has one
definition. It names the issue and the mode (§6.4), which are the two things
about the lifecycle the skill cannot work out for itself.

**How a skill is named is the harness's own**, so the brief's wording comes
from the harness rather than from the bridge: a `/foregent-worker JIM-42
pull-request` slash command in Claude Code, and in Codex a sentence naming the
skill, since Codex has no slash form for one and instead lists every skill it
found by name and description in the model's own prompt.

**Both harnesses read the same file.** A skill is `<name>/SKILL.md` with
name-and-description front matter, in a user-level directory that applies to
every project: `~/.claude/skills` and `$CODEX_HOME/skills`. So foregent ships
one skill and installs it twice, rather than one per harness.

Skills ship inside the installed package, so they travel with a
`uv tool install`. Two paths put them on disk, `foregent setup` for every
harness and the server for the one an issue is about to be worked on, and both
call the one installer in `skills/__init__.py`: what an agent is briefed from
is what foregent ships. Dispatch overwrites because
the drift is otherwise silent in both directions — a skill left over from an
older foregent can name a file that no longer exists, and neither the agent
nor the log has any way to say so (JIM-143). The cost is a hand-edited skill
lost at the next dispatch; edit the packaged one. Each file is staged in its
destination directory and renamed into place, so a concurrent dispatch never
loads a half-written `SKILL.md`.

### 6.3 MCP servers are split by lifetime

**Foregent's own server is per-run.** Its URL is this bridge's, so the launch
spec declares it — `--mcp-config` for Claude Code, `-c mcp_servers.…` for
Codex, which merges over the machine's own table.

**Linear and GitHub are per-machine.** `foregent setup` writes them at Claude
Code's *user* scope — the only scope that applies in a fresh workspace, and
the only kind Codex has — and every agent inherits them. They go in through
`claude mcp add-json -s user` and `codex mcp add --url` rather than by editing
either config file, because a harness rewrites its own.

Nothing restricts an agent to what foregent declares, so one configuration
serves agents and the operator's own sessions alike. The machine is already
the isolation boundary (§1.1). It is also not expressible in Codex, whose
`-c` overrides merge rather than replace (§6.1).

**A server is declared once, in foregent's own spelling** — a URL and the
*name* of the variable holding its token — and each harness renders it. That
is what keeps one list of servers from becoming two that can disagree.

Credentials never reach disk. Claude Code stores the literal
`${LINEAR_API_KEY}` or `${GITHUB_TOKEN}` in a header; Codex stores the
variable's name as `bearer_token_env_var`. Both expand it per session from
the herdr server's environment.

**Each harness is provisioned separately**, and every one on the box is,
whether or not it will be used: the files are small, and a harness provisioned
only when it is first needed is one whose first dispatch is where the operator
finds out it was not.

### 6.4 Project modes

- **bootstrap** — no GitHub surface. The agent rebases onto `main` and commits
  there; the bridge moves the bookmark when the issue completes (§4.3).
- **pull request** — the agent fetches, rebases onto `main`, pushes the branch
  Linear names on the issue as `gitBranchName` and opens a pull request through
  the GitHub MCP, then reports blocked on the review. That name is what makes
  the pull request findable from the issue and the issue readable out of the
  head branch (§4.2), so the skill states the field and the two jj commands
  rather than leaving the agent to name a branch. This is how foregent develops
  itself.

  The fetch and the rebase are the agent's, on every push rather than only the
  first. A workspace is built from the box's local `main` (§6.5) and the agent
  then parks on a review, so `main` moves both before the pull request is
  opened and while it waits; the review that wakes it is often the conflict
  itself. Foregent cannot do it for the agent — resolving a conflict is a
  judgement about the change, and the working copy belongs to the agent.

**A project's mode is derived, not declared.** `jj git remote list` decides it
at dispatch: an `origin` remote on GitHub is where a pull request can be
opened, and everything else — no remotes, an origin hosted elsewhere, a
directory that is not a jj repo — is bootstrap, which needs nothing. The
answer travels to the agent in the brief (§4.1), and the same call answers
again at completion, because it is a pure function of the repo and a stored
copy would be a second place for it to be wrong.

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

  **The bridge holds a lock across the whole advance**, because that refusal
  is only reached by an advance that has read the previous one (§5.2). One
  process performs every advance on a box, so a process-wide lock is the whole
  of it.

  **A conflicted commit is refused before the bookmark moves.** jj publishes
  one happily — the move exits zero, and the file git ends up with is one side
  of the conflict under a commit message claiming the other — so the range
  about to be published is checked for conflicts and the agent is sent back to
  resolve them. Rebasing onto a `main` another agent just landed on is the
  ordinary way to acquire one.
- **A secondary workspace names the repo it belongs to.** Its `.jj/repo` is a
  file holding the path of the shared repo directory, so the repo a teardown
  has to run `forget` in can be read back out of the agent's cwd. That is how
  an agent the state file does not know gets its repo at boot (§5.4). A repo's
  own root answers nothing — there `.jj/repo` is a directory — so nothing
  mistakes a project for a disposable workspace.

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
- **Codex's trust follows git, and fails a dispatch later.** Established by
  driving codex 0.153 under herdr in directories of each shape:
  - **It resolves to a git repository's root** where the cwd is in one, and to
    the exact directory otherwise, walking up no further: a trusted parent
    does not cover its non-git child. A *git worktree* resolves to the main
    repository's root, so one entry there would cover every worktree of it —
    but a secondary jj workspace has no `.git` at all, so it is its own
    project and gets its own entry.
  - **`--dangerously-bypass-approvals-and-sandbox` does not skip the dialog.**
    It governs what a running agent may do, not whether the directory is
    opened at all.
  - **herdr reads the dialog as `idle` and `interactive_ready`**, not
    `blocked` as it does Claude Code's. So an untrusted Codex cwd does not
    fail the launch; it fails the brief, which the prompt's own delivery check
    catches as `agent_prompt_stalled` and reports with the screen quoted. A
    later failure and a noisier one, which is why foregent writes the entry
    rather than relying on the operator.
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
- **Pre-accepted workspace trust.** A fresh directory makes a harness open its
  trust dialog before accepting input. For Claude Code, herdr's detection reads
  that dialog as `blocked` — so the agent never reaches idle and the launch
  fails. Every workspace is a fresh directory, so trust the workspace *root*
  once and every workspace under it inherits it (§7.1). Foregent writes the
  entry itself for any workspace it finds untrusted, so this is a should, not a
  must; doing it by hand keeps foregent out of `~/.claude.json`, which every
  running Claude Code session rewrites.

  **Codex inherits nothing and there is nothing to pre-accept**, because it
  resolves trust to a git repository's root and a secondary jj workspace has no
  `.git`. Foregent writes the exact workspace path, appended to
  `$CODEX_HOME/config.toml` so an operator's own comments and layout survive,
  and that file grows an entry per issue. Nor does the bypass flag skip the
  dialog, and herdr reads it as an idle agent rather than a blocked one, so
  without the entry the dispatch fails at the brief instead of at the launch
  (§7.1).
- **The herdr integration for each harness** (`herdr integration install
  claude`, `herdr integration install codex`), so session identity is reported
  back to herdr.
- **A logged-in harness.** `claude` and `codex login` each hold their own
  credentials, and an unauthenticated one opens a sign-in screen that herdr
  reads exactly as it reads a trust dialog.
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
| `FOREGENT_MAX_AGENTS` | bridge | Live agents at once, in either mode (§5.2). Default 5. |
| `FOREGENT_MAX_ACTIVE` | bridge | Of those, how many actually work at once (§5.2). Default 3, independent of `FOREGENT_MAX_AGENTS`. |
| `FOREGENT_STATE_FILE` | bridge | Where the issue store is persisted (§5.4). Default `~/.local/state/foregent/state.json`. |
| `FOREGENT_LOG_LEVEL` | CLI | Default of `serve --log-level`. Default `info`. |
| `LINEAR_API_KEY` | bridge, agents | Linear API and MCP authentication. |
| `LINEAR_WEBHOOK_SECRET` | bridge | Webhook signature verification. |
| `GITHUB_TOKEN` | bridge, agents | GitHub MCP authentication, and the bridge's lookup of a pull request's head branch (§4.2). |
| `GITHUB_WEBHOOK_SECRET` | bridge | GitHub webhook signature verification. |
| `CLAUDE_CONFIG_DIR` | bridge | Claude Code's own: where its skills and trusted projects are read and written. Default `~/.claude`. |
| `CODEX_HOME` | bridge | Codex's own: the same, plus its MCP servers. Default `~/.codex`. |
