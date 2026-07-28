# Foregent — Design & Plan

Status: **source of truth** for this repo — design, decisions, and phase
status. Rejected designs are summarized in §9; everything before it describes
only the current one. Behavior marked *(verified)* was confirmed by driving
herdr 0.7.5 (protocol 17) and Claude Code 2.1.220 directly.

## 1. Motivation

Foregent runs a fleet of coding agents unattended on a dedicated machine per
project, driven entirely through Linear and GitHub. The hard, boring layer —
terminal driving, readiness detection, launch/resume plumbing, crash
detection — is bought rather than built: **herdr** owns terminals and agent
state, **Claude Code** is the agent, and foregent spends its effort on the
parts nobody ships: webhook-driven dispatch, workspace management, and project
provisioning.

A second principle: **self-hosting from day one**. The first milestone is not
the bridge — it is a provisioned VM where foregent-launched agents develop
foregent itself. Foregent's own development is the first foregent-managed
project, initially hand-driven, progressively automated as features land.

## 2. Platform

**herdr as the runtime substrate, Claude Code as the agent harness, foregent
as the orchestrator.** Nothing sits between the bridge and the agents.

What that buys, all *(verified)* on a live box:

- **Headless operation with attachable observability.**
  `herdr --session <name> server` runs with no client attached, and
  `workspace.create {cwd,label}` returns a workspace + tab + root pane at that
  cwd in one call. An operator attaches later with `herdr --session foregent`
  over SSH or `herdr --remote <ssh-target>` from a laptop.
- **Session resume.** Claude Code accepts `--session-id <uuid>` at launch, so
  foregent *assigns* the conversation id rather than scraping it, and
  `--resume <uuid>` restores that conversation in a fresh process. Verified end
  to end: seed a fact → `pkill` the process → relaunch with `--resume` in a new
  pane → the fact is recalled. herdr learns the id independently too — its
  Claude integration hook reports `session_id` + `transcript_path` via
  `pane.report_agent_session`, surfaced as `AgentInfo.agent_session`.
- **Crash authority.** A killed agent leaves `agent.list` immediately, and
  herdr emits `pane_exited` / `pane_closed` on its event stream. Agent death is
  a subscription, not a probe.
- **An event stream, not a poll loop.** `events.subscribe` over the socket
  delivers agent status changes, `pane_exited`, `pane_closed`,
  `workspace_closed`, `pane_agent_detected`, plus workspace/tab/worktree
  events. Three details of that stream shape the consumer (§5.13): status
  changes are **per-pane** subscriptions and the global `pane.updated` is not
  a substitute (its `PaneInfo.agent_status` lags — it reports an agent idle
  while it is working); stopping an agent by closing its workspace emits only
  `workspace_closed`, with no pane event; and herdr spells that one event
  dotted (`pane.agent_status_changed`) where every other name is underscored.
- **A real status enum**: `idle | working | blocked | done | unknown`, driven
  by a versioned, auto-updating detection manifest
  (`~/.local/state/herdr/agent-detection/remote/claude.toml`) with rules for
  permission prompts, selection forms, and the transcript viewer.

The cost is that herdr is a hard dependency: 0.7.x, solo-maintained, protocol
17. Contained by the AgentManager seam (§5.13), a protocol check at startup
(`ping` returns the version), and a degraded fallback inside Claude Code
itself (`claude -p --output-format stream-json`, or `--bg` with
`claude agents`) at the cost of observability.

### Language

**Python.** FastAPI for the webhook/HTTP surface, the official MCP SDK for the
foregent MCP server. Nothing about herdr (a newline-delimited JSON unix
socket) or Claude Code (a CLI) argues against it.

## 3. Goals

1. Fully autonomous multi-agent development system on a dedicated machine/VM
   per project; full permissions (`--permission-mode bypassPermissions`).
2. Driven entirely through **Linear** (tasks) and **GitHub** (code review) —
   the operator interacts through those platforms only.
3. **Bootstrap mode** for young projects: Linear-driven but pre-GitHub /
   pre-code-review — agents auto-merge to the local repo's main branch.
4. One agent per issue, briefed by a foregent skill; how it decomposes the
   work (including whether it spawns Claude Code subagents) is its business,
   not foregent's.
5. Ralph-style continuous operation: webhook-triggered plus a keep-things-
   moving loop; no human babysitting.
6. Parallel tasks in isolated workspaces; **rebase-based, linear history** is
   a hard requirement. jj preferred, not mandatory.
7. Multi-repo projects (binius = binius.xyz + binius64 + tracing-profile).
8. One-command provisioning of a fresh machine (devbox VM or cloud VM) via
   cloud-init, with project skills installed and kept in sync.
9. SSH observability: watch agents (`herdr --session foregent`), never
   interact.
10. Claude Code as the initial harness; the **AgentManager** abstraction
    (§5.13) keeps other harnesses open — herdr already detects 15+ agent kinds.
11. Track agents blocked on external events (PR review, issue update); park
    them alive and wake them with a prompt when the event arrives.
