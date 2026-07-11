# Foregent — Design & Plan

Status: **source of truth** for this repo. Supersedes the first-generation
plan (`foregent` repo, `docs/PLAN.md` there) and the transition draft
(`docs/PLAN-V2.md` there). CAO findings herein are from a code-level
investigation of 2026-07-10 (CAO @ `45636f8`).

## 1. Motivation

Foregent's first generation (Bun/TS) built its own agent runtime: tmux
driving, readiness detection via regex markers, launch/resume plumbing, crash
detection. That layer was the persistent source of pain, and it is exactly
the layer that existing open-source orchestrators have already built and
maintain. This generation stands on an existing agent-orchestration platform
and spends our effort on the parts nobody ships — webhook-driven dispatch
through Linear/GitHub, workspace management, and project provisioning.

A second principle: **self-hosting from day one**. The first milestone is not
the bridge — it is a provisioned VM where CAO-launched agents develop
foregent itself. Foregent's own development is the first foregent-managed
project, initially hand-driven, progressively automated as features land.

## 2. Platform decision

Surveyed (July 2026): awslabs/cli-agent-orchestrator, Gas Town, thurbox, Orca,
superset, ruflo, Vibe Kanban, Sandcastle, claude-squad, agentbox, herdr.

**Base platform: awslabs/cli-agent-orchestrator (CAO)** — Apache-2.0, small
active AWS Labs team, neutral governance.

- Best-in-class file-based agent profiles (provider / model / role / tool
  allowlist / MCP servers / prompt per profile) — maps directly to our
  developer / reviewer / task-manager / cryptographer personas.
- Best-in-class async messaging: DB-backed inbox with idle-gated (or eager)
  delivery into running sessions.
- Real provider abstraction: Claude Code first-class (incl.
  `--dangerously-skip-permissions` by default, `permissionMode` per profile),
  Codex and 7 others behind the same interface.
- Terminal backends: tmux (default) and **herdr** — an agent-state-aware
  multiplexer with native working/idle/done/blocked status and process-death
  events over a unix-socket API. We run herdr from the start (§5.10).

