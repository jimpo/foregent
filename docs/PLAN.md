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
- The capabilities we'd fork *for* — live session resume, a blocked-on-
  external-event status, generic crash detection — **do not exist inside CAO
  either** (see §5.6, §9). They are new code wherever they live, so forking
  buys nothing; it only buys a standing rebase against a fast-moving ~40k-LOC
  codebase (97 commits in 2 months) whose team is optimizing for a different
  shape (bounded synchronous workflow steps, not long-parked resumable
  sessions).
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
    them and wake/resume with context when the event arrives.
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
  │ foregent bridge (Python/FastAPI)                │
  │  • webhook ingestion + event routing            │
  │  • dispatch state: issue ↔ agent ↔ workspace    │
  │  • park/resume of blocked agents                │
  │  • workspace manager (worktrees/jj, multi-repo) │
  │  • skills sync • policy (bootstrap vs full)     │
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

The bridge is the only stateful authority. CAO is the muscle: it launches,
supervises, and messages terminals. Agents talk back to the world through
Linear/GitHub MCP tools, and to the bridge through a small foregent MCP
server (assignment fetch, report-blocked, complete-task).

Webhook delivery: GitHub/Linear → public endpoint on the project VM (or a
relay/tunnel — see Q8) → bridge routes by event type + issue/PR mapping.

## 5. Feature set

### 5.1 Event bridge (the core)
- Receives Linear webhooks (issue created/updated/assigned/commented) and
  GitHub webhooks (PR review submitted, comments, checks, merges).
- Maintains the issue ↔ agent ↔ workspace mapping (SQLite).
- Dispatch: new/assigned issue → acquire workspace → launch CAO terminal with
  the right profile + cwd → deliver assignment.
- Wake: event matching a parked agent's blocker → resume (see 5.6).
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
- **bootstrap**: no GitHub surface. Task lifecycle: Linear issue → agent works
  in workspace → rebase onto main → fast-forward main locally → issue done.
  Optional self-review stage (reviewer profile) before merge.
- **full**: agent pushes branch → opens PR (GitHub MCP) → reviewer
  agent/human reviews → merge via queue with rebase semantics.
- Same pipeline, stages toggled per project in the manifest. Bootstrap mode
  must produce history clean enough to graduate the repo to full mode.

### 5.6 Blocked tracking + park/resume
- Agent hits an external dependency → calls foregent MCP `report_blocked`
  with a typed blocker (`pr-review:binius64#123`, `issue-update:BIN-42`) →
  bridge records blocker, terminates the CAO terminal, frees the workspace
  slot (release-and-re-dispatch).
- Wake on matching webhook: re-acquire the workspace (resume is pinned to the
  original workspace — Claude sessions are per-cwd), relaunch with session
  resume so context is restored.
- Mechanism (CAO offers nothing here — confirmed: no `--resume`/session-id
  code anywhere in `providers/`; `terminal restore` recreates a plain shell
  with scrollback, not a provider session):
  - **Capture**: a SessionStart hook in the workspace's `.claude` settings
    writes the Claude session ID to `<workspace>/.foregent/session-id`; the
    bridge reads it on `report_blocked`.
  - **Resume without forking**: CAO builds the `claude` command itself with
    no extra-args hook, so wake-launches go through a thin `claude` PATH shim
    that checks `<cwd>/.foregent/resume-session` (written by the bridge just
    before the wake launch, consumed by the shim) and appends
    `--resume <id>` when present. Resume stays pinned to the original
    workspace cwd, so the marker-file handshake is race-free per workspace.
  - Fallback if the shim is too cute: a ~5-line contained patch to
    `_build_claude_command` adding an extra-args env passthrough (small,
    rebaseable divergence — not a fork of substance).
- Crash detection is also ours: on the tmux backend a dead `claude` process
  degrades to status `UNKNOWN` forever (Claude's provider never emits
  `ERROR`), so the bridge runs its own liveness probe as the exit authority.
  The herdr backend emits real `pane.closed` events and is the structural
  fix — one more reason herdr is the default (§5.10).