12. Shared Rust build caching across parallel agents (sccache).

Non-goals: local/desktop development UX; GUI; self-learning/memory systems.

## 4. Architecture overview

One machine (or VM) per project. Three layers:

```
            Linear / GitHub  (cloud)
                 │ polled              ▲ comments, PRs, issue updates
                 ▼                     │ (written by agents via MCP)
  ┌───────────────────────────────────┴─────────────┐
  │ foregent bridge (Python/FastAPI, STATELESS)     │
  │  • event tick + routing                         │
  │  • AgentManager (herdr+Claude Code impl)        │
  │  • issue↔agent↔workspace: in-memory cache only, │
  │    rebuilt from herdr agents + Linear           │
  │  • park-alive + prompt-wake of blocked agents   │
  │  • workspace manager (jj/worktrees, multi-repo) │
  │  • skill install • policy (bootstrap vs full)   │
  └───────────────┬─────────────────────────────────┘
                  │ unix socket (newline-delimited JSON)
                  ▼
  ┌─────────────────────────────────────────────────┐
  │ herdr server (headless, systemd, session         │
  │ "foregent")  workspaces • panes • agent state    │
  │ • event stream                                   │
  └───────────────┬─────────────────────────────────┘
                  ▼
        Claude Code sessions in per-issue workspaces
        (observed via `herdr --session foregent`, local or --remote)
```

herdr is the muscle: it owns panes, processes, and agent state. Foregent
launches, prompts, and reaps agents through it. Agents talk back to the world
through Linear/GitHub MCP tools, and to the bridge through a small foregent
MCP server (`report_blocked`, `complete_task`).

Event delivery: the bridge **polls** Linear and GitHub for changes on the
issues it is tracking, then routes by event type + issue/PR mapping. Foregent
is never publicly reachable and receives nothing inbound (Q8, resolved
2026-07-28).

## 5. Feature set

### 5.1 Event bridge (the core)
- A **periodic tick** asks Linear what changed on the issues the bridge is
  tracking (comments, status, assignment), and later GitHub the same (PR
  review submitted, comments, checks, merges). One query per source covers
  the whole fleet, keyed on the in-flight issues, so cost scales with work in
  progress rather than with workspace size.
- **Events are foregent's own shape**, not a provider payload. The transport
  is a source feeding one matcher; that seam is what makes push an additive
  change later rather than a rewrite.
- Maintains the issue ↔ agent ↔ workspace mapping as an **in-memory cache**,
  rebuildable from herdr + Linear — the bridge owns no database (§5.11).
- Dispatch: ready issue → claim it (assignee + In Progress, §5.12) → acquire
  workspace (§5.7) → `AgentManager.launch()` → brief the agent with its issue
  key and the `foregent-worker` skill.
- Wake: an event on a parked agent's own issue → prompt delivered to the
  still-alive agent (§5.6). Matching is `wakes(event) -> key`, a lookup rather
  than a scan, because the event names the issue it is about.
- **Never wake on foregent's own writes.** Claiming an issue assigns it and
  moves its state, and agents comment through the Linear MCP under the same
  account; both come back as changes. `wakes()` drops them by actor identity,
  and polling can drop most of them a step earlier, server-side
  (`user: { id: { neq: $viewerId } }`).
- The tick is also loop insurance: it re-checks Linear for stuck/unassigned
  work, so nothing missed can stall the system (Ralph loop).

### 5.2 The issue agent
**One Claude Code agent owns one Linear issue, end to end.** There is no
supervisor/worker hierarchy: the agent reads the issue, drives its Linear
status, writes the code, and closes it out. If it wants to fan work out to
Claude Code subagents, it does so natively and foregent neither knows nor
cares.

- **Workflow lives in a skill.** `foregent-worker` (installed per §5.8)
  explains the lifecycle: how to fetch its assignment, the bootstrap-vs-full
  mode rules, how to report blocked, when to call `complete_task`, and the
  rebase/linear-history requirement.
- **Configuration is a launch spec**, a small structure foregent owns, rendered
  to Claude Code flags by the AgentManager (§5.13): `--model`, `--effort`,
  `--permission-mode bypassPermissions`, `--append-system-prompt`,
  `--mcp-config` + `--strict-mcp-config`, `--allowedTools` /
  `--disallowedTools`, `--settings`, `--add-dir`, `--session-id`,
  `-n <display name>`, plus cwd and env.