Known CAO gaps we own: no webhook ingestion, no worktree isolation (open FR
awslabs/cli-agent-orchestrator#100), no continuous loop (open FR #49),
localhost-oriented server posture, git-only assumptions.

Runner-up notes: Gas Town has the most complete autonomous machinery but a
closed compile-time role set (no custom "cryptographer" without forking),
an opinionated Beads/Dolt task system that would fight Linear-as-interface,
and Codex as a second-class citizen. Thurbox is feature-close but bus-factor
1. Herdr is not an orchestrator but is the best agent-state substrate seen.

### Fork vs. supplement

**Decision: supplement, don't fork** — confirmed by the code investigation
(2026-07-10, CAO @ `45636f8`).

- The REST surface is sufficient to drive CAO externally: launch with
  profile + provider + arbitrary `working_directory` (always accepted on
  `POST /sessions` / `POST /sessions/{name}/terminals`), inbox message send,
  status + transcript reads, terminate. Events are poll-first (an SSE stream
  exists behind `CAO_MCP_APPS_ENABLED`); polling is acceptable initially.
- The capabilities we'd fork *for* — a blocked-on-external-event status,
  generic crash detection — **do not exist inside CAO either** (see §5.6, §9),
  so forking buys nothing; and we now design *with* CAO's grain rather than
  against it: blocked agents park alive on CAO's native `WAITING_USER_ANSWER`
  status (§5.6) and skills go through CAO's own catalog/`load_skill`
  subsystem (§5.8), so there is no session-resume or skill-discovery layer to
  fork for at all. Forking would only buy a standing rebase against a
  fast-moving ~40k-LOC codebase (97 commits in 2 months).
- CAO plugins are observe-only (4 lifecycle events); reaching further means
  importing undocumented internals in-process. We avoid the plugin route and
  keep all foregent logic in our own service.
- **Fallback (option C)** if driving cao-server externally proves awkward:
  vendor just `backends/` (~1.4k LOC, clean 17-method ABC over tmux/herdr)
  plus the claude provider (~800 LOC of TUI status parsing) into the bridge
  and drop cao-server entirely. The valuable, hard-to-rewrite part of CAO is
  exactly those screen-scrapers and backend drivers, not the server around
  them.

### Language

**Python.** CAO is Python: we call `cao-server` with a typed client instead
of shelling to the CLI, import its models/config where useful, and can vendor
or fork components later without a language boundary. FastAPI for our own
HTTP surface (webhooks), matching CAO's stack. The first-generation Bun/TS
code is retired; what carries over is design: issue-keyed dispatch,
release-and-re-dispatch, resume-pinned workspaces, manager-authority crash
detection.

## 3. Goals

1. Fully autonomous multi-agent development system on a dedicated machine/VM
   per project; full permissions (`bypassPermissions` / equivalent).
2. Driven entirely through **Linear** (tasks) and **GitHub** (code review) —
   the operator interacts through those platforms only.
3. **Bootstrap mode** for young projects: Linear-driven but pre-GitHub /
   pre-code-review — agents auto-merge to the local repo's main branch.
4. Multiple agent profiles: developer, reviewer, task-manager, cryptographer,
   … per-project extensible.
5. Ralph-style continuous operation: webhook-triggered plus a keep-things-
   moving loop; no human babysitting.
6. Parallel tasks in isolated workspaces; **rebase-based, linear history** is
   a hard requirement. jj preferred, not mandatory.
7. Multi-repo projects (binius = binius.xyz + binius64 + tracing-profile).
8. One-command provisioning of a fresh machine (devbox VM or cloud VM) via
   cloud-init, with project skills and profiles installed and kept in sync.
9. SSH observability: watch agents (herdr/tmux attach), never interact.
10. Claude Code as the initial provider; keep CAO's multi-provider door open.
11. Track agents blocked on external events (PR review, issue update); park
    them alive and wake them with an inbox message when the event arrives.
12. Shared Rust build caching across parallel agents (sccache).

Non-goals: local/desktop development UX; GUI; supporting non-CAO runtimes
initially; self-learning/memory systems.

## 4. Architecture overview

One machine (or VM) per project. Three layers:

```
            Linear / GitHub  (cloud)
                 │ webhooks            ▲ comments, PRs, issue updates
                 ▼                     │ (written by agents via MCP)
  ┌───────────────────────────────────┴─────────────┐
  │ foregent bridge (Python/FastAPI, STATELESS)     │
  │  • webhook ingestion + event routing            │
  │  • issue↔agent↔workspace: in-memory cache only, │
  │    rebuilt from CAO session names + Linear       │
  │  • park-alive + inbox-wake of blocked agents    │
  │  • workspace manager (worktrees/jj, multi-repo) │
  │  • profile/skill install • policy (boot vs full)│
  └───────────────┬─────────────────────────────────┘
                  │ REST / ops-MCP client calls
                  ▼
  ┌─────────────────────────────────────────────────┐
  │ cao-server (stock)                              │
  │  profiles • terminals (herdr|tmux) • inbox      │
  └───────────────┬─────────────────────────────────┘
                  ▼
        Claude Code sessions in per-task workspaces
        (observed via herdr/tmux attach over SSH)
```

The bridge is **stateless** — it owns no database. It holds an in-memory
cache of the issue↔agent↔workspace mapping and rebuilds it from two durable
backends it already has, so a bridge restart loses nothing (see §5.11):
- **CAO** is the persistent store for *live* agents. Each agent runs as a CAO
  session named `foregent/<ISSUE-KEY>` with `working_directory` derived from
  the issue key, so one `GET /sessions` on startup reconstructs every live
  binding — no bridge-side persistence for running work.
- **Linear** holds the durable per-issue metadata that outlives a session
  (last agent/session, typed blocker, project mode) in an **Attachment
  `metadata` JSON** upserted by a stable synthetic url (`foregent://issue/<KEY>`),
  plus the ownership claim (assignee) and lifecycle state — including an
  **Orphaned** state for in-flight issues whose agent is gone (§5.12).

CAO is the muscle: it launches, supervises, and messages terminals. Agents
talk back to the world through Linear/GitHub MCP tools, and to the bridge
through a small foregent MCP server (assignment fetch, report-blocked,
complete-task).

Webhook delivery: GitHub/Linear → public endpoint on the project VM (or a
relay/tunnel — see Q8) → bridge routes by event type + issue/PR mapping.

## 5. Feature set

### 5.1 Event bridge (the core)
- Receives Linear webhooks (issue created/updated/assigned/commented) and
  GitHub webhooks (PR review submitted, comments, checks, merges).
- Maintains the issue ↔ agent ↔ workspace mapping as an **in-memory cache**,
  rebuildable from CAO + Linear — the bridge owns no database (§5.11).
- Dispatch: ready issue → claim it (assignee + In Progress, §5.12) → acquire
  workspace → launch CAO terminal `foregent/<KEY>` with the right profile +
  cwd → deliver assignment.
- Wake: event matching a parked agent's blocker → inbox message to the
  still-alive session (see 5.6).
- Loop insurance: a periodic tick (CAO flow or internal scheduler) re-checks
  Linear for stuck/unassigned work, so a missed webhook can't stall the
  system (Ralph loop = webhooks + tick).

### 5.2 Agent profiles
- CAO profiles, one file per persona: `developer`, `reviewer`,
  `task-manager`, `cryptographer`, project-specific extras.
- Live in the project's config repo/dir (see 5.8) and are `cao install`ed at
  provision time and on sync.
- All profiles get: foregent MCP server, Linear MCP, GitHub MCP,
  `permissionMode: bypassPermissions`, project env (sccache etc.).
- Each profile carries a `skills:` filter (fnmatch patterns) scoping which of
  CAO's installed skills appear in that persona's catalog (see 5.8).

### 5.3 Task-manager agent
- A profile whose job is grooming: select, order, decompose, and assign
  Linear issues; keep WIP bounded.
- Triggered by the bridge on a schedule and/or on "queue empty" /
  "issue completed" events. It writes to Linear; the bridge reacts to the
  resulting webhooks — the task-manager never dispatches work directly.

### 5.4 Review-comment monitor
- GitHub PR review / comment webhooks → triage: actionable now (deliver to
  the PR's owning agent via inbox) vs follow-up (file a Linear issue).
- Triage itself is a small agent invocation (cheap model) or rule-based to
  start.

### 5.5 Project modes
- **bootstrap**: no GitHub surface. Task lifecycle: Linear issue → bridge
  claims it (assignee + In Progress, §5.12) → agent works in workspace →
  rebase onto main → fast-forward main locally → issue done. Optional
  self-review stage (reviewer profile) before merge.
- **full**: agent pushes branch → opens PR (GitHub MCP) → reviewer
  agent/human reviews → merge via queue with rebase semantics.
- Same pipeline, stages toggled per project in the manifest. Bootstrap mode
  must produce history clean enough to graduate the repo to full mode.

### 5.6 Blocked tracking + park-alive
- Agent hits an external dependency → calls foregent MCP `report_blocked`
  with a typed blocker (`pr-review:binius64#123`, `issue-update:BIN-42`) →
  bridge records the blocker. **The agent session stays alive and idle in its
  workspace** — nothing is terminated, no context is captured or replayed.
  This maps onto CAO's native `WAITING_USER_ANSWER` status: a parked agent is
  simply one waiting for input.
- Wake on matching webhook: the bridge posts the resolving event to the
  agent's inbox (`POST /terminals/{id}/inbox/messages`). Context is intact
  because the session never died and the workspace was never released — no
  session-resume mechanism is needed, and CAO has none (confirmed: no
  `--resume`/session-id code in `providers/`; `terminal restore` is a plain
  shell + scrollback, not a provider session). Embracing this is what lets us
  drive CAO entirely stock.
- Trade-offs accepted:
  - **Crash/reboot loses parked context.** A dead process or VM restart can't
    be resumed — CAO can't, and we no longer try. Wake then degrades to
    re-dispatch fresh (the same fallback crash recovery needs anyway).
  - **Live sessions have a soft ceiling** (each holds a process + herdr pane —
    memory/PIDs, not tokens; idle agents make no API calls). Fine for a small
    fleet; a cap + reaper on long-parked sessions is a later refinement, not a
    phase-2 concern.
- Crash *detection* is still ours: a parked-alive session can die, and on the
  tmux backend a dead `claude` process degrades to status `UNKNOWN` forever
  (Claude's provider never emits `ERROR`), so the bridge runs its own liveness
  probe as the exit authority. The herdr backend emits real `pane.closed`
  events and is the structural fix — one more reason herdr is the default
  (§5.10).

### 5.7 Workspace manager
- Unit: **workspace = directory containing one checkout of each project
  repo** (multi-repo native; single-repo is the degenerate case).
- Pool of N workspaces per machine; issue-keyed acquisition. A parked-alive
  agent holds its workspace for the duration of the block (§5.6), so its
  context and cwd are exactly where it left them.
- Isolation backend: jj workspaces preferred (natural rebase/linear-history
  workflow, no branch-name juggling); git worktrees as fallback if tooling
  friction appears (Q6). CAO is handed only a cwd, so it has no opinion —
  the manager owns VCS entirely.
- Sync: workspace refreshed (rebase onto main / trunk) before each dispatch.

### 5.8 Provisioning & skills sync
- Per-project **manifest** (in the project's infra repo): repo list, mode,
  profiles, skills, env, tokens (referenced, not stored), machine size.
- cloud-init consumes the manifest: install tmux/herdr/uv/CAO/providers/
  foregent-bridge, auth (Claude, GitHub app, Linear), clone repos, install
  profiles + skills, systemd units for cao-server + bridge, start.
- Targets: devbox-managed libvirt VMs (first, via `.devbox/cloud-config.yaml`
  layered on devbox's common template) and cloud VMs from the same material.
- Skills: use **CAO's own skill subsystem** (embrace, don't work around it).
  CAO keeps a global skill store (`cao skills add <folder>`, into
  `~/.aws/cli-agent-orchestrator/skills`) plus `extra_skill_dirs` (settings
  key that can point directly at a repo's `.claude/skills/`, no copy). At
  launch CAO injects a catalog of the installed skills into the system prompt
  and the agent loads a skill's body via the `load_skill` MCP tool
  (`cao-mcp-server`, wired in automatically because CAO builds the launch
  command with `--mcp-config --strict-mcp-config`).
- Why this beats native `.claude/skills/` discovery for foregent: profiles
  carry a per-agent `skills:` filter (fnmatch patterns over skill names), so
  each persona advertises only its relevant subset — `developer` sees dev
  skills, `reviewer` sees review skills, `cryptographer` sees crypto skills —
  which native tree-walk discovery (every agent sees every skill) can't do.
- Two sources, both CAO-native: foregent's own skills are `cao skills add`ed
  at provision/sync time (same shape as `install-profiles.sh` does for
  profiles); a managed repo's project-shipped skills in its `.claude/skills/`
  are picked up live by pointing `extra_skill_dirs` at that folder. Verify the
  `claude_code` provider honors the injected catalog + `load_skill` in
  phase 1.

### 5.9 Rust build caching
- One sccache server per machine; `RUSTC_WRAPPER=sccache` in every profile's
  env; per-workspace `CARGO_TARGET_DIR` (parallel agents must not share a
  target dir).
- Later if needed: shared `CARGO_HOME` (registry/git caches, read-mostly),
  warm target-dir seeding via reflink copies.

### 5.10 Observability
- herdr backend from the start (`terminal.backend: "herdr"` in CAO settings):
  CAO labels it experimental, but the code tells a better story — a complete
  853-LOC backend with no TODO stubs, ~2.6k LOC of dedicated tests, native
  working/idle/done/blocked status, and real `pane.closed` process-death
  events (the only true crash signal in CAO). tmux stays installed as the
  fallback backend if herdr misbehaves (a settings flip, no re-provision).
- Observe over SSH: `herdr --session cao` attaches the TUI (or
  `tmux attach` on the fallback). CAO web dashboard stays localhost, reachable
  over SSH port-forward.
- Bridge exposes a status CLI (`foregent status`): issues in flight, agent
  states, blockers, workspace pool.

### 5.11 Stateless bridge (no database)
- The bridge owns **no persistent store**. Its issue↔agent↔workspace map is an
  in-memory cache; every fact in it is derivable from a durable backend the
  system already runs, so a bridge crash/restart rebuilds full state and loses
  nothing.
- **Live agents ← CAO (source of truth).** cao-server is a separate,
  SQLite-backed process that outlives the bridge. Convention: launch each
  agent as a CAO session named `foregent/<ISSUE-KEY>` with
  `working_directory = <pool>/<ISSUE-KEY>`. On startup the bridge does one
  `GET /sessions`, parses the issue key from each session name, and
  reconstructs every live binding (including parked-alive agents) — no
  bridge-side persistence for running work at all.
- **Durable per-issue metadata ← Linear.** The few facts that must outlive a
  session (last agent/session id, typed blocker, project mode) live in a
  Linear **Attachment `metadata`** JSON blob, one per issue, upserted by a
  stable synthetic url `foregent://issue/<KEY>` (`attachmentCreate` updates in
  place on url match rather than appending). Linear has no custom fields, so
  attachment metadata is the store for per-issue foregent state. (Assignee is
  used, but as an *ownership claim*, not agent identity — see §5.12.)
- Startup reconciliation is the claim/orphan protocol in §5.12, not a blind
  re-dispatch: in-flight issues with no live CAO session move to an **Orphaned**
  state and wait for a scheduling decision.
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
  share one Linear account, so assignee can't name an agent; it names "owned
  by foregent." The durable definition of an owned, in-flight issue is
  `assignee = foregent account ∧ state ∈ {In Progress, In Review, Orphaned}` —
  a set fully recoverable from Linear on boot, which is what makes §5.11's
  stateless reconstruction possible.
- **Claim before work (Todo → In Progress).** The bridge starts an issue only
  after claiming it: set `assignee = foregent` *and* move it to In Progress in
  one step. Nothing is dispatched without a durable ownership record, so a
  crash mid-claim is unambiguous on the next boot.
- **Orphaned is a real Linear workflow state.** On boot the bridge queries its
  team for owned in-flight issues and, for each, looks up the CAO session
  `foregent/<KEY>`:
  - session alive → rebind the in-memory cache; keep working.
  - session gone → transition the issue to **Orphaned** (keeping the
    assignee), recording the prior stage (in-progress vs in-review) and last
    blocker in the attachment metadata so a later re-dispatch resumes at the
    right stage.
- **Orphaned feeds the scheduler, never auto-re-dispatch.** Orphaned issues
  are a queue the scheduler / task-manager (or operator) decides on —
  re-dispatch, defer, or escalate. Re-dispatch transitions Orphaned → In
  Progress/In Review and launches a fresh `foregent/<KEY>` session, briefed
  from the metadata.
- **Partitioning assumption.** Because the account is shared, each issue must
  belong to exactly one foregent instance, enforced by Linear team/project
  scoping; otherwise the shared assignee cannot disambiguate cross-instance
  claims. Within one instance the bridge is the sole arbiter, so claims
  serialize without contention.
- Requires an **Orphaned** workflow state to exist in each managed team
  (provisioning/onboarding step).

## 6. What we deliberately reuse from the first generation (design, not code)

- Issue-keyed, bridge-driven dispatch; a queue that drains without head-of-
  line blocking. (v2 **departs** from v1's release-and-re-dispatch for blocked
  agents: they now park alive on CAO's `WAITING_USER_ANSWER` rather than being
  terminated and resumed — §5.6.)
- Manager exit authority as the single source of truth on agent death
  (reimplemented as the bridge's liveness probe / herdr `pane.closed`).
- Base profile owning lifecycle, project skills owning workflow — the same
  split, now expressed through CAO profiles + CAO's skill catalog (§5.8).

## 7. Phases

Ordered to reach **self-hosting** — CAO developing foregent inside a devbox —
as fast as possible; everything after is developed (increasingly) by the
system itself.

0. **Provisioning skeleton** *(in progress)*: `.devbox/config.toml` +
   `.devbox/cloud-config.yaml` layered on devbox's common template (which
   already ships Claude Code + github/linear plugins and jj): installs tmux,
   uv, herdr, CAO (uv tool from git), writes CAO settings (herdr backend),
   cao-server systemd user unit + linger. Exit criteria: `devbox create`
   yields a box where `cao-server` is up, and a hand-launched
   `cao launch --agent-profile developer` Claude terminal works in the
   foregent checkout.
1. **Self-hosting bootstrap**: this repo pushed into the box (devbox `push`
   mode); a minimal `developer` profile; operator hand-slings foregent tasks
   to agents via `cao` CLI / inbox, observes via `herdr --session cao`.
   Agents commit with jj, rebase-to-main locally (bootstrap mode by hand).
   Also the validation spike, run on the box: inbox messaging (this is the
   wake mechanism — §5.6), status transitions (including the parked agent
   sitting at `WAITING_USER_ANSWER` and waking on an inbox message),
   kill-the-process crash behavior (expect `UNKNOWN` on tmux / `pane.closed`
   on herdr), CAO skill-catalog + `load_skill` working for the `claude_code`
   provider with a per-profile `skills:` filter (§5.8), SSE stream
   (`CAO_MCP_APPS_ENABLED`).
2. **Bridge core**: FastAPI service, in-memory cache rebuilt from CAO session
   names (no database — §5.11), CAO REST client, foregent MCP server
   (get_assignment / report_blocked / complete_task), manual dispatch via CLI.
   Single repo, bootstrap mode, no webhooks yet. From here on, foregent
   development itself runs through the bridge.
3. **Linear loop**: webhook ingestion + task-manager profile + tick; the
   claim/orphan protocol (§5.12: claim-on-start, boot reconciliation, Orphaned
   state + scheduler); the Linear-persistence spike (§5.11: attachment
   `metadata` upsert-by-url, self-webhook actor filtering); the managed team
   gains an **Orphaned** workflow state. Foregent development driven from
   Linear end-to-end in bootstrap mode.
4. **Workspaces**: pool, jj-or-git decision executed (Q6), multi-repo layout,
   sccache wiring.
5. **GitHub full mode**: PR flow, reviewer profile, review-comment monitor,
   park-alive on PR blockers + inbox wake. Foregent graduates from bootstrap
   to full mode on its own repo.
6. **Provisioning generalization + hardening**: manifest + cloud-init for
   cloud VMs; binius onboarded (multi-repo, cryptographer profile); crash
   recovery, missed-webhook reconciliation, cost/usage tracking.

## 8. Risks

- **CAO control-surface gaps** force more vendoring than planned → mitigated
  by the phase-1 validation spike before the bridge is built.
- **CAO is ~1 year old, small team** → could stall or pivot; mitigated by the
  bridge owning all orchestration logic while treating CAO as a swappable
  runtime driven over REST (option C vendoring path as fallback).
- **herdr is young and solo-maintained** → tmux fallback is a settings flip;
  the CAO backend ABC keeps us backend-agnostic.
- **Parked sessions accumulate / are lost on crash** → park-alive (§5.6)
  means blocked agents hold a live process + herdr pane, and a crash or reboot
  loses their context (unresumable). Mitigated by keeping the fleet small,
  adding a cap + reaper on long-parked sessions later, and degrading wake to
  re-dispatch-fresh when a session is gone.
- **Multi-repo + rebase automation** has sharp edges (cross-repo atomic
  changes, conflict handling) → start with binius' real dependency shape,
  keep cross-repo tasks single-owner.
- **Webhook exposure** of per-project VMs → prefer a relay (single hardened
  ingress fanning out to VMs) over N public endpoints (Q8).

## 9. Open questions

**CAO internals — resolved by the 2026-07-10 investigation (CAO @ `45636f8`):**
- **Q1 — Session resume: absent, and we no longer need it.** No
  `--resume`/`--continue`/session-id capture anywhere in `providers/`;
  `terminal restore` = plain shell + scrollback replay; the workflow-journal
  "resume" the team is building is step-DAG replay with fresh terminals, not
  conversational continuity. → Sidestepped by park-alive: a blocked agent
  keeps its live session and workspace and is woken by an inbox message
  (§5.6), so there is nothing to resume. Crash/reboot loss is the accepted
  trade-off.
- **Q2 — Crash/exit authority: absent on tmux.** Status enum is
  `UNKNOWN/IDLE/PROCESSING/COMPLETED/WAITING_USER_ANSWER/ERROR`, but the
  Claude provider never emits `ERROR`; a dead process reads `UNKNOWN`
  forever. No agent self-report tool, no "blocked on external event" state.
  herdr backend is the exception: real `pane.closed` events.
  → Bridge owns liveness (§5.6); herdr default (§5.10).
- **Q3 — Control surface: sufficient.** Launch with profile + provider +
  arbitrary cwd, inbox send, status/output reads, delete — all REST (and
  mirrored 1:1 in ops-MCP). Eventing is poll-first; SSE exists behind
  `CAO_MCP_APPS_ENABLED`. Web UI fully decoupled. → Supplement path viable.
- **Q4 — Plugins: observe-only** (4 lifecycle events, no status events, no
  veto). Deeper reach = importing undocumented in-process internals.
  → Not our extension route; everything lives in the bridge.
- **Q5 — herdr backend: more mature than its "experimental" label** —
  complete implementation, no stubs, ~2.6k LOC of tests, native status +
  process-death events. One legacy wart: DB columns are tmux-named.
  → Adopted as default backend from phase 0; tmux fallback retained.

**Ours to decide:**
- **Q6 — jj workspaces vs git worktrees** for the workspace pool: does jj
  colocation stay friction-free when agents run inside CAO-launched
  sessions? Decide during phase 4 with a spike; the hard requirement is
  rebase/linear history, not jj itself.
- **Q7 — Multi-repo task semantics.** Is one Linear issue ever cross-repo?
  If yes: one workspace, one agent, N repos, ordered pushes — or forbid and
  decompose? Start by forbidding cross-repo issues; revisit with binius data.
- **Q8 — Webhook ingress.** Public endpoint per VM vs a single relay
  (Cloudflare tunnel / small cloud relay fanning out over SSH/WireGuard) vs
  MCP-delivery (webhook events queued and served to agents/bridge via MCP).
  Relay preferred on current thinking. Moot until phase 3 for devbox VMs
  (host can reach them directly; Linear/GitHub cannot).
- **Q9 — Reviewer agents in bootstrap mode**: is there a lightweight
  self-review stage before auto-merge, or is speed the point?
- **Q10 — Task-manager authority bounds.** Can it close/reprioritize
  human-created issues, or propose-only?
- **Q11 — Cost controls.** Per-issue/per-day token budgets, and what happens
  when exceeded (park + Linear comment?).
- ~~**Q12 — Repo residency.**~~ Resolved: this repo (`foregent-v2` on disk,
  "foregent" as the project name); the first-generation repo is retired in
  place.