### 5.7 Workspace manager
- Unit: **workspace = directory containing one checkout of each project
  repo** (multi-repo native; single-repo is the degenerate case).
- Pool of N workspaces per machine; issue-keyed acquisition; resumes pinned
  to their original workspace.
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
- Skills: manifest-declared skill set synced into each repo's
  `.claude/skills/` (ancestor discovery) — refreshed on provision and before
  each dispatch. Note: CAO has its **own** parallel skills subsystem (catalog
  injected into the system prompt + a `load_skill` MCP tool) that is
  unrelated to Claude Code's native discovery; we skip it entirely and rely
  on native `.claude/skills/`, which works because CAO passes only
  `--append-system-prompt-file`/`--mcp-config` and leaves cwd discovery
  alone. Verify interop in phase 1.

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

## 6. What we deliberately reuse from the first generation (design, not code)

- Issue-keyed, manager-driven dispatch; release-and-re-dispatch for blocked
  agents (no long-poll, no parked processes).
- Resume pinned to original workspace; queue drains without head-of-line
  blocking.
- Manager exit authority as the single source of truth on agent death
  (reimplemented as the bridge's liveness probe / herdr `pane.closed`).
- The worker skill owning lifecycle; project skills owning workflow.

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
   Also the validation spike, run on the box: inbox messaging, status
   transitions, kill-the-process crash behavior (expect `UNKNOWN` on tmux /
   `pane.closed` on herdr), SessionStart-hook session-ID capture → PATH-shim
   `--resume` relaunch in the same cwd, native `.claude/skills/` discovery
   under a CAO-built launch command, SSE stream (`CAO_MCP_APPS_ENABLED`).
2. **Bridge core**: FastAPI service, state store, CAO REST client, foregent
   MCP server (get_assignment / report_blocked / complete_task), manual
   dispatch via CLI. Single repo, bootstrap mode, no webhooks yet. From here
   on, foregent development itself runs through the bridge.
3. **Linear loop**: webhook ingestion + task-manager profile + tick. Foregent
   development driven from Linear end-to-end in bootstrap mode.
4. **Workspaces**: pool, jj-or-git decision executed (Q6), multi-repo layout,
   sccache wiring.
5. **GitHub full mode**: PR flow, reviewer profile, review-comment monitor,
   park/resume on PR blockers. Foregent graduates from bootstrap to full mode
   on its own repo.
6. **Provisioning generalization + hardening**: manifest + cloud-init for
   cloud VMs; binius onboarded (multi-repo, cryptographer profile); crash
   recovery, missed-webhook reconciliation, cost/usage tracking.

## 8. Risks

- **CAO control-surface gaps** force more vendoring than planned → mitigated
  by the phase-1 validation spike before the bridge is built.
- **CAO is ~1 year old, small team** → could stall or pivot; mitigated by
  bridge-owns-state design (CAO swappable; option C vendoring path).
- **herdr is young and solo-maintained** → tmux fallback is a settings flip;
  the CAO backend ABC keeps us backend-agnostic.
- **Session resume fragility** (Claude session ids, per-cwd coupling) → the
  one first-generation subsystem we must rebuild carefully; spiked in
  phase 1.
- **Multi-repo + rebase automation** has sharp edges (cross-repo atomic
  changes, conflict handling) → start with binius' real dependency shape,
  keep cross-repo tasks single-owner.
- **Webhook exposure** of per-project VMs → prefer a relay (single hardened
  ingress fanning out to VMs) over N public endpoints (Q8).

## 9. Open questions

**CAO internals — resolved by the 2026-07-10 investigation (CAO @ `45636f8`):**
- **Q1 — Session resume: absent.** No `--resume`/`--continue`/session-id
  capture anywhere in `providers/`; `terminal restore` = plain shell +
  scrollback replay. The workflow-journal "resume" the team is building is
  step-DAG replay with fresh terminals, not conversational continuity.
  → We own it (hook capture + PATH shim; §5.6).
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