- **MCP set:** foregent (lifecycle tools), Linear, GitHub — but split by
  lifetime, not bundled. Foregent's own server is per-run (its URL is this
  bridge's), so it is declared in the launch spec via `--mcp-config`. Linear
  and GitHub are per-machine, so they are provisioned once into the box's
  user-level Claude Code config by `foregent setup` (§5.8) and inherited.
  `--strict-mcp-config` stays **off** (decided 2026-07-28, JIM-93).
  - Rejected: declaring Linear/GitHub in the launch spec under strict mode.
    It would have made an agent's reach a property of its launch spec rather
    than of the machine, which is the stronger isolation story — but on a
    one-project-per-box design the machine *is* the boundary, and strict mode
    buys that isolation by breaking the operator: hand-launched sessions
    (§5.10, the whole phase-1 workflow) would keep needing a separate manual
    `claude mcp add`, configured twice and drifting apart. One provisioned
    config serving agents and operator alike is the simpler invariant.
  - The cost accepted: an unprovisioned box dispatches agents that cannot read
    their own issue. Mitigated by `foregent setup` being the one installer for
    both halves, and by the bridge warning at startup when the servers or
    their credentials are absent rather than discovering it mid-issue.
  - Credentials are never written to disk: the stored header is the literal
    `${LINEAR_API_KEY}` / `${GITHUB_TOKEN}`, expanded per session by Claude
    Code from the herdr server's env (§5.8). Revisit strict mode if a box ever
    hosts more than one project, or if agent and operator need different
    identities.
- Project-specific variants (e.g. a binius agent with a cryptography skill and
  a different model) are a different launch spec plus different skills.

### 5.3 Issue-scout agent (deferred)
- An agent whose job is grooming: select, order, decompose, and assign Linear
  issues; keep WIP bounded. It writes to Linear; the bridge reacts to the
  resulting webhooks — the scout never dispatches work directly.
- **Deferred to a later phase.** Until it exists, issue selection/sequencing is
  the operator's job (or a simple bridge tick over ready issues).

### 5.4 Review-comment monitor
- GitHub PR review / comment webhooks → triage: actionable now (prompt the
  PR's owning agent) vs follow-up (file a Linear issue).
- Triage itself is a small agent invocation (cheap model, `claude -p`) or
  rule-based to start.

### 5.5 Project modes
- **bootstrap**: no GitHub surface. Task lifecycle: Linear issue → bridge
  claims it (§5.12) → agent works in workspace → rebase onto main →
  fast-forward main locally → issue done. Optional self-review stage before
  merge.
- **full**: agent pushes branch → opens PR (GitHub MCP) → reviewer
  agent/human reviews → merge via queue with rebase semantics.
- Same pipeline, stages toggled per project in the manifest. Bootstrap mode
  must produce history clean enough to graduate the repo to full mode.

### 5.6 Blocked tracking + park-alive
- Agent hits an external dependency → calls foregent MCP `report_blocked`
  with a plain-language blocker (`a review of the PR`) → bridge records it.
  **The agent stays alive and idle in its
  workspace** — nothing is terminated, no context is captured or replayed.
  herdr reports it as `idle` (or `blocked` if it is sitting on a prompt).
- Wake on matching webhook: the bridge delivers the resolving event with
  `AgentManager.send()`. Context is intact because the process never died and
  the workspace was never released. Capacity does not change either — the
  parked agent was holding its slot the whole time.
- **An event wakes the agent that owns the issue the event is about**, and the
  blocker is never read (`foregent/events.py`, a pure `wakes(event) -> key` so
  it is testable without a server, a transport or a live agent — the tick
  (§5.1) then becomes a trigger for machinery that already works).
  Three kinds wake an agent, and nothing else does:
  - a comment or reply on the agent's own Linear issue;
  - a review or comment on the pull request linked to that issue, inline or
    PR-level;
  - that pull request ceasing to merge cleanly as main advances.
- **The blocker is a note, not a key.** An earlier design had the agent report
  a *typed* blocker (`pr-review:<repo>#<n>`, `issue-update:<KEY>`,
  `human:<what you need>`) that the bridge matched events against. Rejected
  (2026-07-28, JIM-101): every typed form reduces to "something happened on my
  ticket" once the PR is linked, so the typing bought a parsing vocabulary and
  a scan over parked agents in exchange for nothing. The blocker stays as
  free text — what the agent was waiting for, for the operator reading
  `foregent status`.
  - **Linking is Linear's job, not the agent's.** A worker that pushes its
    branch gets the PR linked to the ticket by Linear itself, off the branch
    name, so the bridge resolves PR → issue server-side and the agent never
    reports a PR number to be findable.
  - Consequence accepted: an agent waiting on a *different* ticket to land has
    no automatic wake, since activity there never touches its own ticket. The
    operator comments on the parked ticket instead — a person deciding the
    dependency is satisfied, rather than the bridge guessing it from a state
    change.
  - Linear *field* updates deliberately do not wake: a priority tweak or a
    label change is not an answer to the question the agent asked.
- The wake message **carries the event**, not merely "you are unblocked": the
  agent has to act on the feedback, and re-reading the issue to find out what
  it was is a round trip it does not need.
- `POST /issues/{key}/wake` is the seam ingestion will call, and is drivable
  with `curl` on its own. It **sends before it unblocks**, so a harness
  failure leaves the issue BLOCKED with no rollback path to get wrong and a
  retry is safe — and because an agent that has not received the message is
  not awake yet. A blocked issue with no agent recorded is a 409, like one
  that was never parked.
- Cold parking (terminate on block, `--resume` on wake) is technically viable
  since resume works, but instant wake and a guaranteed-intact context beat the
  memory savings for a small fleet. Resume is for *recovery* (§5.12), not
  routine parking.
- Trade-offs accepted:
  - **Reboot costs a re-dispatch** — but not a *fresh* one: the conversation id
    is durable (§5.11), so an orphaned issue resumes rather than restarts.
  - **Live sessions have a soft ceiling** (each holds a process + herdr pane —
    memory/PIDs, not tokens; idle agents make no API calls). A cap + reaper on
    long-parked sessions is a later refinement; because resume works, the
    reaper can cold-park rather than kill.
- Crash *detection* is herdr's: a killed agent disappears from `agent.list` at
  once and the bridge sees `pane_exited` / `pane_closed` on the event stream.

### 5.7 Workspace manager
- Unit: **workspace = directory containing one checkout of each project
  repo** (multi-repo native; single-repo is the degenerate case).
- **Foregent owns VCS; herdr is handed a cwd.** The manager creates the
  checkouts (jj workspaces preferred, git worktrees as fallback — Q6), then
  calls `workspace.create {cwd, label}`. herdr's own `worktree.create` is not
  the primary path: it is git-only and one-repo-per-workspace, which would
  foreclose Q6 and fight goal 7. `worktree.open` may be called afterward so
  herdr's TUI shows repo/branch for the single-repo git case.
- Pool of N workspaces per machine; issue-keyed acquisition. A parked-alive
  agent holds its workspace for the duration of the block (§5.6).
- Sync: workspace refreshed (rebase onto main / trunk) before each dispatch.

### 5.8 Provisioning & skills sync
- Per-project **manifest** (in the project's infra repo): repo list, mode,
  skills, env, tokens (referenced, not stored), machine size.
- cloud-init consumes the manifest: install uv/herdr/Claude Code/foregent,
  auth (Claude, GitHub app, Linear), clone repos, install skills, systemd
  units for the herdr server + bridge, start.
- **Skills are installed into the box's Claude Code skill directory**
  (`~/.claude/skills/`, or `$CLAUDE_CONFIG_DIR/skills` where that is set).
  Foregent's own skills (`foregent-worker`, plus workflow skills) ship *inside
  the installed package* (`foregent/skills/<name>/SKILL.md`), so they travel
  with a `uv tool install` rather than only existing in a repo checkout; a
  managed repo's project-shipped skills in its `.claude/skills/` are discovered
  natively from the workspace cwd. Accepted consequence: every agent on the box
  sees every installed skill — fine, since there is one agent kind per issue.
- **Two paths put them there**, both over `foregent/skills.py` so they cannot
  drift:
  - `foregent setup` — run at provision time and again after every foregent
    upgrade. Copies every packaged skill, overwriting stale ones, and reports
    per skill whether it installed, updated, or changed nothing, so an
    operator whose edits were replaced is told rather than left to find out.
  - **The server writes any missing skill before it launches an agent.** A box
    where setup was never run still dispatches correctly, instead of briefing
    an agent to use a skill that is not on disk. It only fills gaps — updating
    stays setup's job, so a deliberately edited skill survives every dispatch.
    The cost is that a skill left over from an older foregent persists
    silently; the fix is making setup part of provisioning, not making
    dispatch overwrite files mid-flight.
- Two constraints the implementation turns on. **The write must complete
  before `agent.start`, not alongside it**: Claude Code picks up edits to a
  skills directory live, but only one that existed when the session started,
  so on a fresh box the skill has to be there first or that agent never sees
  it. And **concurrent dispatches race**, so each file is staged in its
  destination directory and renamed into place — an agent never loads a
  half-written `SKILL.md`.
- **`foregent setup` also provisions the shared MCP servers** (`foregent/
  mcp_servers.py`, JIM-93): Linear and GitHub, at Claude Code's *user* scope —
  the only scope that applies in a fresh per-issue workspace. Written through
  `claude mcp add-json -s user` rather than by editing `~/.claude.json`
  directly: every running session rewrites that file, so foregent reads it to
  decide and lets Claude Code's own writer do the writing. Gap-filling only,
  unlike skills — re-adding a server would discard an OAuth login foregent
  cannot recreate, and setup must stay safe to re-run after every upgrade.
- Knowing that skills live in `~/.claude/skills/` and MCP servers in
  `~/.claude.json` is a Claude Code detail leaking through the `AgentManager`
  seam (§5.13). Accepted while there is one harness; a second one makes the
  ensure step a manager method.
- **Provisioning tasks that are dispatch blockers if missed:**
  - *Clean environment for the herdr server.* Every pane inherits the server's
    env. A server started from inside another Claude Code session leaks
    `CLAUDECODE=1` / `CLAUDE_CODE_CHILD_SESSION=1` into the agent, which
    **silently disables transcript saving** ("Transcript saving is off —
    inherited CLAUDE_CODE_…") and breaks resume. The systemd unit must set an
    explicit env, not inherit one.
  - *Pre-accept the workspace trust dialog.* A fresh directory makes Claude
    Code open `❯ 1. Yes, I trust this folder` before accepting any input, and
    every per-issue workspace is a fresh directory. Pre-seed trust for the
    workspace pool root rather than answering a modal per dispatch.
  - *Install the herdr Claude integration* (`herdr integration install claude`)
    so `SessionStart` reports session identity back to herdr.
  - *Pin/record the herdr protocol version* (`ping` → `protocol: 17`) and fail
    startup on mismatch.
  - *`LINEAR_API_KEY` / `GITHUB_TOKEN` in the herdr server's env.* The MCP
    config stores the variable, not the token (§5.2), so a server missing its
    variable is configured, looks installed, and fails to authenticate once an
    agent is already working. The bridge warns about both at startup.

### 5.9 Rust build caching
- One sccache server per machine; `RUSTC_WRAPPER=sccache` in every agent's
  env; per-workspace `CARGO_TARGET_DIR` (parallel agents must not share a
  target dir).
- Later if needed: shared `CARGO_HOME` (registry/git caches, read-mostly),
  warm target-dir seeding via reflink copies.

### 5.10 Observability
- One named herdr session (`foregent`) per box, run headless as a systemd user
  unit; its socket lives at `~/.config/herdr/sessions/foregent/herdr.sock`.
- **Which session the bridge drives is resolved, not compiled in**:
  `FOREGENT_HERDR_SESSION` first, then the session the bridge process is
  itself running in (herdr injects `HERDR_SOCKET_PATH` into every pane it
  owns, so this is herdr's own signal rather than an inference), then herdr's
  default session. The order is what keeps deployment deterministic: the
  systemd unit runs outside any pane, so it **must set the variable** or it
  would land in the default session — the operator's interactive one — rather
  than the dedicated session that exists to be attached to read-only. A dev
  box sets nothing and reaches whichever session its shell already lives in.
  An inherited socket path that is dead fails loudly instead of falling back;
  talking to a different session than the environment names is the worse
  outcome. Startup logs the session and socket it resolved to.
- Observe by attaching: `herdr --session foregent` over SSH, or
  `herdr --remote <ssh-target> --session foregent` straight from a laptop.
  Attaching is read-only by convention; never interact.
- Per-agent inspection without attaching: `agent.list`, `agent.get`,
  `agent.read {source: visible|recent|detection}`, `agent.explain` (why herdr
  believes an agent is in a given state).
- Bridge exposes a status CLI (`foregent status`): issues in flight, agent
  states, blockers, workspace pool.

### 5.11 Stateless bridge (no database)
- The bridge owns **no persistent store**. Its issue↔agent↔workspace map is an
  in-memory cache; every fact in it is derivable from a durable backend the
  system already runs, so a bridge crash/restart rebuilds full state.
- **Live agents ← herdr (source of truth).** The herdr server is a separate
  systemd unit that outlives the bridge. Convention: each agent is launched
  with herdr agent name `fg-<issue-key-lowercased>` (herdr enforces
  `[a-z][a-z0-9_-]{0,31}`, unique among live agents) in a workspace labeled
  with the uppercase issue key. On startup the bridge does one `agent.list`,
  parses the issue key out of each name, and reconstructs every live binding
  (including parked-alive agents).
- **Durable per-issue metadata ← Linear.** The facts that must outlive a
  process (**Claude Code conversation id**, workspace path, blocker,
  stage, project mode) live in a Linear **Attachment `metadata`** JSON blob,
  one per issue, upserted by a stable synthetic url `foregent://issue/<KEY>`
  (`attachmentCreate` updates in place on url match). Linear has no custom
  fields, so attachment metadata is the store for per-issue foregent state.
- The conversation id is **generated by foregent** (a UUID passed as
  `--session-id`), not scraped, so it is known and recorded *before* the agent
  starts — a crash between launch and first checkpoint is still recoverable.
- **Constraints this imposes** (both real, designed-for from day one):
  - *Not the agent-facing MCP.* Attachment `metadata` and url-upsert are
    GraphQL-API features the agents' Linear MCP plugin does not expose; the
    bridge needs its own direct Linear API client.
  - *Self-webhook filtering.* The bridge writing to Linear can emit webhooks
    the bridge then receives; it must drop events whose actor is its own bot
    account or it will chase its own writes.
- Spike (phase 3): confirm `attachmentCreate` upsert-by-url and `metadata`
  round-trip on the live Linear API before relying on it.

### 5.12 Issue claiming & orphan reconciliation
- **Assignee = ownership claim, not agent identity.** All foregent instances
  share one Linear account, so assignee names "owned by foregent." The durable
  definition of an owned, in-flight issue is `assignee = foregent account ∧
  state ∈ {In Progress, In Review, Orphaned}` — a set fully recoverable from
  Linear on boot, which is what makes §5.11's reconstruction possible.
- **Claim before work (Todo → In Progress).** The bridge starts an issue only
  after claiming it: set `assignee = foregent` *and* move it to In Progress in
  one step. Nothing is dispatched without a durable ownership record.
- **Orphaned is a real Linear workflow state.** On boot the bridge queries its
  team for owned in-flight issues and, for each, looks for the live herdr agent
  `fg-<key>`:
  - agent alive → rebind the in-memory cache; keep working.
  - agent gone → transition the issue to **Orphaned** (keeping the assignee),
    recording the prior stage and last blocker in the attachment metadata.
- **Only an in-flight issue can be orphaned** — In Progress, In Review or
  Blocked. The event stream cannot tell a crash from foregent's own teardown
  (both are `workspace_closed`, §2), and foregent stops an agent itself the
  moment its issue completes; the issue's status is what distinguishes them,
  and it is already Done by the time that event arrives. So orphaning is a
  transition *out of* an in-flight state, never a status a Done, already-
  orphaned, or never-dispatched issue can be moved into.
- **Orphaned re-dispatch resumes, it does not restart.** Because the
  conversation id and workspace path are in the attachment metadata, and
  `--resume` works, re-dispatching an orphan relaunches the same conversation
  in the same workspace and prompts it with what changed. Restart-from-scratch
  is the fallback when the transcript is unusable.
- **Orphaned feeds the scheduler, never auto-re-dispatch.** Orphaned issues are
  a queue the scheduler / issue-scout (or operator) decides on.
- **Partitioning assumption.** Because the account is shared, each issue must
  belong to exactly one foregent instance, enforced by Linear team/project
  scoping. Within one instance the bridge is the sole arbiter, so claims
  serialize without contention.
- Requires an **Orphaned** workflow state in each managed team (provisioning
  step).

### 5.13 The AgentManager abstraction
Foregent owns *what an agent is for*; the AgentManager owns *how a harness is
driven*. The seam exists so a second harness can be added without touching
dispatch, and so the herdr dependency is contained in one module.

```python
@dataclass(frozen=True, slots=True)
class LaunchSpec:
    label: str                     # "fg-jim-52" — the harness-level agent name
    cwd: str
    env: Mapping[str, str]
    model: str | None
    effort: str | None
    system_prompt: str             # appended to the harness default
    tools_allow: tuple[str, ...]
    tools_deny: tuple[str, ...]
    mcp_servers: Mapping[str, Mapping]   # foregent | linear | github
    conversation_id: str | None    # foregent-generated UUID; None = new
    resume: bool

class AgentStatus(StrEnum):
    IDLE; WORKING; BLOCKED; DONE; UNKNOWN; GONE

class AgentManager(Protocol):
    def launch(self, spec: LaunchSpec) -> AgentRef
    def send(self, ref, text, *, when_idle: bool = True) -> None
    def status(self, ref) -> AgentStatus
    def wait(self, ref, until: Collection[AgentStatus], timeout: float) -> AgentStatus
    def read(self, ref, lines: int) -> str          # scrollback, for triage
    def stop(self, ref) -> None
    def list_agents(self) -> list[AgentRecord]      # boot reconciliation (§5.11)
    def events(self) -> Iterator[AgentEvent]        # status_changed | exited
```

Calls are synchronous and may block for as long as an agent takes; the API
server runs them in a threadpool, as it already does for the Linear client.
Harness failures surface as a single `AgentError`, so the bridge never
catches one runtime's socket errors and another's HTTP errors.

**`HerdrClaudeManager`** is the implementation we build: it renders
`LaunchSpec` to a `claude` argv, calls `workspace.create` → `agent.start` →
`agent.prompt`, and translates herdr's event stream into `AgentEvent`s.
Requirements observed on live runs:

- `launch()` waits for `idle` and then polls for `interactive_ready`. An
  agent reads as idle seconds before its TUI will take input, and prompting
  in that window is refused with `agent_not_ready`; `interactive_ready` is
  the real precondition, so there is no settle time to guess at.
- **Every prompt carries a `wait` block, and that is not optional.** herdr
  only runs its delivery check — answering `agent_prompt_stalled` when the
  prompt produced no lifecycle change — when a prompt is sent with `wait`. A
  bare `agent.prompt` is reported as succeeding even when the text is
  swallowed by a modal the agent is sitting on, leaving the message unsent in
  the input box while the agent still reads as idle and interactive. Neither
  the screen nor `state_change_seq` distinguishes that case: typed-and-unsent
  text is on screen, and typing alone moves the counter.
- **A stall means the agent never saw it, so a resend cannot double up.** A
  `timeout` on that wait means the opposite — the agent reacted but did not
  reach the watched state — and counts as delivered.
- `send(when_idle=True)` gates delivery: `wait(until={IDLE})` then `prompt`.
  Queueing semantics are ours.
- `GONE` is derived from an agent's absence from `agent.list` or a
  `pane_exited` / `pane_closed` event.
- Agents are addressable by herdr agent name, so no handle needs persisting
  (§5.11).

The interface is deliberately tolerant of harnesses with weaker eventing:
`events()` may be implemented as a polling loop, and `GONE` is explicit rather
than inferred from a status enum (see §9).

## 6. Phases

Ordered to reach **self-hosting** — agents developing foregent inside a devbox
— as fast as possible; everything after is developed (increasingly) by the
system itself.

0. **Provisioning skeleton** *(in progress)*: `.devbox/config.toml` + mods in
   `.devbox/mods/` appended to devbox's base playbook (which already ships
   Claude Code + github/linear plugins and jj): install uv + herdr, install the
   herdr Claude integration, write the herdr systemd user unit for session
   `foregent` **with an explicit clean env** (§5.8), enable linger, pre-accept
   workspace trust for the pool root. Exit criteria: `devbox create` yields a
   box where the headless herdr server is up and a hand-launched Claude Code
   agent in a herdr workspace reaches `idle` and accepts a prompt.
1. **Self-hosting bootstrap**: this repo pushed into the box; the
   `foregent-worker` skill written, shipped in the package, and installed into
   `~/.claude/skills/` by `foregent setup` (§5.8); the operator hand-launches
   an agent per foregent task via the herdr CLI and
   observes with `herdr --session foregent`. Agents commit with jj and rebase
   to main locally (bootstrap mode by hand).
   Spikes for this phase: Linear/GitHub MCP provisioning — **resolved
   2026-07-28 (JIM-93)**: installed per machine by `foregent setup` at user
   scope and inherited by agents, not declared per launch spec under
   `--strict-mcp-config` (§5.2 records why) — and skill discovery confirmed for
   both `~/.claude/skills/` and a workspace repo's `.claude/skills/`.
2. **Bridge core**: FastAPI service, `HerdrClaudeManager` (§5.13), in-memory
   cache rebuilt from herdr agent names (no database — §5.11), foregent MCP
   server (`get_assignment` / `report_blocked` / `complete_task`), manual
   dispatch via CLI, event-stream consumer for status/death. Single repo,
   bootstrap mode, no event delivery yet. From here on, foregent development
   itself runs through the bridge.
3. **Linear loop**: the wake path (unblock + prompt an agent, blocker matching)
   and the tick that feeds it over tracked and ready issues; the claim/orphan
   protocol (§5.12) including resume-based re-dispatch; the
   Linear-persistence spike (§5.11: attachment `metadata` upsert-by-url,
   self-event actor filtering); the managed team gains an **Orphaned**
   workflow state. Foregent development driven from Linear end-to-end in
   bootstrap mode.
4. **Workspaces**: pool, jj-or-git decision executed (Q6), multi-repo layout,
   sccache wiring.
5. **GitHub full mode**: PR flow, review-comment monitor, park-alive on PR
   blockers + prompt wake. Foregent graduates from bootstrap to full mode on
   its own repo.
6. **Provisioning generalization + hardening**: manifest + cloud-init for
   cloud VMs; binius onboarded (multi-repo, cryptography skills); crash
   recovery, cost/usage tracking.

## 7. Risks

- **herdr is young and solo-maintained**, and is a hard dependency → contained
  behind the AgentManager (§5.13); protocol version checked at startup;
  degraded fallback is Claude Code's own headless modes at the cost of
  observability.
- **Agent-state detection is screen-scraping under the hood.** herdr's
  `claude.toml` manifest auto-updates against Claude Code's TUI; a Claude Code
  release can move faster than the manifest. Mitigations: the integration hook
  (authoritative session identity), `agent.explain` for diagnosis, and pinning
  a known-good manifest if a regression lands.
- **Prompt delivery is TUI-mediated**, so it can stall or double-deliver
  (§5.13). Mitigated by state-change verification in the manager; a future
  option is Claude Code's stream-json input for machine-to-agent traffic.
- **Parked sessions accumulate / are lost on reboot** → park-alive (§5.6) holds
  a live process per blocked issue. Mitigated by a small fleet, a later
  cap + cold-park reaper, and resume-based recovery (§5.12).
- **Multi-repo + rebase automation** has sharp edges (cross-repo atomic
  changes, conflict handling) → start with binius' real dependency shape, keep
  cross-repo tasks single-owner.
- **Event latency and API budget** replace webhook exposure as the delivery
  risk, now that the bridge polls and is never publicly reachable (Q8). Linear
  allows 2,500 requests/hour per key; a 30-second tick spends under 5% of it,
  and the ceiling is the tick rate rather than the fleet size. A push
  transport, if one is ever needed, is the mitigation.

## 8. Open questions

- **Q6 — jj workspaces vs git worktrees** for the workspace pool. Decide during
  phase 4 with a spike; the hard requirement is rebase/linear history, not jj
  itself.
- **Q7 — Multi-repo task semantics.** Is one Linear issue ever cross-repo? Start
  by forbidding cross-repo issues; revisit with binius data.
- ~~**Q8 — Webhook ingress.**~~ **Resolved 2026-07-28 (JIM-102): poll, don't
  receive.** Bridges sit on private networks with no inbound port, and every
  push option pays for that with infrastructure — a Cloudflare named tunnel
  (domain + daemon + DNS + subscription lifecycle) or a Lambda/SQS relay (AWS
  credentials on every box). What they buy is latency: sub-second against a
  30-second tick, when every consumer is a human replying to a review.
  Decisive point: §5.1 commits to a periodic tick regardless, so push would be
  built *on top of* the loop rather than instead of it — and polling's failure
  mode is lateness where push's is silence. Revisit when latency is genuinely
  felt (→ Cloudflare tunnel) or when one endpoint serves many bridges and lost
  events stop being acceptable (→ Lambda + SQS, whose buffer earns its keep
  there). Both stay cheap to add because the event shape is foregent's own.
- **Q9 — Reviewer stage in bootstrap mode**: is there a lightweight self-review
  pass before auto-merge, or is speed the point? A question about the skill's
  workflow, not about a second agent.
- **Q10 — Agent authority bounds.** Can an agent close/reprioritize
  human-created issues, or propose-only?
- **Q11 — Cost controls.** Per-issue/per-day token budgets, and what happens
  when exceeded (park + Linear comment?).
- **Q12 — Agent self-identification.** herdr injects `HERDR_PANE_ID`,
  `HERDR_WORKSPACE_ID`, and `HERDR_SOCKET_PATH` into every pane, so the
  foregent MCP server could identify the calling agent from its environment
  instead of trusting an issue key passed as a tool argument. Worth doing in
  phase 2 if it is cheap.

## 9. Alternative designs

### CAO-centric (rejected 2026-07-25)

The prior design made **awslabs/cli-agent-orchestrator (CAO)** the base
platform: the bridge drove `cao-server` over REST, agents were CAO sessions
launched from file-based CAO profiles, herdr appeared only as CAO's terminal
backend, and work was split across three personas — a `task_supervisor` per
issue delegating to `developer` and `reviewer` workers over `cao-mcp-server`,
with MCP allowlists as the isolation boundary.

Why it lost:

- **Its two known gaps were structural.** CAO has no session resume (no
  `--resume`/session-id handling in its providers) and no crash authority on
  the tmux backend (a dead `claude` process reads `UNKNOWN` forever). Both are
  native to herdr + Claude Code, and both were load-bearing for park/wake and
  orphan recovery.
- **Everything else it supplied is a thin layer we own better.** Profiles → a
  launch spec rendered to `claude` flags; the idle-gated inbox → `agent.wait`
  + `agent.prompt`; the skill catalog with per-profile filters → Claude Code's
  native skills; the session registry → `agent.list`.
- **The persona hierarchy was solving a CAO-shaped problem.** Worker spawning
  and the `--strict-mcp-config` isolation trick existed because CAO models
  multi-agent work. One Claude Code agent per issue, briefed by a skill, does
  the same job and decides its own decomposition.
- **CAO's own escape hatch pointed here.** The documented fallback was to
  vendor CAO's `backends/` plus its Claude provider — the screen-scraping and
  terminal driving — which is precisely what herdr already is, with an event
  stream CAO's tmux backend cannot produce.

What survives: the AgentManager seam (§5.13) is shaped so a `CaoManager` could
be added without touching dispatch — it would render `LaunchSpec` to a CAO
profile plus `POST /sessions`, map CAO's status enum
(`IDLE/PROCESSING/COMPLETED/WAITING_USER_ANSWER/ERROR/UNKNOWN`) onto
`AgentStatus`, use the inbox for `send()`, and implement `events()` as a poller
(or CAO's SSE stream behind `CAO_MCP_APPS_ENABLED`). That possibility is why
`events()` may poll and why `GONE` is explicit. It is documented, not built.

Artifacts retired by this decision: `src/foregent/cao.py`, `tests/test_cao*.py`,
`profiles/*.md` (CAO frontmatter), and `scripts/install-profiles.sh`.

### Other options considered

- **Cold parking instead of park-alive** — terminate a blocked agent and
  `--resume` it on wake. Viable now that resume is verified, and it removes the
  live-session ceiling; rejected for routine use because instant wake with a
  guaranteed-intact context is worth more at this fleet size (§5.6). Retained
  as the mechanism for orphan recovery and a future reaper.
- **herdr-owned workspaces** — `worktree.create` builds the checkout and opens
  it as a workspace in one call, with lifecycle events. Rejected as the primary
  path: git-only and one repo per workspace, which forecloses jj (Q6) and the
  multi-repo requirement (goal 7). `worktree.open` remains available for TUI
  niceness (§5.7).
- **Bridge-orchestrated roles** — no agent-side judgment at all; the bridge
  sequences a developer agent, then a reviewer agent, per issue. Cheapest and
  most deterministic, but it moves decomposition into bridge code, which is the
  work the agent is better at.
