# auto-maintainer design

Status: v0.4, 2026-11-05. Owner: `andylin-hao`. The first implementation exists; section 11 records what it covers.

auto-maintainer is a long-running service that helps the maintainers of GitHub repositories keep up with their projects. It watches pull requests, issues, and CI. It drives the coding agents installed on its host (Claude Code, Codex, Antigravity, OpenCode, pi, dsh, Gemini CLI) to review, score, and diagnose, and it acts on a repository only within limits its maintainers set. Maintainers configure it, approve its proposals, and talk to it through a web dashboard. What it learns, and its own code, live in GitHub repositories, so every change to its behavior has a commit, an author, and a reason.

Nothing in the core knows about a particular repository. Each repository is described by a **repo profile**: its contribution rules, CI model, review process, labels, priority rubric, test labs, and languages. A profile is assembled from reusable **packs** (Conventional Commits, DCO, GitHub Actions, CODEOWNERS, GPU labs, and so on) plus settings of its own, and the service drafts the profile itself when a repository is added. A repository's configuration belongs to its maintainers: only they can change it, and the service acts only on a profile they approved.

In short, the service is a control loop. Sources turn repository activity, schedules, and maintainer input into events. The engine runs a playbook for each event; the playbook reads state and the repo's profile, asks an agent for judgment where code cannot decide, and returns actions. A policy engine decides whether each action runs, waits for a maintainer's approval, or is only recorded, and a single actuator performs the ones that run. How maintainers respond flows back as learning signals into a Git-hosted memory that shapes later decisions. The code is split into a pure domain core, adapters for the forge, agents, Git, labs, and storage, an application layer of playbooks, policy, profiles, and actuator, and delivery through the dashboard, the CLI, and an MCP server for agents.

This document is the contract for the first implementation, and it is self-contained: every rule and model the service uses is defined here. Section 2 explains how the service adapts to a repository and who may configure it, and section 3 lists its features. Section 4 explains how the system is structured: principles, the control loop, core abstractions, layers, components, runtime and deployment views, state ownership, failure handling, and the decisions behind them. Section 5 describes each module and section 6 walks through the main flows. Sections 7 to 12 cover storage, configuration, security, testing, milestones, and open questions, and Appendix A works through the profile of a repository with demanding conventions.

## 1. Goals and non-goals

Goals:

- Serve any GitHub repository through configuration. Adding a repository takes a profile, not code.
- Put every agent behind one interface, so a review can move from Claude Code to Codex by editing config.
- Take over routine work a maintainer should not have to think about: rerunning flaky CI, merging approved PRs once checks pass, labeling, hygiene checks, nudging stale PRs.
- Keep humans accountable. A maintainer can see why the service did something, undo it where the forge allows, and change the rule that caused it.
- Learn from maintainers' corrections in each repository, and share what generalizes (flake signatures, review lessons) across repositories with a maintainer's approval.
- Keep each repository's configuration in its maintainers' hands. Nobody else, including the people who run the service, can change how it behaves on a repository.

Non-goals:

- Approving PRs. The service can recommend that a PR is ready; only a human approval counts toward a merge.
- Committing to target repositories on its own. Many projects require a human author or a human sign-off (DCO), so the service's own account never pushes to a repository it maintains. A maintainer can have an agent change a PR's code and open that change as a PR into the PR's branch under their own account; the push is theirs, signed off by them (5.5).
- Running CI. The repository's CI stays as it is; the service reads it and reruns it.
- Forges other than GitHub in the first version. The ports are named so that a GitLab adapter can be added later (4.4).
- Running the service itself on several hosts. One host runs it; test machines ("labs") are reached over SSH.

## 2. Adapting to a repository

### 2.1 What varies between repositories

The core handles what every maintained repository has in common: PRs with reviews and checks, issues, labels, CI that sometimes flakes, and maintainers with limited time. Everything else is a setting. A profile groups those settings by concern:

| Concern | What a repository decides | Examples |
|---|---|---|
| Scope | which PRs and issues the service handles | minimum PR number, drafts, bot authors, base branches |
| People | who maintains what and who counts as the team | `CODEOWNERS` path, team members (an org team or a list), review identities |
| Access | how much permission changing the configuration takes (2.3) | `maintain` (default), `admin`, or `write` |
| Contribution rules | what a well-formed PR looks like | commit convention, PR title rule, sign-off, PR template sections, license header |
| Checks | which hygiene checks run and which ones block a merge | built-in, command, and agent checks (2.4) |
| Review | how a review is done and written | review skill, output template, recommendation values, verification pass, style guide |
| Labels | the taxonomy and what each label means | keyword and path rules, role labels |
| CI | which checks matter and how CI starts | provider, required workflows, gate label, whether a cancelled run counts as failed, rerun budget, scarce runner groups |
| Merge | when and how to merge | who may arm a merge, method, message template, preserved trailers, freeze windows |
| Priority | how PRs are ranked | rubric dimensions and weights, tiers, stage goals |
| Timing | response expectations | business-day calendar, first-response and re-review SLAs, stale rules |
| Labs | where PR code may run | hosts, test command templates, environment selection, safety rules |
| Language and confidentiality | what goes public, in which language | public and internal languages, internal entity names |

**Role labels.** Playbooks never name a label. They emit a role, and the profile maps each role to one of the repository's labels. A role without a mapping is skipped.

| Role | Meaning | Example label |
|---|---|---|
| `needs_review` | waiting for a maintainer | `needs review` |
| `changes_requested` | the author has work to do | `waiting on author` |
| `ci_gate` | allows the full CI run | `run-ci` |
| `reproduced` | verified on a lab | `reproduced` |
| `needs_docs` | user-facing change without docs | `needs-docs` |
| `community` | external contributor | `community` |
| `automerge` | merge armed | `automerge` |

### 2.2 Profiles, packs, and layering

A profile is YAML validated against the `RepoProfile` schema in `domain`. It is merged from layers, each overriding the ones before it:

1. **Service defaults** (`config/defaults/profile.yaml` in the code repo): safe values for any repository.
2. **Packs** named under `extends:`. A pack bundles the settings, checks, label rules, flake signatures, and skills for one convention. Built-in packs: `conventional-commits`, `dco`, `codeowners`, `github-actions`, `gha-paths-filter`, `python`, `docs-bilingual`, `gpu-lab`. They live in `packs/<name>/` in the code repo; a deployment can add its own in the memory repo.
3. **In-repo file** (optional): `.github/auto-maintainer.yml` in the target repository, read from its default branch. It holds public conventions (scope, labels, checks, CI, merge, review template) that the repository's maintainers change through ordinary PRs. It can also set **ceilings**: the highest autonomy any action may reach in this repository. Later layers cannot raise an action above its ceiling.
4. **Memory profile**: `repos/<owner>/<repo>/profile.yaml` in the private memory repo. It holds private settings (rubric, internal entities, autonomy, routing) and anything the repository does not want to publish.
5. **Host overrides** in `local.yaml`: host paths for labs and mirrors. The schema rejects any other key, so the people who run the host cannot change behavior through this layer.

`am profile show <owner>/<repo>` prints the merged profile and the layer each value came from.

**The approved profile.** The service never acts on a freshly merged profile. It acts on the repository's lock, `repos/<owner>/<repo>/profile.lock.yaml` in the memory repo: the fully merged profile with the hash of every layer it came from. When any layer changes, the service computes a candidate, shows its difference from the lock on the dashboard, and replaces the lock only when the change passes the configuration gate (2.3). Until then the repository keeps running on the old lock, which also protects it from a layer that stops validating.

A minimal profile for a typical repository:

```yaml
# memory repo: repos/acme/widget/profile.yaml
schema: 1
repo: acme/widget
extends: [conventional-commits, codeowners, github-actions]
scope: {base_branches: [main], ignore_authors: ["dependabot[bot]"]}
labels:
  roles: {needs_review: "needs review", changes_requested: "waiting on author"}
ci:
  required_workflows: ["CI"]
merge:
  method: squash
  arming: {who: codeowners}
review:
  skill: builtin:pr-review
languages: {public: en, internal: en}
```

### 2.3 Who can change configuration

A repository's configuration is everything that decides how the service behaves on it: every layer of its profile and the approved lock, which together cover scope, checks, review, labels, CI, merge, rubric, labs, languages, policy, and routing. Only the repository's maintainers can change it, whichever way the change arrives:

| Source of a change | How it is applied |
|---|---|
| Dashboard, chat, slash command, `am` CLI | The configuration gate checks the user's permission on that repository with the forge at the moment of the change. A user who qualifies approves the change by making it; anyone else can only propose it. |
| In-repo file | The change reaches the default branch through the repository's own review and branch protection. The lock follows when the user who merged it meets `access.configure`; otherwise a maintainer approves the candidate. |
| Memory repo PR | Only the service can push to the memory repo's default branch. The memory gate merges a PR only after a maintainer of each repository whose directory it touches has approved it, as a PR review or on the dashboard. Proposals from the learner, onboarding, and drift detection take the same path. |
| New release of the service | Changed defaults or packs that alter a repository's merged profile become a candidate for that repository, which keeps running on its current lock until its maintainers approve. Defaults never set an action that touches a repository above `shadow`. |
| Host overrides | Limited by schema to host paths; any other key is rejected. |
| Service settings (8.1) | The same gate, applied to the service's own repository (`code_repo`, else the memory repository): a live check that the user has `self_update.approve_permission` there (`maintain` by default). A change arrives from the Settings page, from a conversation in which the agent drafts it, or as a memory PR; it applies when such a maintainer makes or approves it. Operators of the host have no say here unless they are also such maintainers. |

- **Maintainers** of a repository are the users whose permission on it is at least `access.configure`: `maintain` by default. A profile can raise the bar to `admin`, or lower it to `write` for a team that treats everyone who can push as a maintainer; it can never go below `write`. Changing the bar is itself a configuration change, so only the current maintainers can make it.
- **Collaborators with write access** below the bar can use the dashboard for that repository, approve proposals, and arm merges where `merge.arming` allows it, but they cannot change its configuration.
- **Learned knowledge** (T1, 5.11) is not configuration. It never edits a profile layer, the rubric's dimensions and weights, or the policy, and it merges without a review only when the repository's configuration allows it and the change comes from that repository's own signals.
- **Maintainers of the service's repository** own the service settings (8.1): which agents run and on which model providers, their budgets, the default route per task, and system values such as the polling interval and the public write caps. They set API keys, which stay on the host.
- **Operators**, the people who run the service's host, start it and complete its first-run setup on the web (7.3), which needs the one-time code printed on the host. After that, the host setup (the address, the service's GitHub account, sign-in, and the memory repository) changes on the dashboard through the service gate, like the service settings. Operators also manage lab hosts, host paths, and maintenance schedules (8.1). They cannot change any repository's configuration or the service settings unless they are also maintainers of the repository concerned. They can pause the service; stopping never needs a repository's approval.
- **Everyone else**, including maintainers of other repositories in the same deployment, contributors, and the service itself, can propose a change but never apply it. When the service acts as a person's own GitHub account (the GitHub CLI's login on the host), that person still configures it after signing in to the dashboard or from the CLI; what the forge reports that account did, such as landing the in-repo file, never counts as an approval.

### 2.4 Extension points

When a repository needs behavior rather than values, its profile points at one of these. None needs a change to the core.

| Extension point | Forms | Example |
|---|---|---|
| Hygiene check | built-in (`title_regex`, `commit_convention`, `signoff`, `template_sections`, `codeowners_coverage`, `license_header`); `command` (a script from the target repo's default branch, run in a sandbox against the PR's data); `agent` (a prompt that an agent judges) | the repository's own PR-title linter as a command check; parity between two language versions of the docs as an agent check |
| Review skill | a built-in skill, or a skill path in the target repo read from its default branch | `repo:.agents/skills/review/SKILL.md` |
| Review template | ordered sections the main review comment must contain, and the allowed recommendations | a fixed list of questions every review must answer |
| Rubric | dimensions with weights and scoring rules, tier thresholds, stage goals | the default rubric (5.6), or dimensions of the repository's own |
| Label rules | keyword and path patterns mapped to labels | domain and component labels |
| Flake signatures | regexes scoped by workflow, job, and step | shared ones from packs, learned ones per repository |
| Lab test templates | named commands with environment selection | unit tests on CPU, end-to-end runs on a GPU host |
| Lab guard rules | forbidden command patterns and protected paths | no command that stops other users' jobs on a shared host, no writes to shared caches |
| CI provider | an adapter for logs and reruns; check results are read from the forge for any provider | GitHub Actions |

Scripts and skills taken from the target repository always come from its default branch, never from the PR head. Otherwise a PR could change the instructions it is reviewed with (section 9).

### 2.5 Onboarding a repository

1. A maintainer of the repository gives the service's GitHub account access to it (write, to post reviews) and runs `am repo add <owner>/<repo>` or uses the dashboard. An operator can start onboarding too, but the repository stays inactive until step 4.
2. The `onboard` playbook reads the repository: `CONTRIBUTING*`, `AGENTS.md`, `CLAUDE.md`, the PR and issue templates, `.github/workflows/`, `CODEOWNERS`, the label list, branch protection, existing agent skills, and the last 50 merged PRs (title shapes, trailers, labels, review habits).
3. An agent drafts a profile: packs to extend, the role-label mapping, checks, CI and merge settings, a starter rubric, and the questions it could not answer. The deterministic **profile doctor** then checks the draft against the live repository: labels exist, workflows exist, skill paths resolve, command checks run.
4. The draft and its first lock go out as a memory PR. Maintainers of the repository edit and approve it (2.3); until then the service only reads the repository.
5. The repository starts in shadow mode. The dashboard shows agreement scores per action kind, and maintainers raise autonomy when the scores justify it.
6. From then on, the `profile_drift` playbook watches the files from step 2. When `CONTRIBUTING.md`, a workflow, or the label set changes, it proposes the matching profile change.

A repository is supported when it can go through these steps without a code change. Milestone M2 tests this with a second repository whose conventions differ from the first one's (section 11).

## 3. Features

Every feature works on any repository; the profile decides its details. Each row names its default autonomy after the shadow period (5.8) and its milestone (section 11).

### 3.1 Requested features

| Feature | What it does | Default | Milestone |
|---|---|---|---|
| PR scan and triage | Lists open PRs in scope, classifies review state (`NEW_REVIEW`, `FOLLOWUP_REVIEW`, `APPROVED_NEW_COMMITS`, `UP_TO_DATE`, `SELF_PR`), writes a one-sentence summary, suggests labels from the profile's rules. | labels: `propose`, then `auto` | M1 |
| First review | Runs the profile's review skill in a worktree at the PR head, verifies each finding with a second pass, and drafts a review in the profile's template: a main comment plus inline comments with suggested fixes. By default it runs only when a reviewer selects PRs (dashboard queue, `/am review`, chat); the draft goes to that reviewer's inbox to edit and post. `review.trigger: scan` reviews a bounded batch on a schedule; `review.trigger: events` reviews as soon as a PR opens. | requested: draft for the reviewer; scan and events: `propose` | M1 |
| Follow-up review | When a reviewed PR has new commits and a reviewer selects it (or the scan or events trigger picks it), checks every earlier finding, marks fixed ones resolved, carries open ones forward, and adds findings introduced since the last reviewed SHA. | as for first reviews | M1 |
| PR value evaluation | Scores each PR on the profile's rubric, adds stage-goal boosts (capped), assigns tiers, and shows a private ranked queue. | internal only | M1 |
| Priority calibration | A maintainer override needs a reason. A calibration agent decides whether it is a one-off, a stage-goal change, a rubric change, or needs a clarifying question, and checks it for oscillation against recent overrides. | memory tiers (5.11) | M1 |
| Agent drivers | Runs Claude Code, Codex, Antigravity, OpenCode, pi, dsh, and Gemini CLI through one runtime interface with routing, fallback, budgets, and health probes. | n/a | M0 to M4 |
| Dashboard | Web UI for the review queue, PR detail, proposal inbox, CI health, memory, agents, settings, profiles, and the audit log, across all repositories the signed-in user maintains. | n/a | M0, M1 |
| Talk to it | Per-maintainer chat threads backed by an agent session with the service's tools, and slash commands (`/am ...`) in PR comments. | proposals inline | M1 (read), M3 (act) |
| Configurable behavior | A profile per repository, changed only by that repository's maintainers (2.3). Dashboard edits become memory PRs that the service merges after their approval. The service's own settings belong to the maintainers of its repository. | n/a | M1 |
| Growing memory on GitHub | Learns from overrides, draft edits, rejected proposals, CI outcomes, and chat; writes per-repository and global knowledge to a private memory repo through tiered commits and PRs. | tiered | M3 |
| Self-evolving code | Turns reviewers' requests in chat into PRs against its own code, with tests; a maintainer of its repository approves the merge and, separately, the deploy, which tests the new commit, switches, restarts, and rolls back if it fails to start (5.12). | maintainer approval | built |
| Auto-merge | A maintainer arms a merge; the service waits for checks, reruns flaky jobs, updates the branch if needed, and merges when the profile's merge gate holds (5.5). | armed by a human | M2 |
| Flaky CI rerun | Classifies failed jobs by signature or agent diagnosis, reruns only the failed jobs within a budget, and explains real failures to the author. | rerun: `auto` | M2 |

### 3.2 Additional features

| Feature | Why it is worth having | Default | Milestone |
|---|---|---|---|
| Repository onboarding | Drafts a profile from the repository's own files and history, checks it against the live repository, and starts the repository in shadow mode once its maintainers approve the profile (2.5). | maintainer approval | M1 |
| Profile drift | Proposes profile changes when the repository's contribution guide, workflows, or labels change, so the profile does not rot. | maintainer approval | M2 |
| Shadow mode and agreement score | Runs everything without writing and compares what it would have done with what maintainers did. Autonomy for an action kind rises only after its agreement passes a threshold. | on for new kinds and repos | M0 |
| Proposal inbox with edit-before-approve | Maintainers fix a draft instead of rejecting it; the edit is the strongest signal for review quality. | n/a | M1 |
| Hygiene checks | The profile's checks in one comment per head SHA, updated in place: title, commits, sign-off, template, license headers, CI path coverage, anything a pack or command adds. Saves a review round. | `propose`, then `auto` | M2 |
| CI gate management | For repositories whose full CI starts on a label (the `ci_gate` role), proposes the label when a team member's PR is reviewed as ready, within a daily runner budget, and tells community authors why CI has not started. | `propose` | M2 |
| Reviewer routing and claims | Suggests reviewers from `CODEOWNERS` and current load. A maintainer can claim a PR on the dashboard so two people do not review it. | `propose` | M2 |
| SLA and stale tracking | Business-day timers for first response and re-review. Nudges reviewers first, then authors with requested changes; proposes closing abandoned drafts. | nudges `auto`, closing `propose` | M2 |
| Merge conflict notices | Detects conflicts with the base branch after other merges and tells the author once, naming the conflicting files. | `auto` | M2 |
| Overlap and duplicate detection | Finds PRs and issues working on the same thing (titles, files, summary similarity) and links them for maintainers. | internal | M2 |
| Runner health | Watches self-hosted runners (offline, queue time, disk) and tells maintainers instead of blaming authors for infrastructure failures. | notify | M2 |
| Flaky test report | Weekly flake rate per test and job; proposes a tracking issue for tests above a threshold. | `propose` | M2 |
| Lab testing | For repositories with a lab, runs the profile's test templates for a PR under the lab's safety rules and applies the `reproduced` role on success. External contributors' code runs only after approval. | team `auto`, external `propose` | M1 |
| Reproduction agent | A fresh agent session that follows only the PR's user docs on a lab and reports the first failure and every intervention. | `propose` | M3 |
| Cross-agent review | An optional second runtime reviews the same PR; a verifier keeps the findings that survive. Fewer false positives on large PRs. | per profile | M3 |
| Cross-repository knowledge | Flake signatures and review lessons that hold in several repositories are proposed for global memory, which a repository uses only if its maintainers opt in. Private repository content never crosses. | opt-in | M3 |
| Issue triage | Labels issues from the issue templates, asks for missing details, links docs, finds duplicates. | labels `auto`, comments `propose` | M4 |
| Digest | Daily digest per maintainer (dashboard and IM) in the internal language: new PRs, top of the queue, pending proposals, CI health, stale PRs. Weekly metrics: time to first review, time to merge, flake rate, agent cost. | `auto` | M3 |
| Release assistant | Drafts release notes from merged PRs grouped by label and checks that notable features are announced. | `propose` | M4 |
| Kill switch and anomaly pause | One switch stops all execution, globally or per repository. The service pauses itself on anomalies such as write bursts or repeated actuator errors. Proposals keep accumulating while paused. | on | M0 |
| Replay evals | Re-runs review, evaluation, and CI diagnosis on recorded historical cases and scores them against what maintainers decided. Prompt, skill, rubric, and pack changes must not lower the scores. | n/a | M4 |
| Cost and quota tracking | Usage per runtime, task kind, repository, and PR; daily budgets; routing to the next runtime when one runs out. | n/a | M2 |

## 4. System structure

This section shows how the service is put together; section 5 then describes each module. It starts with the principles and then looks at the system from several sides: the control loop it runs, the types that move through that loop, the layers the code is split into, the components and what each owns, the processes it runs as, where it is deployed, where its state lives, how it recovers from failures, and the decisions behind this structure.

### 4.1 Principles

1. **Repositories are data.** Core code never names a repository, label, workflow, path, or person of a target repository. All of that comes from the repository's profile, and a test fails if `src/` contains a name that belongs to a profile.
2. **Agents propose, the core acts.** Agents get read-only credentials and return structured results. Only the actuator writes to the forge, runs lab commands, or pushes to the memory and code repos, and only after the policy engine allows the action. A compromised or confused agent can produce a bad proposal; it cannot post one through the service. Agents run in their CLI's auto mode, and what they would ask a person goes to a reviewer (5.7); bypass mode is opt-in, and confining agents with `command_prefix` keeps the host's own credentials out of their reach.
3. **Every outward effect is an `Action` with an idempotency key.** A key such as `review.post:acme/widget#42@0ebdf9c` makes replays, restarts, and duplicate events harmless.
4. **State lives where its owner can review it.** Anything a maintainer should be able to review or revert is in Git; anything operational or rebuildable from the forge is in SQLite; secrets stay on the host (4.8).
5. **Code for control flow, agents for judgment.** Playbooks are plain Python state machines. They call an agent only for steps that need reading or writing prose or code: summaries, reviews, diagnoses, calibration, profile drafts.
6. **Public and internal are types.** Every text field the service produces carries a `Visibility`. Leaking a priority score into a PR comment is a type error at the actuator, not a reviewer's catch.
7. **Autonomy is earned per action kind and per repository.** New action kinds and new repositories start in `shadow`; maintainers raise them after the agreement score shows the service would have done what they did.
8. **Configuration belongs to the repository's maintainers.** The service acts on a repository only under a profile its maintainers approved, and every change to that profile, whatever its source, waits for their approval (2.3).

### 4.2 The control loop

The service is a control loop around each repository: it observes the repository and its maintainers, decides what to do, acts, and learns from how the result was received.

```
   ┌───────────┐  Event   ┌────────────────────┐  Action   ┌──────────────────┐
   │  OBSERVE  │─────────►│       DECIDE       │──────────►│       ACT        │
   │ poller    │          │ engine → Job       │           │ execute: actuator│
   │ webhooks  │          │ playbook reads     │           │ propose: inbox   │
   │ schedules │          │ state, asks agents │           │ shadow: audit    │
   │ /am, UI,  │          │ policy → Decision  │           │                  │
   │ chat      │          └─────────▲──────────┘           └───┬─────────┬────┘
   └─────▲─────┘                    │ new memory               │         │
         │                ┌─────────┴──────────┐   Signals     │         │
         │                │       LEARN        │◄──────────────┘         │
         │                │ Signal → learner → │                         │
         │                │ MemoryChange       │                         │
         │                └────────────────────┘                         │
         └─────────── GitHub changes, action.executed events ────────────┘
```

- **Observe.** Sources turn the outside world into `Event`s. The poller compares a fresh `PrSnapshot` with the stored one and emits an event for each difference. It reads only repositories with an approved profile: on the polling interval, and at once when a repository's profile lock changes, so a first approval brings in its open PRs without waiting for the next round. Webhooks, schedules, slash commands, the dashboard, and chat emit events directly.
- **Decide.** The engine maps each event to a `Job`: one playbook for one key, such as a PR at its head SHA. The playbook reads what it needs, including the repository's profile, asks agents for judgment where code cannot decide, and returns `Action`s. The policy engine gives each action a `Decision`.
- **Act.** The actuator executes allowed actions. Actions that need a human wait in the inbox as `Proposal`s; shadow actions are only recorded. The actuator reports every result back as an event, so a playbook waiting on it (a merge waiting for a rerun) continues.
- **Learn.** How maintainers react to the service's work (overrides, edits to drafts, rejections, findings fixed or dismissed) and how the world responds (reruns pass, PRs merge) become `Signal`s. The learner turns them into memory changes, and the next decision reads the new memory.

The loop runs at three speeds. The slower the loop, the more it changes and the stronger its human gate:

| Loop | Period | What it changes | Human gate |
|---|---|---|---|
| Fast | seconds to minutes | the repository: labels, comments, reruns, merges | policy per action kind: `shadow`, `propose`, `auto` |
| Slow | hours to weeks | memory: profiles, rubric, stage goals, label rules, flake signatures | memory tiers: journal, notice window, maintainer-merged PR |
| Slowest | weeks | code: playbooks, adapters, packs, skills, prompts | maintainer-merged PR after CI and replay evals, then a supervised deploy |

### 4.3 Core abstractions

Eleven types carry everything through the loop. They are defined in `domain` (5.1), and every module speaks in them.

| Type | Meaning | Produced by | Consumed by | Persisted in |
|---|---|---|---|---|
| `RepoProfile` | The approved, merged settings of one repository (2.2). | profiles | everything that acts on a repository | the lock in the memory repo, mirrored in `profiles` |
| `Event` | Something happened: a PR changed, a check finished, a maintainer clicked, a timer fired, an action ran. | sources, actuator, web | engine | the payload of the job it triggers |
| `Job` | One durable run of one playbook for one key. | engine | worker pools | `jobs` |
| `PrSnapshot` | A consistent read of a PR at one head SHA. | `ForgeReader` | playbooks, policy, actuator | `pr_snapshots` |
| `AgentTask`, `AgentResult` | A request for judgment and its validated answer. | playbooks, chat | agent router, runtimes | `agent_runs`, transcripts |
| `Action` | One intended outward effect, with an idempotency key and a visibility. | playbooks, MCP propose tools, dashboard | policy | `actions` |
| `Decision` | `execute`, `propose`, `shadow`, or `deny`, with the rule that decided it. | policy | actuator, inbox, audit | `actions` |
| `Proposal` | An action waiting for a human. | policy | dashboard, chat | `proposals` |
| `Signal` | A learning input. | playbooks, actuator, web | learner | `signals` |
| `MemoryChange` | An edit to the memory repo, with its tier. | learner, onboarding, settings page, chat | policy, actuator | memory repo |
| `Evaluation` | Internal priority scores and tier of a PR under its repository's rubric. | `evaluate`, `calibrate` | queue page, digest | `evaluations` |

**An action's life.** Whoever creates an action, it takes the same path:

```
 playbook · MCP propose tool · dashboard
                 │ Action
                 ▼
              Policy ──► deny ───────────────────────────────────────► audit
                 │  ───► shadow ─────────────────────────────────────► audit, agreement score
                 │  ───► propose ─► Proposal ─┬─► reject (reason) ───► audit, Signal
                 │                            ├─► superseded / expired
                 │                            └─► approve (payload may be edited)
                 │                                     │
                 ▼                                     ▼
              execute ◄──────────────────────── Policy again (pause, hard limits)
                 │
                 ▼
             Actuator: already done? → target still current? → record intent
                       → write → record result → audit → register undo
                 │
                 ▼
             Event action.executed / action.failed ──► engine ──► waiting jobs
```

An approved proposal passes through policy a second time, so an approval cannot bypass a pause, a hard limit, or a head SHA that moved on.

**A job's life.** A job is `queued`, `running`, `waiting`, `done`, or `failed`. A playbook never sleeps on the outside world. When it needs something to happen first (CI to finish, a rerun to pass, a proposal to be approved, a backoff to elapse), it returns `wait_for(events, timeout)`. The engine parks the job and runs it again when a matching event arrives or the timeout fires. Every run starts from fresh reads, including the current profile, so a playbook's result depends only on the current state, which is what makes jobs safe to restart at any point.

### 4.4 Layers

The code uses a ports-and-adapters structure. `domain` holds the types, the pure rules, and the interfaces to the outside world (ports). Adapters implement those ports for a specific technology. The application layer runs the workflows through ports only. Delivery exposes the application to people and to agents.

```
┌───────────────────────────────── delivery ─────────────────────────────────┐
│   web: dashboard, API, webhooks      cli: am      mcp: tools for agents    │
├─────────────────────────────── application ────────────────────────────────┤
│   engine: jobs, waits, pools   playbooks   policy   profiles   actuator    │
├────────────────────────────────── domain ──────────────────────────────────┤
│   types · RepoProfile · review state · scoring · merge gate · calendar     │
│   ports: ForgeReader, ForgeWriter, CiProvider, AgentRuntime, MemoryStore,  │
│          LabHost, Store, Notifier, Clock                                   │
├───────────────────────────────── adapters ─────────────────────────────────┤
│   github (forge, Actions)   agents (claude, codex, agy, opencode, pi, dsh) │
│   memory (git)      labs (ssh, local)     store (SQLite)     notify (IM)   │
└────────────────────────────────────────────────────────────────────────────┘
   Application and adapters both depend on domain, never on each other.
   app.py, the composition root, builds the adapters and injects them.
```

The ports are named after what they do, not after GitHub: `ForgeReader` and `ForgeWriter` for repositories, PRs, issues, and checks; `CiProvider` for logs and reruns. GitHub is the first and, in this version, only forge adapter, and GitHub Actions the first CI provider.

This structure gives five properties the service relies on:

- The rules that matter most (review state, scoring, the merge gate, profile merging) are pure functions and need no fakes to test.
- Playbooks run unchanged against `FakeForge`, `ScriptedAgentRuntime`, and any profile, which is how the unit tests and the replay evals work.
- A new agent CLI, IM, CI provider, or forge is a new adapter; no playbook changes.
- A new repository is a new profile; no code changes.
- Write power is a wiring decision. `app.py` hands `ForgeWriter`, `CiProvider` reruns, the lab executor, and the memory writer to the actuator and to nothing else.

### 4.5 Components

```
┌────────┐  webhooks, polling ┌─────────┐  Event  ┌────────────────────────┐ action.executed
│ GitHub │───────────────────►│ Sources │────────►│ Engine                 │◄─────────┐
└────▲───┘                    └────▲────┘         │ jobs · schedules       │          │
     │                             │              │ worker pools · waits   │          │
     │                   /am, dashboard, chat     └───────────┬────────────┘          │
     │                                                        │ runs job              │
     │                                                        ▼                       │
     │  ┌─────────────────────┐            ┌────────────────────────────────────┐     │
     │  │ Agent router        │◄─AgentTask─│ Playbooks                          │     │
     │  │ → runtimes:         │            │ reads: ForgeReader, MemoryStore,   │     │
     │  │ claude, codex, agy, │            │        LabHost, Store, profile     │     │
     │  │ opencode, pi, dsh,  │            └──────────────────┬─────────────────┘     │
     │  │ gemini              │                               │ Actions               │
     │  └─────────────────────┘                               ▼                       │
     │                                           ┌─────────────────────────┐          │
     │                                           │        Policy           │          │
     │                                           └──┬─────────┬─────────┬──┘          │
     │                                      execute │         │ propose │ shadow      │
     │                                              │         ▼         ▼             │
     │                                              │   ┌───────────┐ ┌─────────────┐ │
     │                                              │   │   Inbox   │ │ Audit log   │ │
     │                                              │   │(dashboard)│ └─────────────┘ │
     │                                              │   └─────┬─────┘                 │
     │                                              │         │ approve               │
     │                                              ▼         ▼                       │
     │                                        ┌─────────────────────┐                 │
     └─ writes ───────────────────────────────┤ Actuator            ├─────────────────┘
                                              └──────────┬──────────┘
                                                         ▼
                                     lab hosts (ssh) · memory repo · code repo
```

| Component | Responsibility | Owns | Depends on |
|---|---|---|---|
| Sources | Turn forge state, webhooks, schedules, and human input into events, per repository. | poll cursors, ETags, snapshots | `ForgeReader`, `Store`, `Clock` |
| Profiles | Merge profile layers, keep each repository's approved lock, and run the configuration gate, the doctor, and drift detection. | `profiles` table, locks | `ForgeReader` (in-repo file, permissions), `MemoryStore` |
| Engine | Map events to jobs, run them under concurrency limits, park and wake waiting jobs, fire schedules. | `jobs`, worker pools, timers | `Store`, `Clock` |
| Playbooks | One workflow each (5.5): read state and profile, ask agents, return actions, signals, and waits. | nothing persistent | `ForgeReader`, `CiProvider` reads, agent router, `MemoryStore` reads, `LabHost` reads, `Store` reads |
| Agent router and runtimes | Pick a runtime, run the task in a sandbox, normalize its events, validate its output, track budgets and health. | `agent_runs`, transcripts, circuit breakers | `AgentRuntime` adapters |
| Policy | Decide each action from the profile's policy and ceilings, conditions, counters, pause, and freezes; build `PublicText`. | nothing; pure over its inputs | `PolicyConfig` from the profile, counters from `Store` |
| Actuator | Execute allowed actions exactly once and report each result as an event. | `actions`, `audit_log`, undo handles | `ForgeWriter`, `CiProvider` reruns, `LabHost`, `MemoryStore` writes, `Notifier` |
| Memory store | Typed reads of the memory repo checkout; commits and PRs on the actuator's behalf. | the checkout | git, forge |
| Labs | Host access, resource leases, worktrees, command guard. | `leases` | SSH, local shell |
| Web | Auth, dashboard API, inbox, settings, chat sessions, SSE. | `users`, `chat_threads`, `chat_messages` | application layer |
| MCP server | Tools that agents call. Read tools go to the readers; propose tools create actions on the same path as playbooks. | per-run tool scopes | application layer |
| Supervisor | Deploy new code, hot-reload memory, roll back. | `releases/`, database backups | git, `Store` |

### 4.6 Runtime view

```
service host
├── am supervisor                 separate process (systemd unit): deploys and restarts am serve
└── am serve                      one asyncio process
    ├── HTTP server (uvicorn)     API, SSE, webhooks, built UI, MCP endpoint on 127.0.0.1
    ├── poller                    one task per repository
    ├── scheduler
    ├── engine dispatcher
    │   ├── io pool               forge reads (8 slots)
    │   ├── agent pools           one per runtime (claude-code 3, codex 2, antigravity 2, ...)
    │   └── lab pools             one per lab host, sized by its leasable resources
    ├── actuator                  one ordered queue per target repository
    └── agent subprocesses        claude -p, codex exec, agy -p or agy acp, ...
                                  each in its own worktree with an allowlisted environment
                                  ─── ssh (one shared connection per host) ───► lab hosts
```

- **Pools isolate kinds of work.** A backlog of reviews cannot delay a CI rerun, which needs only the io pool and the actuator.
- **The actuator serializes writes per repository.** Two playbooks acting on the same PR cannot race, rate limits are counted in one place, and the order of writes on a PR matches the order of decisions.
- **Agents call back over MCP with a per-run token.** The token tells the service which run, repository, and chat thread a tool call belongs to, and which tools that task kind may use.
- **SQLite runs in WAL mode with one writer connection**; reads use a small pool.
- **One process is enough.** A deployment serving a handful of repositories sees a few hundred open PRs and tens to hundreds of agent runs a day, with the heavy work in agent subprocesses. Because jobs live in the database, the worker pools can move to separate processes later without touching playbooks.

### 4.7 Deployment view

```
 ┌────────────────────────────────── GitHub ──────────────────────────────────┐
 │  target repositories   auto-maintainer (code)   auto-maintainer-memory     │
 │                                                 (private)                  │
 └───────▲──────────────────────────▲──────────────────────────▲──────────────┘
         │ REST, GraphQL            │ git fetch, push, PRs     │ webhooks (optional)
         │ (gh token)               │                          │
 ┌───────┴──────────────────────────┴──────────────────────────┴──────────────┐
 │  service host                                                              │
 │  am supervisor · am serve · agent CLIs (claude, codex, agy, opencode, ...) │
 └───────▲─────────────────────────────────────────┬──────────────────────────┘
         │ HTTPS through a TLS reverse proxy       │ ssh, template commands only
         │                                         ▼
   maintainers' browsers                        lab hosts: worktrees,
                                                environments, GPUs
```

Files on the service host:

```
~/.config/auto-maintainer/
├── local.yaml                 port, data directory, proxy, lab hosts, host paths
└── secrets.env                mode 0600
<data_dir>/                    default ~/.local/share/auto-maintainer
├── am.db                      SQLite (WAL); backups/ beside it
├── releases/<sha>/            code checkouts; current -> releases/<sha>
├── memory/                    memory repo clone
├── mirrors/<owner>__<repo>.git  bare mirror per target repository; agent worktrees come from it
├── worktrees/<run-id>/        one per agent run, removed afterwards
└── runs/<run-id>/             prompt, transcript, structured output
```

Agent worktrees come from mirrors the service owns, never from clones that people use. Lab worktrees follow each lab's own paths from its profile. The dashboard listens on every interface by default (`host: 0.0.0.0`, port 7777) and serves plain HTTP. `base_url` is the address browsers use and builds the GitHub sign-in callback. Beyond a trusted network, put a TLS reverse proxy in front, set `base_url` to its HTTPS address and `cookie_secure: true`, or bind `host: 127.0.0.1` behind the proxy.

### 4.8 State and ownership

| Home | Holds | Written by | If the service host is lost |
|---|---|---|---|
| Target repositories (forge) | PR and issue state; the service's comments with their markers; optional in-repo profile files | everyone; the actuator for the service | nothing lost; review state is rebuilt from the markers |
| Memory repo | service settings, repository profiles and locks, rubrics, knowledge, journals (including every override) | only the service pushes; every other change arrives as a PR and passes the memory gate (2.3) | nothing lost |
| Code repo | code, built-in packs, runtime skills, default config | maintainers; `self_improve` PRs | nothing lost |
| SQLite | jobs, runs, snapshots, evaluations, proposals, chat, audit, profile cache | engine, actuator, web, profiles | restored from the nightly backup; snapshots are polled again |
| Host config | secrets, paths, port | the admin | provisioned again by hand |

The rule behind this split: what a maintainer should be able to review or revert lives in Git, what is operational or can be rebuilt from the forge lives in SQLite, and secrets never leave the host.

### 4.9 Package layout

```
auto-maintainer/
├── AGENTS.md                  instructions for coding agents; CLAUDE.md links here
├── docs/design.md             this document
├── pyproject.toml             uv project; Ruff and pytest config
├── config/defaults/           default profile (every action in shadow), routing, schedules
├── packs/                     built-in packs: settings, checks, label rules, signatures, skills
│   ├── conventional-commits/  dco/  codeowners/  github-actions/  gha-paths-filter/
│   └── python/  docs-bilingual/  gpu-lab/
├── skills/                    skills the service gives its runtime agents
│   ├── pr-review/             default review skill for repositories without their own
│   ├── pr-triage/             summary, labels, overlap
│   ├── pr-followup/           resolved and open findings
│   ├── pr-priority/           rubric scoring and calibration
│   ├── ci-diagnose/           failed job classification
│   ├── repo-onboard/          profile drafting from a repository's files and history
│   └── maintainer-chat/       chat persona, tool use, memory etiquette
├── src/auto_maintainer/
│   ├── domain/                types, RepoProfile schema, pure rules (review state, scoring, merge gate, markers, CODEOWNERS, calendar), ports
│   ├── service.py             application wiring over a set of adapters; imports no adapter
│   ├── app.py                 composition root: builds the real adapters and hands them to service.py
│   ├── subproc.py             async subprocesses with timeouts, for adapters
│   ├── store/                 SQLite adapter: SQLAlchemy Core tables, Alembic migrations
│   ├── github/                forge adapter (REST, GraphQL, App and token auth, webhooks) and Actions CI provider
│   ├── agents/                runtime adapters, exec transport, structured output, router, sandbox
│   ├── memory/                git-backed MemoryStore: clone, typed reads, branches, merges
│   ├── workspace/             bare mirrors and worktrees for agent runs; the check-script sandbox
│   ├── labs/                  local and SSH hosts, GPU leases, lab executor, command guard
│   ├── notify/                Slack and Feishu/Lark webhooks, log channel
│   ├── profiles/              layer merging, packs, locks, configuration gate, knowledge, checks, doctor
│   ├── policy/                autonomy rules, ceilings, hard limits, leak guard
│   ├── playbooks/             one module per playbook, prompt helpers, the playbook contract
│   ├── actuator/              action pipeline, executor, one handler per action kind, memory gate
│   ├── engine/                event bus, job runner, waits, poller, scheduler, worker pools
│   ├── web/                   FastAPI app, sign-in, routes, SSE, chat
│   ├── testing/               fakes of every port, a full-service harness, the demo data set
│   ├── mcp/                   (planned, M3) MCP server exposing read tools and propose tools
│   ├── selfupdate/            (planned, M4) supervisor, deploy, rollback
│   └── cli.py                 `am` entry point
├── ui/                        React + TypeScript + Vite dashboard, built into web/static
├── evals/                     recorded cases and scorers, grouped by repository and playbook
└── tests/
```

Import rules, enforced by `tests/test_layers.py`:

- `domain` imports nothing from the project.
- Adapters (`store`, `github`, `agents`, `memory`, `workspace`, `labs`, `notify`) import only `domain` and `subproc`.
- `policy` and `profiles` import only `domain`.
- `playbooks` import `domain`, `policy`, and `profiles`; never `actuator` and never an adapter. They get ports from their context, and the context has no `ForgeWriter`.
- `actuator` imports `domain`, `policy`, and `profiles`.
- `engine` also imports `playbooks` (to run them) and `actuator` (to submit their actions).
- `web` imports the application layer, `service`, and `domain`; the forge's webhook verifier is injected by `cli.py`.
- `app.py` and `cli.py` are the only modules that import adapters.

`tests/test_layers.py` also fails if `src/` outside `testing/` names a repository that appears in the tests or evals, if any module but the leak guard touches `GUARD_KEY`, if a playbook mentions `ForgeWriter`, or if the default profile lets any repository action leave shadow.

Stack: Python 3.11+ via uv, asyncio, Pydantic v2, FastAPI with uvicorn, SQLAlchemy 2 on SQLite (WAL) with Alembic, httpx, Typer, the official `mcp` SDK, `croniter`. UI: React, TypeScript, Vite, TanStack Query, server-sent events for live updates.

### 4.10 Failure handling

| Failure | What happens |
|---|---|
| The service crashes or restarts during a job | The job returns to the queue when the service starts again (job store, 5.4) and runs again from fresh reads. Idempotency keys stop repeated writes. An agent task resumes its session when the runtime supports it and restarts otherwise. |
| The service crashes between a forge write and recording it | The actuator records an intent before each write. On startup, an intent without a result is reconciled by looking for the action's marker or reading the target's state, and it is retried only if the write did not happen. |
| The forge is down or rate-limited | Polling backs off, the actuator holds its queue, jobs wait, and the dashboard shows a banner. |
| An agent CLI fails, runs out of quota, or returns invalid output | Invalid output gets one repair turn; after that, the router moves to the fallback runtime and opens the failing runtime's circuit breaker. A job that runs out of runtimes fails and shows on the dashboard. |
| A profile layer becomes invalid | The repository keeps running on its lock. The candidate is flagged on the dashboard, cannot be approved until it validates, and the service proposes a fix. |
| A lab is unreachable or has no free resources | Lab jobs wait up to a deadline; after it, the review reports that tests were deferred. |
| A memory push conflicts with a maintainer's edit | The actuator rebases and retries. On a real conflict the maintainer's version wins and the learner runs again on top of it. |
| A deploy is bad | The supervisor rolls back the code and the database (5.12). |
| The service misbehaves | The public write cap pauses it automatically; any maintainer can pause it, globally or for one repository, from the dashboard or with `am pause`. |

### 4.11 Design decisions

| Decision | Alternative considered | Why |
|---|---|---|
| Repository behavior is a layered profile built from packs. | Per-repository code plugins, or a fork of the service per repository. | Profiles are reviewable, can be drafted by an agent during onboarding, and need no deploy. Code is only for new kinds of extension point. |
| The in-repo profile file is optional. | Require `.github/auto-maintainer.yml` in every repository. | Repositories that will not commit a file still work; those that do keep public conventions next to the code they describe and reviewed by their own maintainers. |
| The service acts only on an approved profile lock, and every change to it needs the repository's maintainers. | Apply profile changes as soon as a layer changes, and let deployment admins configure any repository. | A service release, a shared pack, or the maintainers of another repository cannot change how the service behaves on a repository behind its maintainers' backs. The lock also keeps a repository running when a new layer stops validating. |
| Playbooks in code call agents for single judgments. | One agent session follows a large skill from start to finish. | Control flow in code is testable, resumable, cheaper, and safe to run unattended. A single long session works only when a maintainer watches each run. |
| Only the actuator writes; agents are read-only. | Agents call `gh` to post and label. | One place enforces policy, idempotency, the leak guard, and the audit log. A prompt injection inside a PR cannot post anything. |
| Polling is the source of truth; webhooks only trigger polls. | Webhooks alone. | The host may not be reachable from the forge, and webhook deliveries can be lost. Reconciliation makes correctness independent of delivery. |
| SQLite inside one process. | PostgreSQL and separate workers. | The load is small, there is one file to back up, and jobs in the database leave room to split workers later. |
| Learned knowledge in a Git repo. | Database rows or a vector store. | Maintainers already review PRs; Git gives diffs, blame, and revert; agents read files natively. |
| A separate private memory repo. | A `memory/` directory in the code repo. | It holds confidential content, changes far more often, and follows a different review policy. A deployment can split it into several memory repos when teams must not see each other's private data. |
| Forge-neutral ports with GitHub as the only adapter. | GitHub types throughout the core. | The cost now is naming; the benefit is that a second forge or an external CI provider is an adapter, not a rewrite. |
| Exec and ACP transports behind one runtime interface. | Agent-specific code in each playbook, or a single agent. | Playbooks stay agent-agnostic, routing and fallback are config, and ACP lets chat sessions ask the policy engine for permissions. |
| Autonomy per action kind and repository, earned in shadow. | One global switch. | Trust differs by action and by repository: a wrong rerun costs a runner slot, a wrong review comment costs a contributor's trust. Agreement is measured per kind and repository. |
| A React dashboard over a JSON API. | Server-rendered pages. | The inbox, chat streaming, and draft editing are interactive, and the same API serves the CLI and the MCP server. |

## 5. Modules

### 5.1 Domain

Pure dataclasses, Pydantic models, rules, and ports; no I/O. The ports are `typing.Protocol`s (`ForgeReader`, `ForgeWriter`, `CiProvider`, `AgentRuntime`, `MemoryStore`, `LabHost`, `Store`, `Notifier`, `Clock`) that adapters implement and `app.py` injects (4.4). The pure rules are `classify_review_state`, priority scoring and tiering under a rubric, the merge gate predicate (5.5), profile layer merging, and the business-day calendar. The important types:

- `RepoRef`, `PrRef(repo, number)`, `Sha`.
- `RepoProfile` and its sections (2.1), with schema version and the layer each value came from.
- `PrSnapshot`: one consistent read of a PR at a head SHA: metadata, files, commits (with trailers), labels, reviews, issue comments, review threads with their resolved state, check runs, mergeable state. The GitHub adapter fetches it with one GraphQL query per page of PRs.
- `ReviewState`: `NEW_REVIEW`, `FOLLOWUP_REVIEW`, `APPROVED_NEW_COMMITS`, `UP_TO_DATE`, `SELF_PR`, plus `DRAFT` and `CLOSED`. `classify_review_state(snapshot, identities, markers)` is a pure function defined in 5.6; the profile supplies the review identities and any legacy comment markers.
- `Event`: `pr.opened`, `pr.synchronized`, `pr.labeled`, `pr.review_submitted`, `pr.comment` (with parsed slash command), `checks.completed`, `issue.opened`, `repo.files_changed` (for drift), `schedule.tick`, `dashboard.command`, `chat.message`, `memory.changed`, `code.changed`, and the loop-closing events `action.executed`, `action.failed`, and `proposal.decided`.
- `Action`: `kind`, `target`, `payload`, `visibility`, `rationale`, `idempotency_key`, `provenance` (job, agent runs, signals). Label actions carry a role or an explicit label name. Action kinds are an enum (8.3 lists them).
- `Visibility`: `PUBLIC` or `INTERNAL`. `PublicText` and `InternalText` are distinct types; a `PublicText` can only be built through the leak guard.
- `Proposal`: an action that waits for a human. States: `pending`, `approved`, `rejected`, `executed`, `failed`, `superseded` (a newer head SHA or a newer draft replaced it), `expired`.
- `Signal`: a learning input (5.11).
- `Rubric`: named dimensions with weights and rule text, tier thresholds, a boost cap, and stage goals with boosts and expiry (5.6).

### 5.2 Profiles

- **Merging.** Loads the layers of 2.2 in order, resolves `extends:` (packs may extend packs; cycles are an error), merges maps key by key and replaces lists unless a key is marked additive (checks, label rules, flake signatures, guard rules are additive), applies ceilings last, and validates the result against `RepoProfile`.
- **Lock and candidates.** The approved profile is the lock in the memory repo (2.2), mirrored in SQLite. A change to any layer (a memory commit, a push to the target's default branch touching the in-repo file, a release that changes a default or a pack, a shared-knowledge update the repository opted into) produces a candidate: the new merged profile and its difference from the lock. The candidate becomes the lock through the configuration gate (2.3): automatically for an in-repo change merged by a user who meets `access.configure`, otherwise after a maintainer of the repository approves it on the dashboard or on the memory PR.
- **Configuration gate.** One function, `profiles.gate`, decides whether a user may change a repository's configuration. It reads the user's permission on that repository from the forge at the moment of the change and compares it with the lock's `access.configure`. The dashboard, chat, slash commands, the memory gate, and the `am` CLI all call it; nothing else can apply a configuration change.
- **Doctor.** `am profile doctor <owner>/<repo>` and a nightly job check the profile against the live repository: mapped labels exist, required workflows exist, skill and script paths resolve on the default branch, command checks run in the sandbox, lab hosts answer, `CODEOWNERS` parses. Failures show on the dashboard's profile page.
- **Drift.** The poller emits `repo.files_changed` when the files that onboarding read (2.5) change on the default branch, and `profile_drift` proposes a profile change.

### 5.3 Sources

- **Poller** (always on; the source of truth). Per repository: lists open PRs and issues every 2 minutes with ETags, polls checks for PRs with pending runs, and runs a full reconcile every 30 minutes. It turns differences between the new snapshot and the stored one into events, so a missed webhook costs latency, not correctness. After the service writes to a PR (an executed action: a label, a review, a comment), it refreshes that PR's snapshot at once, so the dashboard shows the write without waiting for the next poll.
- **Webhooks** (optional). `POST /webhooks/github` with HMAC verification, for lower latency when the host is reachable from GitHub, directly or through a relay. A webhook triggers an immediate targeted poll rather than being trusted as state.
- **Slash commands.** `/am help`, `/am review`, `/am rerun`, `/am merge`, `/am merge cancel`, `/am label <names>`, `/am why`, `/am pause`. The parser checks the commenter's permission on that repository (write or above, except `help`; PR authors may `/am rerun` once per head SHA when the profile allows it). `/am why` explains the service's last public action on that PR and never mentions priority. The command prefix is a profile setting.
- **Dashboard and chat** emit `dashboard.command` and `chat.message` events through the same engine.

Network: forge calls honor `HTTPS_PROXY`.

### 5.4 Engine

- **Job store.** Jobs are rows with `repo`, `playbook`, `key`, `status`, `attempts`, `payload`, `result`. The key is unique per playbook, so a second event for the same PR and SHA joins the existing job. Jobs survive restarts. A stop (a deploy, `am stop`) cancels the running jobs and puts them back in `queued` without counting the attempt it ended (`Engine.stop`), and on startup any job a crashed process left `running` returns to `queued` before the engine starts, that attempt counted (`Engine.recover`), so unfinished reviews and other work run again at once from fresh reads instead of waiting for their lease to expire; questions their agents left pending are cancelled (`cancel_orphans`).
- **Scheduler.** Cron triggers from `schedules.yaml` (digest, reconcile, compaction, flake report, doctors), each producing a `schedule.tick` event.
- **Worker pools.** Separate concurrency limits for forge I/O, agent runs per runtime, and lab leases, plus a fair share per repository so one busy repository cannot starve the others. The quick quality scores of PRs nobody reviewed yet run in a pool of their own (`quality`, two at a time by default), so the background work never holds up a review someone waits for; a pool the host's settings leave out runs one job at a time.
- **Waits.** A playbook that returns `wait_for(events, timeout)` parks its job in `waiting` with a subscription (event kinds plus target). A matching event or the timeout re-queues the job, which runs again from fresh reads (4.3).
- **Retries.** Exponential backoff with a per-playbook limit; a job that exhausts it shows on the dashboard with its last error.
- **Live events.** Job and proposal changes go to an in-process bus that feeds the dashboard's SSE stream.

### 5.5 Playbooks

A playbook is a class with triggers, a key function, and an async `run` that reads through its context and returns actions, child jobs, signals, internal artifacts, and an optional `wait_for`:

```python
class Playbook(Protocol):
    name: ClassVar[str]
    triggers: ClassVar[tuple[EventKind, ...]]

    def key(self, event: Event) -> str: ...
    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult: ...
```

`PlaybookContext` holds the repository's `RepoProfile`, the `ForgeReader`, `CiProvider` reads, the agent router, the memory reader, the lab client (checks only; running code is an action), the store, and a clock. It has no way to write. A profile can turn any playbook off for its repository.

| Playbook | Trigger | Does | Emits |
|---|---|---|---|
| `onboard` | repository added | Reads the repository, drafts a profile, runs the doctor (2.5). | `memory.change` (T2) |
| `profile_drift` | `repo.files_changed` | Compares changed conventions with the profile. | `memory.change` |
| `triage` | PR opened or synchronized, reconcile | Snapshot, review state, summary, labels from the profile's rules, overlap search; starts child jobs. | `label.apply`, `evaluate`, `review`, `followup`, `hygiene` jobs |
| `review` | a review request (dashboard, `/am review`, chat, scan) for a PR in `NEW_REVIEW`; PR events only with `review.trigger: events` | Worktree at head; the profile's review skill, or the repository's own when the profile keeps the built-in one; interactive for a person's request, headless otherwise; verifier pass; public-text checks. | `review.post`, `label.apply` (`changes_requested`, and the labels the review suggests), `label.ci_gate`, `pr.request_reviewers` |
| `followup` | the same requests and triggers, for a PR in `FOLLOWUP_REVIEW` or `APPROVED_NEW_COMMITS` | Diffs the last reviewed SHA to the head; status per earlier finding; new findings; regression spot-check for approved PRs. | `review.update`, `thread.reply`, `review.post` |
| `review_scan` | `review_scan` tick, with `review.trigger: scan` | When `review.scan.schedule` is due, requests reviews and follow-ups for at most `review.scan.max_prs` PRs without a draft at their head, highest priority first. | review requests |
| `quality` | `quality.requested` (Score quality on the dashboard, or from `quality_scan`) | Has an agent score a PR's quality by the repository's quality guide (5.6): a reviewed PR from its latest review, reading the checkout, the review, and the lessons, the score replacing that review's own; a PR without a review (or any, on request) quickly from its diff. | none |
| `quick_learn` | `quality.learn` (from `quality_scan`, after three new misses), in the `quality` pool | Draws at most six rules for the repository's quick checks from the quick scores its reviews corrected; every quick score reads them. | none |
| `quick_quality` | `quality.quick` (from `quality_scan`), in the `quality` pool | Gives an open PR nobody reviewed yet a quick score from its diff at its head, unless a review already scored it or it has one at this head. | none |
| `quality_scan` | `review_scan` tick | Asks `quality` to score the latest review of each open PR that has no score (one written before reviews scored quality, or whose agent left it out), three per repository and tick, once per review. Unless `review.quick_quality` is off, and once the last batch is done, asks `quick_quality` for six PRs nobody reviewed, highest priority first: those never scored, then those scored at an older head at least six hours ago, without a reason in every dashboard language, or without checks. | quality requests |
| `evaluate` | new PR, material change, a rubric change, a maintainer's request | Agent scores the rubric dimensions on new or materially changed PRs, and every dimension of a PR a maintainer asks to score again, with the repository's lessons in the prompt; deterministic recompute of boosts and tiers for everything else. | internal evaluation |
| `calibrate` | priority override, clarification reply | Calibration agent picks a disposition and checks for oscillation. | evaluation override, `memory.change` |
| `hygiene` | PR opened or synchronized | Runs the profile's checks. | `comment.upsert` (one per PR, updated) |
| `ci_doctor` | checks completed with failure | Signature match or agent diagnosis; rerun, notify, or explain. | `ci.rerun_failed`, `comment.upsert`, `notify` |
| `merge` | merge armed, checks completed, review submitted, its turn in the merge queue | Evaluates the merge gate; takes turns with the repository's other armed PRs; updates the branch when needed; merges. | `pr.update_branch`, `pr.merge`, `comment.upsert` |
| `lab_test` | review asks for a run, `/am review` with tests | Lease, worktree, environment selection, test template, cleanup. | `lab.run`, `label.apply` (`reproduced`) |
| `reproduce` | a maintainer's reproduce request for a PR (dashboard), naming a test machine; a review or follow-up that starts one (`review.reproduce`) | Agent writes a reproduction that follows the PR's documentation from a checkout of the head, or says what the machine lacks; waits for the requester to approve it, or, for a review, as the repository's `lab.reproduce` rule says; after it ran on the machine, the agent reads the runs and drafts a report, or adds the result (or why no run happened) to the review draft (5.10). | `lab.reproduce`, then `comment.upsert`, drafted for the requester; for a review, decided by the rule, and the result goes into the pending review draft |
| `stale` | daily tick | SLA timers; nudges; closing proposals for abandoned drafts. | `pr.nudge`, `pr.close` |
| `issue_triage` | issue opened | Template labels, missing information, duplicates, doc links. | `issue.label`, `issue.comment` |
| `digest` | daily and weekly tick | Builds the digest and metrics per maintainer. | `notify` |
| `learn` | signal batch, weekly compaction | Turns signals into memory changes (5.11). | `memory.commit`, `memory.change` |
| `self_improve` | `improve` request from chat, once a maintainer of the code repo approves it | Agent edits a worktree of the code repo; the playbook runs the tests and Ruff and has the diff described. | `code.pr` |
| `fix` | a maintainer's fix request for a review (dashboard) | Agent changes a worktree at the PR head for the review's findings; the playbook takes the diff. The approver chooses where it goes: a PR into the PR's branch, or one commit straight onto that branch (which they must be allowed to push to); either way it is pushed with their own account. | `pr.fix`, drafted for the requester |
| `merge_base` | a maintainer's Merge request on a PR that conflicts with its base branch (dashboard) | Starts merging the base branch into a worktree at the PR head; an agent resolves each conflict, keeping what both sides meant, runs nothing, and may ask the maintainer. The draft shows the resolution; approving pushes a merge commit (parents: the PR head and the base branch's commit) onto the PR's branch, in the contributor's fork when it lives there, as the approver. A PR that merges cleanly does not come here: the press has GitHub update the branch as the person (`pr.update_branch`). | `pr.fix` with `merge_ref`, drafted for the requester |

**Review requests.** A review request names the PR and, unless it comes from the scan, the reviewer who asked (`Action.draft_for`). The service routes it by the PR's state: a first review for `NEW_REVIEW` (or the reviewer's own PR), a follow-up for `FOLLOWUP_REVIEW` and `APPROVED_NEW_COMMITS`, nothing for `UP_TO_DATE`. A reviewer's request may target drafts and PRs outside `scope`; scans and events respect `scope` and `scope.backfill_reviews`. Every action a requested job produces is a proposal for the requester, even where the policy level is `shadow` or `auto`; when a maintainer approves it, it runs even under `shadow`. Dev mode, pauses, `off`, and the hard limits still apply. Drafts from scans and events follow the policy as usual.

**Skills, questions, and posting.** A review follows the skill the profile names. When the profile keeps the built-in skill and the repository's default branch has a review skill of its own (in `.agents/skills/`, `.claude/skills/`, `.codex/skills/`, `.gemini/skills/`, or `.opencode/skills/`, with "review" in its name or path), the review follows that one; the agent is also told about the repository's other skills. The built-in `pr-review` skill covers correctness, design and reuse, tests, docs, and PR metadata. Review skills are often written for an interactive session. For a review a person asked for, the prompt tells the agent it reviews for that person and may ask them when something blocks a sound judgment; the questions reach that person (5.7). Scans and events run headless: the agent decides on its own. In both cases the agent returns the review result of 5.6 and never posts: whether to post is the requester's decision on the draft, so the agent is told not to ask, and a question about posting or pushing is denied without reaching anyone (5.7).

**One review per commit and agent.** A request for a PR whose head already has an active review job, or a pending draft, from the same agent joins it instead of starting another: the requester sees who asked and which agent runs. A different agent at the same head is a separate review. The requester may pick the agent; that choice replaces the route's primary and fallbacks for the review (or fix) task only, and verification keeps its own route.

**Posting as yourself.** A maintainer can approve a `review.post` draft as themselves: the actuator posts it with the token they signed in with (5.9). The marker stays in the body, so the service still recognizes the review as its own when it classifies review state (`own_actions`), and later follow-ups treat it like any other review it posted.

**Fixes.** From a review with findings, a maintainer can ask an agent to fix them. The `fix` playbook checks out the PR head in the repository's folder, gives the agent the findings, the repository's skills, and an optional note, and lets it ask the maintainer. The agent edits the worktree and never commits, pushes, or posts. The playbook turns the diff into a `pr.fix` action drafted for that maintainer, who reads the diff on the proposal and approves it as themselves: the actuator commits it with them as author and a `Signed-off-by` from them, pushes `am/fix-<n>-<id>` with their token, and opens a PR into the reviewed PR's head branch (for a fork, a cross-repository PR). `pr.fix` cannot be approved any other way; the service's own account never pushes.

**Working directories.** A repository's checkouts live in its own folder on the host (`<data>/repos/<owner>/<name>/`), one worktree per run named after the PR and head. Chat threads about a repository or PR run in such a checkout too, so the agent can read the code it discusses.

**The merge gate.** `merge` merges only when all of these hold at the current head SHA. The profile fills in every repository-specific part.

1. Someone allowed by `merge.arming.who` (anyone with write access, or `CODEOWNERS` of the touched paths) armed it with the slash command, the `automerge` role label, or the dashboard. Arming records the SHA and the maintainer. A later push disarms it unless the approval survives under branch protection and the `followup` spot-check finds no regression; the arming maintainer is told either way.
2. The approvals that branch protection and `merge.approvals` require are present from humans, and no "changes requested" review is outstanding.
3. Not a draft, no label from `merge.blocking_roles` (by default `changes_requested`), no unresolved review threads.
4. Every required check succeeded: those from branch protection plus `ci.required_workflows`. A job skipped inside a required workflow by its own path filters is fine; a skipped or cancelled required workflow is not.
5. Every check marked `blocking` in the profile passes (for example, the PR title check when the title becomes the squash subject).
6. The branch is current with the base when branch protection requires it. Otherwise, or when the merge would conflict, the playbook emits `pr.update_branch` and waits for CI again.
7. The repository is not paused and no freeze window from the profile is active.

**Merge queue.** When PRs must be current with the base before they merge (branch protection requires it, or `merge.update_branch`, on by default), merging one armed PR puts every other armed PR behind again, and each must merge the base and run its CI once more. So the armed PRs of a repository take turns (`domain/merge_queue.py`): only the PR whose turn it is merges the base, waits for its CI, and merges; the others wait without updating, so no CI run is wasted on a branch that another merge will make stale. The turn stays with a PR while it is ready or waits only for its own CI, and moves on when it merges, is disarmed, closes, or stops for another reason (a failed check, a missing approval). The next turn goes, first, to a PR that can merge as it is (current and green), the one whose CI costs most first, because merging it saves running that CI again while a cheap PR, such as a documentation change, pays little to rerun after it; then to PRs that must merge the base, the one whose CI costs least first, since each pays its CI once whatever the order. A PR's CI cost is the time its checks took at its head. The order covers the PRs armed at the time; a PR armed alone that is current and green merges at once. The turn and the order are kept in the store's state (`merge-queue:<repo>`); the job that hands the turn on wakes the next PR's job with a `merge.queue` event, and the PR page shows the queue.

The merge uses `merge.method`. For a squash, the subject comes from `merge.message.subject` (default: the PR title with `(#N)`), and the body lists the commit subjects followed by every distinct trailer named in `merge.message.preserve_trailers` (the `dco` pack adds `Signed-off-by` and `Co-authored-by`).

**CI doctor.** For each failed check run on the current head SHA:

1. Skip runs for a SHA that is no longer the head, and cancelled runs unless `ci.cancelled_is_failure` is set (workflows with `cancel-in-progress` cancel superseded runs routinely).
2. Fetch the job log through the `CiProvider` and cut it to the failing step plus a window around the first error. For providers without log access, use the check run's summary.
3. Match the signature library: signatures from the profile's packs, global ones if the repository opted in, then the repository's own.
4. Without a match, run the `ci_diagnose` task (log excerpt, PR description). It returns `category` (`flaky`, `infra`, `pr_caused`, `unknown`), `confidence`, evidence lines, an explanation, and an optional new signature. This is the first look, cheap and from the log alone.
5. `flaky` with budget left (`ci.rerun.max_per_sha`, default 2, with backoff): rerun failed jobs. A flake that still fails after every rerun is no longer treated as one.
6. **Root cause.** Every failure that is not a flake (and every flake that kept failing) goes, together with the others of the same head, to one `ci_root_cause` task (`ci.root_cause`, on by default): a read-only worktree at the head SHA, the PR diff against the base branch, each job's log excerpt and first look. Several jobs often fail for one reason, so the agent returns one cause per distinct reason (`domain/records.py:RootCause`): the jobs it explains, what fails (a test, a build step, a command), the mechanism, the culprit files and lines, the smallest fix, up to five log lines, a confidence, and its origin: `change` (this PR causes it), `base` (it fails without the change: the base branch is broken), `infra`, `flaky`, or `unclear` (the agent says so rather than guess). The playbook keeps only jobs that failed at this head, and only culprit paths that the PR changes or that exist in the checkout. A `change` cause becomes a comment for the author in the public language (what fails, why, where, the suggested fix; no confidence, no log lines); a `base` cause tells the author it is not theirs and notifies the maintainers (`ci.base`), as do `infra` (`ci.infra`) and `flaky` or `unclear` causes (`ci.unknown`). A job with no cause (the switch is off, or the agent gave no usable answer) keeps the first look: `pr_caused` explains it to the author in one line, `infra` and `unknown` notify.
7. Record the observation, with its root cause, in the repository's journal; the PR page shows both. A suggested signature that recurs three times, or that a maintainer confirms, becomes a T1 memory change; one that recurs in several repositories is proposed for global memory.

### 5.6 Review and priority models

These models are the same for every repository; profiles fill in names, templates, and weights.

**Review states.** `classify_review_state` reads the reviews and comments that the profile's review identities made on a PR, and the SHA each one covers. The reviewed SHA comes from a review's commit, from the `sha=` field of the service's marker in a comment, or from a match in `comments.legacy_markers`. A dismissed review with no body does not count.

| State | Condition | Next step |
|---|---|---|
| `SELF_PR` | the PR's author is one of the review identities | no review; still triaged, labeled, and scored |
| `NEW_REVIEW` | no review and no marked comment from a review identity | `review` |
| `FOLLOWUP_REVIEW` | the latest review or marked comment covers SHA `R`, the head has moved past `R`, and that latest review is not an approval | `followup` |
| `APPROVED_NEW_COMMITS` | the latest review is an approval at `R`, and commits were pushed after it | `followup` spot-check for regressions before a merge |
| `UP_TO_DATE` | the latest review or marked comment covers the head SHA | nothing until the next push |
| `DRAFT`, `CLOSED` | the PR is a draft, or no longer open | out of scope (drafts are in scope with `scope.include_drafts`) |

**Review result.** Whatever skill produced it, a review returns this structure, which the playbook renders into the profile's template:

- `summary`: what the PR does and how, in one or two sentences, and what could regress.
- `findings`: each with an ID, `severity` (`critical`, `major`, `minor`, `nit`), `category` (`correctness`, `design`, `reuse`, `tests`, `docs`, `metadata`, or one the profile adds), the file and line range when a diff line carries it, evidence, a suggested fix, and whether the verifier pass confirmed it.
- `dismissed`: findings the verifier dropped, with one line each on why. Internal only.
- `tests_run`: commands, where they ran, and results; tests skipped and why.
- `sections`: an answer for each section of the profile's template, when it has one.
- `recommendation`: one value from `review.recommendations`, plus the remaining actions for the author and for maintainers.
- `quality`: the agent's judgment of the PR, a score from 0 to 100 and a sentence or two on what decides it, by the repository's quality guide (below). Follow-ups return it for the PR at its new head. Internal.
- `labels`: labels the PR should carry and does not, chosen from the repository's own labels (never the role labels the service manages, nor ones the PR has), at most `review.max_suggested_labels` (3 by default). Follow-ups return the same field. The playbook keeps only names on that list, and what remains becomes a `label.apply` action: for a requested review, a proposal for the requester beside the draft; for scans and events, the repository's policy for `label.apply` decides, as for the labels triage suggests. `review.suggest_labels: false` turns it off for a repository.

When the review follows the repository's own review skill (the profile names it as `repo:<path>`, or the repository has a skill whose name says review while the profile keeps the built-in default) and the profile does not set sections of its own, the skill fixes the format: the agent returns the skill's main-comment sections, headings word for word and in its order, and they are posted as returned; findings on diff lines still become inline comments, worded as the skill says (without the service's own finding heading). The review is told the PR's latest reproduction on a test machine (the approved plan, the machine, the commits it ran on, the exit codes, and the service's conclusion; run output stays with the job), so the skill's questions about how the PR was reproduced and what came out are answered from it, or say that no run was made and what one needs, including when the repository has no test machine. When the repository uses a machine and its `lab.reproduce` rule lets this PR run or be proposed, and nothing is on record or underway at this head, the review says a reproduction is underway and starts one once it is drafted (5.10); its result is added to the draft later. The reviewing agent never reproduces the PR itself. The built-in template has four sections: Summary, Findings, Tests and docs, Recommendation. The built-in recommendations are `ready` (no open critical or major finding), `changes` (open findings the author can fix), and `blocked` (progress needs a maintainer decision, assets nobody has, or another PR). A profile can rename them, add sections, or replace the template; each recommendation keeps its meaning for the playbooks: `changes` applies the `changes_requested` role, and `ready` lets `label.ci_gate` and reviewer requests proceed.

A finding goes inline when a line of the diff carries it, with the suggested fix as a suggestion block when it is a code change. Other findings go in the main comment under their section. There is one review per head SHA, and a new finding on a line that already has a thread is posted as a reply in that thread.

**Follow-up format.** A follow-up edits the service's main comment, or posts a follow-up review when the earlier review used inline threads. Each earlier finding gets a status (`✅ Resolved in <sha7>`, `⚠️ Still open`, or `◐ Partly addressed` with what remains), and new findings go under "New findings since <sha7>". Resolved threads get a reply naming the fixing commit. When everything is resolved and nothing new appeared, the comment says so in one line.

**Writing.** Every review, follow-up, and revision of one (a reviewer's turn in Discuss, a reproduction's result) is written by the repository's writing guide, `review.writing_guide`, or the service's default (`playbooks/prompts.py:DEFAULT_WRITING_GUIDE`) when it sets none. The default writes for a maintainer who has not read the PR and does not know that part of the code. It leads with what the PR does and whether it is ready, then says what must change and why, says each thing once, and keeps code out of the main comment: no code blocks, few identifiers, and few file or line references. A finding on a diff line is posted on that line, where it may name the code. Where a repository's review skill asks for more code or references, its sections stay and the guide wins.

**Quality.** The reviewing agent scores each PR's quality from 0 to 100 in its review (`quality`), by the repository's quality guide, `review.quality_guide`, or the service's default (`DEFAULT_QUALITY_GUIDE`). Before it scores, the agent fills `checks`: the basics of `playbooks/prompts.py:QUALITY_CHECKS` (the repository's requirements for every PR, the tests and docs the change needs, its code style, and the design), each met, not met, or not applicable; the dashboard shows them as a row of ticks and crosses. The default guide asks for a maintainer's judgment against the other PRs the repository gets. It checks the basics first, and each one missed is a fault even when it is easy to mend. It weighs design most: whether the change fits the repository's architecture, design, and style, solves the problem in the right place, and stays simple. Easy faults cost less than hard ones but add up, and several hard faults make a PR unacceptable whatever its strengths. It keeps 90 and up for PRs with nothing left but trivia, and uses the whole range. The guide describes five bands, and the dashboard colors the score by them: excellent (90 and up), good (75), fair (60), poor (40), and bad (below 40). Out-of-range or unreadable scores from an agent are clamped or dropped rather than failing the review, and a revision that does not score the PR again keeps the draft's score. A PR's quality is that of the newest review in the chain follow-ups build on (the posted reviews, then a newer draft or shadow run; a draft a maintainer rejected does not count) that has one. A later scoring of that review by the `quality` playbook (Score again, or the scan for reviews without a score) replaces it. Before a review, every open PR gets a **quick score** at a glance, so reviewers can choose what to take first: an agent reads the diff and as much of the code around it as it needs, by the same quality guide, without running anything or writing a review. It must cost far less than a review, so it reads a condensed diff (each file's diff up to 6,000 characters, sources before tests, without lock, binary, and data files, 24,000 in all), opens at most three other files, runs nothing, and runs at low effort for at most five minutes whatever its route and agent say (`AgentTask.budget: quick`). It holds the PR to the repository's own review standard through a checklist: once per repository, and again whenever they change, an agent draws from its review skill and contributor guide the dozen checks a reader can settle quickly (how docs are organized, naming and code style, the design rules and taste the repository asks for, the tests and docs a change must bring, what every PR must meet), and every quick score reads that checklist instead of the skill. It is also given what the service knows (the PR's size, whether it touches tests and docs, its CI, and the hygiene checks at its head), and, for calibration, PRs of the repository that a review scored, from low to high, and how the quick scores so far are spread (`quick_quality`, in the background, highest priority first; `review.quick_quality: false` turns it off). The review's score is the accurate one: once a review scores the PR, it shows instead, and the PR is not scored quickly again; the PR page keeps the quick score beside it for comparison. When a PR's head moves past its score (new commits after its quick score, or after the review that scored it), it gets a quick score of the new head once it has been quiet for an hour, so a burst of pushes gets one score, and at the latest six hours after the scan first saw it move. Until a follow-up review scores it again, the dashboard shows that quick score, and the PR page shows the review's score beside it with the commit it covers; Score quality on such a PR scores the new commits quickly instead of the old review. A quick score without a reason in every dashboard language or without checks is scored again too.

Quick scores learn from the reviews that follow them. When a review scores a PR at the same commit as its quick score and the two are 15 points or more apart (`QUICK_MISS`), the scan records the miss with what each said. Every later quick score reads the latest misses, and after three new ones an agent (`quick_learn`, in the `quality` pool, reading only) draws again at most six rules for quick checks of that repository from all of them, keeping the rules that still hold; every quick score follows those rules. The rules and the misses show on the repository page beside the quick-score switch. They are internal and change only how PRs are ranked at a glance, so they apply without a maintainer's approval; a maintainer who disagrees changes the quality guide, which wins. The agent writes each reason once per dashboard language (`playbooks/prompts.py:DASHBOARD_LANGUAGES`, English and Chinese), and the dashboard shows each person the one they read it in. The scores are internal: they show on the dashboard and to the chat agent, and never in a public action; the prompts tell the agent never to mention them in what it writes.

**Priority model.** A rubric names its dimensions. The `evaluate` agent scores each from 0 to 10 using the dimension's rule text; the weights sum to 1.

```
base  = 10 × Σ weight[d] × score[d]                       0 to 100
boost = min(Σ boosts of matching active stage goals, boost_cap)
total = clamp(base + boost + manual_boost, 0, 100)
tier  = tier_override, or the highest tier whose threshold total reaches
```

A stage goal is a time-boxed priority: a title, a description, keywords matched against the PR's title, summary, labels, and review state, a boost, an expiry, and who added it. Dimension scores are computed when a PR first appears and again after a material change (a much larger diff, a rewritten description, a change of domain labels), or when a maintainer asks (Score again on a PR, on a selection or the whole view of the Pull requests page, or through the agent), which scores every dimension afresh with the repository's lessons as they are then; a PR already being scored again on request joins that job. Changing weights, thresholds, or stage goals recomputes totals and tiers for every open PR without new agent calls. A PR without a score for every dimension of the new rubric is not recomputed, since the missing ones would count as zero: it keeps its last total, and an agent scores it again (`priority.rescore`). A PR scored on dimensions the rubric no longer has is scored again in full, since a score kept under a shared name may mean something else; otherwise, adding a dimension scores only that one.

The default rubric, for repositories that do not define one:

| Dimension | Weight | Scores high when the PR |
|---|---|---|
| `impact` | 0.30 | is wanted by many users or unblocks important work |
| `urgency` | 0.25 | fixes silent wrong results, crashes, or regressions on core paths |
| `strategic` | 0.25 | serves the project's current goals, key collaborators, or ecosystem commitments |
| `readiness` | 0.20 | is small, clean, and tested, or its author has already addressed review |

Default tiers: `P0` at 78 and above, `P1` at 65, `P2` at 52, `P3` below that; boost cap +5. Scores, tiers, and rubric content are internal and never appear in public text.

**Calibration.** A maintainer can change a PR's dimension scores, add a manual boost, or set a tier, and must give a reason. The `calibrate` agent receives the PR, the values before and after, the reason, the rubric, and the repository's recent overrides by all its maintainers, and returns one disposition:

- `SINGLE_PR_OVERRIDE`: the reason is specific to this PR. Only the PR's evaluation changes, and the journal records it.
- `STAGE_GOAL_UPDATE`: the reason describes a time-boxed shift of focus. A stage goal is added or updated as T1 knowledge (5.11).
- `CORE_RUBRIC_UPDATE`: the reason exposes a gap in the rubric's rules or weights. The agent proposes a rubric change, which is configuration and needs a maintainer's approval (2.3).
- `ASK_CLARIFICATION`: the reason is too vague, or it contradicts recent overrides. The agent asks a question in the dashboard thread, and the answer runs calibration again.

The agent also reports whether the override oscillates against earlier ones, and which ones. A conflict between two maintainers is shown to both instead of being settled silently, and a one-off reason never becomes a rule.

### 5.7 Agent runtimes

One protocol for every agent:

```python
class AgentRuntime(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...
    async def probe(self) -> RuntimeHealth: ...
    def run(self, task: AgentTask) -> AsyncIterator[AgentEvent]: ...
```

`AgentTask` carries `kind`, prompt, `workdir`, `mode` (`read_only` or `workspace_write`), `run_code`, optional output schema (a Pydantic model), skill directories, MCP servers, an optional session to resume, a timeout, and a budget (`quick` caps a run at low effort and five minutes whatever its route says).

**Each agent run in a container of its own.** On a host with docker, every agent run is a fresh container (`agents/container.py`) from an image built from `agents/agent.Dockerfile` (Ubuntu with git, Python, Node, ripgrep, and the like; the service builds it in the background when it is missing, or `am sandbox` does). The container holds only what the run needs: its checkout and that checkout's git metadata (the repository's mirror read-only), its run folder, the skills read-only, the agent CLI and the host's `gh`, `uv`, and `ruff` read-only, a home folder of its own with just the CLI's login (`home_paths`, such as `~/.gemini`), and the GitHub read proxy's socket. It holds none of the service's config, secrets, data folder, memory repo, people's tokens, or other repositories, and no docker socket, so inside it the agent may do anything: it runs in bypass mode with no rules on its commands (`effective_permissions`). CPU, memory, and processes are capped (`sandbox` in `local.yaml`), every Linux capability is dropped, no GPU is passed in, and it runs as the service's user. The network is open: `bridge` by default, or `host` where the host's firewall or proxy passes only the host's own traffic (the container then shares the host's ports, never its files). Environment values reach `docker` through its environment (`-e NAME`), never its command line, and the container is removed when the run ends, whatever ended it.

Agents read GitHub as the service without holding its token: the service serves `agents/github_proxy.py` on a Unix socket each container mounts, and `gh` there sends every request through it (`http_unix_socket`). The proxy adds the service's token to requests for the GitHub API and passes only reads: GET, HEAD, and OPTIONS, and GraphQL queries (`gh pr view` reads through GraphQL); a mutation and every other method are refused with a message saying writes go through the service. Writes stay where they were: agents never post, push, or merge; the actuator does, after approval. Prompts still ask agents not to run a PR's tests or workflow (`prompts.NO_CODE_RULE`), since reproductions run on the repository's test machines; that is guidance, not a defense.

The Settings page shows the sandbox's state and lets a maintainer of the service's repository turn it on or off and set its network and limits; the change is kept in this host's `local.yaml` and applies to the next agent run (turning it off gives agents back to the host at once). A host without docker, or with `sandbox.enabled: false`, runs agents on the host as before: the CLI's own auto mode or `agents/approver.py`, with what it would ask going to the question desk. Some agents (Antigravity) end their turn when a request is refused, waiting for a person; the conversation then tells the agent what was refused and to finish without it, up to three times (`acp.CONTINUE`). The event stream normalizes every runtime into `message`, `tool_call`, `tool_result`, `usage`, `permission_request`, and `final`; the final event carries an `AgentResult` with the validated structured output, the session reference, usage, and the transcript path.

Every built-in runtime talks back to the service while it runs, so that what an agent would ask a person reaches a reviewer (see **Agents' questions** below). Three transports, all over the CLI's standard input and output:

- **Claude Code's stream-json control protocol**: print mode with `--input-format stream-json`; the service sends an `initialize` control request and the prompt as a user message, and answers `can_use_tool` control requests.
- **Codex's app server** (`codex app-server`, JSON-RPC): `initialize`, `thread/start` or `thread/resume`, `turn/start`; the server sends approval and user-input requests.
- **ACP** (Agent Client Protocol, JSON-RPC): `initialize`, `session/new`, setup calls such as `session/set_mode`, `session/prompt`; the agent sends `session/request_permission`.

The generic adapter for other CLIs (pi, dsh) is one-shot: standard input carries the prompt and closes.

Adapters, checked against `--help` of the CLIs on the development host and against live runs where the account allowed (versions in parentheses):

| Runtime | Binary and transport | Auto mode | Structured output | Restricted mode | Bypass mode | Sessions |
|---|---|---|---|---|---|---|
| Claude Code (2.1.318) | `claude -p --input-format stream-json --output-format stream-json --verbose`, `--setting-sources ""` and `--strict-mcp-config` so neither a repository's nor the host user's settings, hooks, or MCP servers apply (verified live) | `--permission-mode auto --permission-prompt-tool stdio`: Claude's classifier approves what it judges safe, the rest arrives as `can_use_tool` (verified live) | `--json-schema` | `--permission-prompts none` with `dontAsk` (read-only) or `acceptEdits` and an `--allowedTools` list | `--dangerously-skip-permissions`; as root it also needs `IS_SANDBOX=1` | `--resume <id>` |
| Codex (0.162.1) | `codex app-server` (verified live) | sandbox `read-only` or `workspace-write`, `approvalPolicy: on-request`, `approvalsReviewer: auto_review` (Codex's reviewer subagent decides escalations); user-input requests reach reviewers | `outputSchema` on `turn/start` (strict: every property required) | `approvalPolicy: never` | sandbox `danger-full-access` | `thread/resume` |
| Antigravity (1.5.1) | `agy acp` (verified live) | the service's automatic rules over ACP mode `default` (read-only tasks, with `--sandbox` unless the runtime option `sandbox: false`) or `accept-edits` | prompt plus validation | automatic rules only, no reviewer | `--dangerously-skip-permissions` | none |
| OpenCode (2.0.32) | `opencode acp` (handshake verified; prompts not run, no quota) | the service's automatic rules; `OPENCODE_CONFIG_CONTENT` makes edits, shell, web fetches, and outside paths ask (edits denied for read-only tasks) | prompt plus validation | automatic rules only | every permission `allow` | none |
| Gemini CLI (0.9.0) | `gemini --experimental-acp` (not run: the account needs `GOOGLE_CLOUD_PROJECT`) | the service's automatic rules over `--approval-mode default` (read-only) or `auto_edit` | prompt plus validation | automatic rules only | `--approval-mode yolo` | none |
| pi, dsh | generic adapter, disabled until an operator sets the command | none: the CLI cannot ask | prompt plus validation | whatever the command does | `bypass_args` in the runtime config | none |

Codex's sandbox and Antigravity's `--sandbox` both failed to start on the development host. Under Codex that sends every command to the reviewer subagent; operators who set `approvals_reviewer: user` would send each one to a reviewer instead. Only Claude Code reports cost, so `daily_usd` budgets bind only for it; `daily_runs` works for every runtime.

The **generic adapter** covers any CLI from config alone: a command template with `{prompt_file}`, `{workdir}`, and `{schema_file}` placeholders, an output format (`text`, `json`, `jsonl`), and a path to the final message. A runtime moves to a native adapter when it needs sessions or a structured event stream.

**Structured output** is tried in this order: the runtime's schema flag; JSON extracted from the final message with one repair turn (the router adds the schema to the prompt for runtimes without a flag). Pydantic validates the result in every case; a second invalid result fails the run and triggers fallback. Tasks that edit a worktree at length (`fix`, `self_improve`) ask for no fixed form: the diff is the result, and a short read-only task describes it.

**Models and reasoning levels.** Each runtime reports what it offers (`AgentRuntime.choices`): ACP CLIs are asked directly, by opening a session and reading its `model` and `thought_level` options without sending a prompt (no tokens; kept for an hour); others list what their CLI accepts (Claude Code's model aliases and `--effort` levels, Codex's reasoning levels). The health probe carries them, the Settings page shows each agent's model and thinking level with its CLI's defaults, and an agent's own `model` applies when a route names none, while its own `effort` wins over the route's, as its own permission mode does.

**Router.** The route for each task kind (primary runtime, fallbacks, optional verifier, model and effort, permission mode, question timeout) comes from the repository's profile, falling back to the service settings and then to `config/defaults/routing.yaml`. The service settings (8.1) declare which runtimes exist, their models, model providers, and budgets; disabling a runtime, or not installing its CLI, moves its tasks to the next runtime in each route; a missing CLI opens no circuit breaker, so it is used as soon as it is installed. A change to the settings rebuilds the runtimes without a restart; runs in progress finish on the runtime they started with. A runtime that fails with auth expiry, quota exhaustion, a crash, or a timeout is skipped for a cool-down period (circuit breaker); two invalid answers in a row pause only that task kind on that runtime, so a CLI that cannot answer one kind of task still runs the others. Pickers and labels name the agent a route runs on now (its first installed agent), not its first entry. Budgets are tracked per runtime per day from reported usage, with optional limits per repository.

**Model providers.** A runtime can use a model endpoint instead of its CLI's own sign-in. A provider has a protocol (`anthropic` for Anthropic Messages, `openai` for Chat Completions, `openai-responses`), a base URL, its models, and the name of the secret that holds its key. The adapter points the CLI at it: Claude Code through `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` (Anthropic protocol only, with an inherited `ANTHROPIC_API_KEY` removed); Codex through `-c model_provider=...` and a `model_providers.<id>` entry whose `env_key` is `AM_PROVIDER_API_KEY`; OpenCode through an inline provider in `OPENCODE_CONFIG_CONTENT` whose key is an `{env:AM_PROVIDER_API_KEY}` reference; the generic adapter through `AM_PROVIDER_*` and the protocol's usual variables. Gemini CLI and Antigravity talk only to their own services, so the settings refuse a provider for them. The key travels only in the agent's environment, never on a command line or in a file, and the exact value is scrubbed from transcripts and errors. A runtime with a provider and no model of its own uses the provider's first model.

**Sandbox.** Every run gets its own worktree from the service's mirror. The environment is built from an allowlist (`PATH`, `HOME`, locale, proxy variables) and no forge token (a GitHub CLI token cannot be down-scoped, so agents read the checkout the service made); it never carries `GITHUB_TOKEN`, `GH_TOKEN`, or the SSH agent socket. Each run also gets an empty `GH_CONFIG_DIR` and its own `GIT_CONFIG_GLOBAL` with system config off, so `gh` and `git` cannot pick up the host's stored credentials. `workspace_write` tasks (`self_improve`, lab script generation) write only inside their worktree. Tasks without a checkout (triage, scoring, onboarding, CI diagnosis) get a folder of their own inside their repository's folder (`scratch/<owner>/<name>/<job>`), so one repository's work never sits beside another's; separating what agents can read beyond that, in bypass mode or on a shared host, takes a confining `command_prefix`.

These measures keep an agent from acting through credentials by accident; they do not stop a process that reads files under the host user's home directly. Reading files is allowed in every mode, which is why the leak guard rejects any public text that contains a credential (section 9). To confine agents further, operators set `command_prefix` on a runtime: a wrapper such as `bwrap ... --`, `firejail`, a container, or `sudo -u <agent-user>`, with `{workdir}` and `{run_dir}` filled in for each run.

**Permission modes.** A route's `permissions` is one of:

- `auto` (the default): each CLI's own auto mode, with what it would ask a person sent to a reviewer. For the ACP runtimes, which have no auto mode of their own, the service's automatic rules (`agents/approver.py`) stand in: they approve reading and searching inside the run's directories (symlinks resolved, so a link a PR commits cannot point out of the worktree), read-only shell commands there (several joined with `&&`, `||`, `;`, or `|` when each one is, with output discarded to `/dev/null` or joined by `2>&1`; no other redirects, substitutions, subshells, or background `&`), and, for writing tasks, edits inside the worktree and running tests or linters; they refuse edits in read-only tasks; everything else goes to a reviewer. An agent's settings can add commands its runs may start without asking, on any PR (`agents.<name>.approve_commands`, a command's leading words, matched with any further arguments while their paths stay in the run's folders): meant for a repository's own read-only scripts, which come from its default branch, so a PR cannot change them or put a module beside them. The settings check refuses an interpreter or a shell without its script (`python3`, `python3 -c`, `bash -c`), runners that run a project's code or another command (`uv`, `npm`, `make`, `pytest`, `env`, `sudo`, ...), network tools, and shell syntax (`approve_command_problems`). Antigravity's own always-allow rules in its `settings.json` do not apply to the ACP sessions the service runs: it asks the service about every command there. A read-only task nobody asked for (triage, scoring, a scheduled review, a CI diagnosis) asks nobody: what the rules do not settle is denied at once, since no one waits on it and a question would hold the run until it expires. Bypass applies only to PRs from the repository's team; on PRs from outside contributors the agent stays in this mode, because their text could steer an agent that skips every check. Claude Code's classifier is more permissive than these rules: in a live run it let a read-only task write a file outside the worktree without asking. Repositories that want the narrower behavior for reviews use `restricted` on those routes.
- `restricted`: the CLI's narrow mode, and any request that would need a person is denied without asking.
- `bypass`: every permission check and CLI sandbox is skipped. It applies only when it is asked for and allowed, either by a route that asks for it on an agent whose settings set `allow_bypass: true` (off by default), or by the agent's own `permissions: bypass` in the service settings, which asks and allows at once; and in both cases only when the input is trusted or the route sets `bypass_untrusted: true`. Input counts as trusted when the PR author is on the team or the task carries no contributor text, as for calibration and for a self-improvement (the request a maintainer of the code repository approved, what people wrote in that conversation, never the agent's answers, which can repeat PR text, and the service's own code). Otherwise the attempt runs in `auto`.

An agent's own `permissions` (`auto`, `restricted`, or `bypass`; unset by default) applies to every task it runs, instead of the route's mode. It is how an agent is switched to skip permissions everywhere, such as Antigravity with `--dangerously-skip-permissions`: one switch on the Settings page (with a confirmation that names the flag and what the agent can then read on the host), or one line asked of the agent, under the service gate. A CLI without a bypass mode (the generic adapter without `bypass_args`) cannot be set to `bypass`.

The router decides per attempt, and every agent run records the mode it actually ran with; the dashboard marks bypass runs. Bypass changes nothing about what the service posts, but it does change what a prompt-injected agent can do on the host: text in an outside contributor's PR can steer an agent that can run commands, read files the host user can read, and reach the network. That is why it is opt-in: someone who maintains the service's repository turns it on, and outside contributors' PRs keep `auto` unless a route says otherwise. The host user's files include the GitHub sign-ins the service keeps and the SSH keys for test machines.

**Progress.** The router reports each attempt to an observer (`AgentRunner.bind_progress`): a `started` event, then every event the runtime emits, with credentials scrubbed. `engine/progress.py` turns them into a job's steps (`domain/progress.py`: what the agent said, the tools it ran with their input in short, their results, its questions, errors, and the end), keeps the steps of running jobs in memory, and saves each attempt's steps in the store's state, where the last 500 jobs' steps stay readable. Dashboards hear about new steps and status changes through `job.progress` events that reach only the event stream (`EventBus.notify`), at most once a second per job. Steps are internal: they carry the agent's words and commands, so they are shown only to people who may view the job's repository.

**Agents' questions.** A permission prompt or a question from an agent (Claude's `can_use_tool`, Codex's approval and `requestUserInput` requests, ACP's `session/request_permission`) becomes an `AgentQuestion`: what the agent wants (command, files, tool input, or a list of questions with options), the runtime, task kind, repository, and PR. Auto mode treats every PR the same, whoever wrote it. The runtime first applies its automatic rules; a `restricted` run is then denied; otherwise the question goes to the **question desk** (`engine/questions.py`), and the CLI waits.

The desk stores the question, publishes `question.asked` to the dashboard's event stream, and alerts the channel the repository's profile names for `agent_question` (or `default`) in its `notify` map. Maintainers of the repository (write access or higher) see it on the Questions page with a banner and an optional browser notification, and answer: allow, allow for the rest of the run where the CLI offers it, deny with a message the agent reads, or answers to each question. Questions outside any repository go to operators. The answer is recorded in the audit log and written back to the CLI. While a question waits, the task's own timeout stops counting; the question has its own, the route's `question_timeout_seconds` (30 minutes by default), after which it is denied. The desk keeps the question in the store and the waiting side watches the store, so a question raised by an agent in another process (`am repo add`) is answered from the dashboard of `am serve` just the same. Questions whose waiting process is gone are cancelled when the service starts.

A question from a job a person asked for (a requested review or a fix) carries `asked_for`: the dashboard alerts only that person and puts it first in their feed, though any maintainer of the repository can still answer it. Before any of this, a question about posting to the forge or pushing (a `gh` write, `git push`, or a question whose text asks whether to post or publish) is denied with a fixed answer (`agents/approver.py:posting_request`): the service decides about posting after the agent returns, so nobody is asked.

**Instructions and skills.** Agents start in a worktree of the target repository at the PR head, so they discover the repository's own agent instructions and skills the way they do for a human (`AGENTS.md`, `CLAUDE.md`, `.claude/skills/`, `.agents/skills/`). Before the agent starts, those files are replaced with their versions from the default branch, so a PR cannot rewrite the instructions it is reviewed with. The service's skills from `skills/` and from the profile's packs are added through each runtime's directory flag (`--add-dir`). The profile's internal and public languages, style guide path, and review template are passed in the prompt.

**Doctor.** `am doctor` (and a nightly job) probes each runtime with a tiny schema task and records version, auth state, latency, and whether structured output works. A new CLI version that breaks an adapter disables that runtime and opens an issue on the code repo.

### 5.8 Policy

The policy engine decides, for each action, one of `execute`, `propose`, `shadow` (record only), or `deny` (with a reason). Inputs: the action, the autonomy level for its kind in its repository, conditions on the target (author association, team membership, labels, paths), rate limits, the pause state, and freeze windows.

Policy is a pure function of its inputs: it takes the `PolicyConfig` from the repository's approved profile, plus rate counters read from the store, and it performs no I/O itself. An approved proposal is decided again at execution time, so a pause, a hard limit, or a freeze still applies to it.

Autonomy levels per action kind: `off`, `shadow`, `propose`, `auto`. Conditions narrow a level, for example `auto` for `label.ci_gate` only when the author is a team member and the review recommends the profile's "ready" value. A ceiling from the in-repo profile file caps the level whatever later layers say.

Hard limits that no config can raise:

- At most one review and one hygiene comment per PR per head SHA; later changes edit the existing comment.
- The service edits and deletes only its own comments and threads, found by its marker.
- No `@` mentions in public text except the PR author and the `CODEOWNERS` of touched paths.
- `INTERNAL` content never reaches a public action.
- No commits or pushes to target repositories by the service's own account, and no approving reviews. The one push into a target repository, `pr.fix`, runs only when a maintainer approves it as themselves, with their token and their sign-off.
- A global cap on public writes per hour, and one per repository; exceeding either pauses the scope and notifies maintainers.

The **leak guard** builds `PublicText`. It rejects text that contains internal fields (scores, tiers, rubric terms, the profile's internal entity names) and text that is not in the profile's public language, then runs the style checks the profile names (by default a short list of filler phrases; a pack or the repository's style guide can add more).

Rollout: a new action kind or a new repository starts in `shadow`. The dashboard shows the **agreement score** per action kind and repository: for each shadow action, whether a maintainer later did the same thing (applied the same label, reran the same job, merged at the same SHA). Maintainers raise the level in settings; the change goes through the configuration gate (2.3) and records who approved it. Service defaults keep every action that touches a repository at `shadow` (8.3), so nothing reaches `propose` or `auto` without the repository's maintainers.

### 5.9 Actuator and identity

The actuator executes allowed actions with one handler per action kind. Each handler:

1. Checks the idempotency key against the `actions` table and returns the earlier result if it already ran.
2. Re-reads the target (for example, the PR head SHA) and refuses stale actions, marking their proposals `superseded`.
3. Resolves profile references: a role label becomes the repository's label name; a missing mapping makes the action a no-op with an audit entry.
4. Records an intent, executes, then records the external reference (comment ID, URL, run ID) and writes the audit log. An intent without a result after a crash is reconciled before any retry (4.10).
5. Registers an undo where the forge allows it: remove a label, delete or restore a comment body, cancel an armed merge.
6. Emits `action.executed` or `action.failed`, which wakes any job waiting on the action.

Writes to one repository go through one ordered queue (4.6).

Every public comment starts with a marker that later runs parse to find their own work, and ends with a footer that states AI assistance:

```
<!-- auto-maintainer: v=1 kind=review pr=42 sha=<40-hex> action=<id> -->
...
<sub>Drafted by auto-maintainer with Claude Code, approved by <login>.</sub>
```

The approver's login is written without `@`, so nobody is pinged. The footer text is a profile setting. A profile can list `legacy_markers` (regexes) from tools the service replaces, so existing reviews keep their state.

**Acting as a person.** Signing in to the dashboard (authorizing the GitHub CLI) keeps that person's token in a file only the service user can read (`user-tokens.json`), keyed by their login; signing out deletes it, and a token kept from the removed GitHub App sign-in is forgotten once it expires. One reviewer's token is used only for what that reviewer approves as themselves (`post_as` must be the approver), never for the service's own reads or writes and never for another reviewer. The file guards against other users of the host, not against processes of the service's own user: an agent runs as that user and can read it unless its runtime has a confining `command_prefix` (5.7). When a maintainer approves `review.post` or `pr.fix` as themselves, the actuator builds a forge writer, and for `pr.fix` a publisher, from that token, and records `posted_as` with the result. Someone signed in another way (a one-time `am login` link) has no token and approves as the service.

**Identity.** The service acts as one GitHub account, through a GitHub CLI login made on the setup page (`github.gh_config_dir`) or, without one, the host user's own. For a team, that is an account made for the service (a machine user), so what it posts is not under a person's name. It needs write access on each target repository (to post reviews, label, and rerun CI) and on the memory repository. GitHub App and pasted-token setups were removed: a host set up with one opens the setup page on its next start, with its other answers kept, and must connect the GitHub CLI before the service starts, so that it never switches silently to whatever account the host's own GitHub CLI is logged in to (`config.legacy_github`). The profile's `people.review_identities` lists every login whose reviews count as the service's own when classifying review state, which lets a repository carry over reviews made under an earlier account, such as an old App's bot.

### 5.10 Labs

A lab, or test machine, is a host where the service may run PR code, reached locally or over SSH. The machine and its use are owned separately. **The machine** belongs to whoever owns the host's access: **test machines** in the service settings (`labs:` in `config/service.yaml`, 8.1), which maintainers of the service's repository add on the Settings page or by asking the agent, or hosts in `local.yaml`. A machine is edited in three parts: its name; how to reach it, either this host or over SSH as an `~/.ssh/config` entry (`HostName`, `User`, `Port`, `ProxyJump`, `IdentityFile`; `parse_ssh_config` refuses every other keyword, `ProxyCommand`, `LocalCommand`, and `Match exec` among them, since they run commands on the service's host); and optionally the only repositories it is offered to. Its environment and rules are written in plain language (`description`): the hardware, how to set up the environment, where data lives, what runs must not do, how long they may take. An agent drafts from that text what the service enforces (`POST /api/settings/labs/<name>/draft`): the run folder, the setup run before every reproduction, the time limit, patterns no script may match, and the GPU thresholds, plus the rules none of these can enforce. The page shows the draft and the settings diff before Save applies it through the gate, and the reproduction agent reads the description itself and follows the rest. A check connects and reports the machine's system and idle GPUs. The service reaches it as its own system user, with `-o` options built from the checked fields, and the host comes after `--`, so nothing can be read as an ssh option, which is why a repository's maintainers cannot add machines themselves: the connection uses the service host's keys. **The use** belongs to each repository's maintainers, in its profile's `labs.<machine>` and through the configuration gate (2.3): a repository uses only the machines its profile lists with `use: true` (opting in, since its environment is its own), and adds its environment setup (run after the machine's), a shorter time limit, extra forbidden patterns, protected paths, and for template runs the base clone, worktree paths, environment rules, and test templates (Appendix A.3 has an example). The repository's page lists every machine offered to it, with a switch and these settings, and the Reproduce button offers only the machines the repository uses (or to turn one on). The profile doctor flags a machine the profile uses that does not exist or is not offered to the repository.

- **Resource lease.** A GPU (or another declared resource) is leasable when it is idle by the lab's thresholds: by default less than 500 MiB of memory used, 0% utilization, and no compute processes. The lease is an `flock` on the lab's lock directory with a heartbeat and a TTL, and the idle check runs again immediately before the command starts. If nothing is free, the run falls back to the CPU-only variant of the template when one exists, and the review says GPU tests were deferred.
- **Command guard.** Lab commands come from templates, not free text. The guard rejects patterns from the lab, packs, and profile (for example `ray stop`, which stops every Ray job on a host), `rm` outside the run's worktree and throwaway environment roots, writes to protected paths, and writes to the base clone.
- **Worktrees.** One git worktree per PR under the profile's worktree root, created from the base clone, never by modifying the clone itself.
- **Cleanup** is idempotent: remove the worktree, remove throwaway environments, release the lease. It runs on success, failure, timeout, and at startup for leftovers.
- **Untrusted code.** Running an external contributor's code is `lab.run` at `propose`: a maintainer looks at the diff first, like GitHub's approval for first-time contributors' workflows.
- **Reproductions.** A maintainer asks for one on a PR and picks a machine, or a review starts one (`review.reproduce`) once it is drafted: when the repository uses a machine that can take it, its `lab.reproduce` rule lets the PR run or be proposed for it (`policy.engine.level_for` with the author), and no reproduction is on record or underway at that head (`playbooks/reproduce.py:auto_reproduction`). An agent reads a checkout of the head (read-only, and it may ask the maintainer who asked) and writes setup commands, a script, which sides to run, and what each result would mean. It reproduces the way a user would: for a PR that adds or changes a documented workflow or claims a result, the script follows the PR's documentation step by step without changing the code, printing each step so the first failure and anything the docs left out show, and the functional and metric results are read separately; for a plain fix, the smallest check that settles it, on the code before the PR and at its head. When the machine cannot run it at all (missing hardware such as GPUs, an accelerator, or a robot, or assets or access the steps need), the agent says so instead (`blocked`). The playbook checks the script against the built-in patterns (`domain/labs.py`: sudo, shutting down, `ray stop`, killing others' processes, writing to devices or system directories, removing a root or home directory), the machine's and the repository's `forbid` lists, and the repository's protected paths. A maintainer's request becomes `lab.reproduce` drafted for them, and nothing runs until they approve it, as written or edited. A review's request is decided by the repository's rule: the recommended rule (`level: auto`, `conditions: {author: team}`, `otherwise: propose`) runs a team PR's script at once and holds an outside contributor's for a maintainer's approval, like GitHub's approval for first-time contributors' workflows. The actuator then checks the PR out on the service host and streams the code before the PR (the merge base) and at its head to the machine as archives over the SSH session, so the machine never gets a credential and private repositories work. Each side runs in a fresh folder under the machine's `workdir` (leasing an idle GPU first when the script needs one), with the machine's `setup`, the repository's, the script's setup, and the script, under the shorter of the machine's and the repository's time limits; the folder is removed whatever happens. The last lines of each run's output, scrubbed of secrets, go back to the agent, which concludes reproduced, not reproduced, or inconclusive. For a maintainer it drafts a comment. For a review it adds the result to the review draft while that waits for a decision (an agent revises the review from the run's record, as the conversation about a review does, and the service saves it without counting it as a person's edit); once the review was posted, the result becomes a comment of its own. A review's reproduction that did not run (blocked, refused by the patterns, not approved, or no free GPU) is added to the draft the same way, with what a run would need. These checks keep a script from hurting other users of a shared machine by accident; they do not confine it. The approval, or the rule that trusts the repository's team, is the guard against a script that means harm, and machines that need more isolation run reproductions under a user of their own or in a container (`setup`).

### 5.11 Memory and learning

**Memory repo.** A private repository (for example `<org>/auto-maintainer-memory`), cloned on the host, with deployment-wide settings, global knowledge, and one directory per target repository (layout in 7.2). It is separate from the code repo because it holds confidential material, changes far more often, and follows a different review policy. A deployment can map repositories to different memory repos. Only the service can push to a memory repo's default branch; every other change arrives as a PR and is merged by the memory gate (2.3).

**Signals** are what the service learns from:

- a maintainer's priority override and its reason;
- the diff between a draft and what the maintainer approved;
- a rejected proposal and its reason;
- the fate of a finding (author fixed it, maintainer dismissed it, thread resolved without change);
- PR outcomes (merged, closed, time to merge) against the predicted tier;
- CI diagnoses that a later rerun or fix confirmed or contradicted;
- explicit chat instructions ("remember that ...", "from now on ...").

**Tiers** decide how a memory change lands:

| Tier | Examples | How it lands |
|---|---|---|
| T0 journal | observations, decisions, CI outcomes | Appended to the repository's `journal/`, committed in batches to `main`. |
| T1 working knowledge | stage goals, label keyword rules, flake signatures, review lessons, contributor notes | PR on the memory repo. If the repository's configuration allows it (`memory.knowledge_auto_merge`) and the change comes from that repository's own signals, the service merges it after a notice window (24 hours by default) unless a maintainer objects, or at once when a maintainer asked for it in chat. Otherwise a maintainer approves it. |
| T2 configuration | profile layers and the lock: rubric dimensions and weights, policy and autonomy, routing, checks, labs, role labels | PR on the memory repo, merged by the memory gate after a maintainer of each affected repository approves it (2.3). |
| Code | playbooks, adapters, packs, skills, prompts | PR on the code repo; a maintainer of the code repo merges it after CI and replay evals pass (5.12). Changes to defaults or packs reach each repository as a candidate for its lock. |

The **learner** (`playbooks/learn`) batches signals per repository and asks an agent for memory changes. Priority overrides go through calibration (5.6). Knowledge that holds in more than one repository is proposed for `global/`. A repository uses global knowledge only if its configuration opts in (`memory.use_global`), and opting in is its maintainers' consent to receive those updates under their own T1 rules. Content from a repository's private files never moves to global memory or to another repository.

Each knowledge entry records `id`, text, source signals, `added_by`, `added_at`, optional `expires_at` (stage goals expire after 30 days by default), and confidence. A weekly **compaction** job merges duplicates, flags contradictions, drops expired entries, and keeps every `MEMORY.md` index under 200 lines.

Skills whose content depends on memory, such as the rubric section of `pr-priority`, are rendered per repository from memory at task time rather than edited in place.

### 5.12 Self-evolution and self-update

The service grows in two ways. Its memory grows from what reviewers teach it (5.11). Its code grows from what they ask for: a reviewer asks in chat for something the service cannot do yet, the service changes its own code, and its maintainers decide whether that code is merged and when the service runs it.

**From a conversation to a PR.** When a request needs code rather than a setting or a lesson, the chat agent proposes `improve` with the request spelled out (5.13). The answer shows it as a request card; nothing starts until a user whose live permission on the code repository is at least `self_update.approve_permission` clicks **Approve and start**, on the card or under **Requests from conversations** on the Approvals page, where those users see every such card (the person who asked may decline it). A conversation has at most one such request pending or running. The PR's commit carries `Requested-by:` the person who asked, and its description names who approved the start. Then the `self_improve` playbook starts:

1. It prepares a worktree of the service's repository (`code_repo` in `local.yaml`) at the tip of its default branch (`self_update.branch`, `main` by default).
2. An agent on the `self_improve` route, in `workspace_write` mode and auto permissions, implements the request following that repository's `AGENTS.md`, adds tests, and updates the docs it changes. Whatever it would ask a person goes to the question desk like any other agent run (5.7), answered by maintainers of the code repository.
3. The playbook collects the change as one patch (`Workspace.changes`) and runs the tests (`uv run --frozen pytest -q -x`) and Ruff itself in the sandbox, whatever the agent reported. The editing agent does not have to answer in a fixed form (long edits often end without one): the diff is what it did, and a second, read-only task on the same route describes the diff as a PR title and summary, falling back to the request and the agent's closing message.
4. It emits a `code.pr` action. The actuator applies the patch to a fresh worktree of the base commit, commits it as the service with a `Requested-by:` trailer, pushes the branch `am/self/<slug>-<id>`, and opens a PR on the default branch whose description holds the request, the summary, both check results, and the agent's own account. The chat thread gets a link.

A request the agent declines, or one that ends with no change, opens no PR; the thread is told why. A run whose agent fails for a reason a retry would not fix ends the job as failed; a timeout, a quota, a crash, or every agent paused is retried with backoff like any job.

**Only maintainers merge and deploy.** Two separate approvals, each by a user whose live permission on the code repository is at least `self_update.approve_permission` (`maintain` by default), checked at the moment of the click:

- **Merge.** The Service page lists open PRs on the code repository, marking self-improvement PRs; **Approve and merge** submits a `code.merge` action, which the actuator executes with a squash merge. Merging on GitHub itself works the same way, under that repository's own branch protection.
- **Deploy.** The Service page shows the commit the service runs and the commits on the default branch it does not run yet. **Deploy** submits a `service.deploy` action for the tip of the branch. A merge never deploys by itself.

Both actions carry the approver in `requested_by`, and their records and outcomes show on the Service page and in the audit log.

**Deploying.** The `ServiceUpdater` (`selfupdate/updater.py`) runs the service from its own git checkout and only moves it to commits on the default branch. A deploy:

1. refuses a checkout with local changes and a commit that is not on the default branch of the remote;
2. checks the commit out in a staging worktree under the data directory, runs `uv sync --frozen` and the test command there (`self_update.test_command`, the unit tests by default), and stops on any failure, leaving the running code untouched;
3. fast-forwards the checkout's branch to the commit (or checks it out detached), syncs the environment, and rebuilds the dashboard when Node is available (a failed dashboard build keeps the previous build);
4. records the previous commit in `deploy.json` and asks the service to restart: `am serve` exits with code 75.

`am supervise` runs `am serve` and starts it again after exit code 75. `am start` runs the supervisor in the background in a session of its own (its process id in `<data>/am.pid`), with `--log-file`: the supervisor then writes everything the service prints, and its own messages, to `<data>/logs/service.log`, rotated by size; being the only writer, it rotates safely. `am logs [-f]`, `am status`, and `am stop` (one SIGTERM, which the supervisor passes on gracefully) work on that background service, and the Service page shows the end of the log, scrubbed of credentials, to maintainers of the service's repository. If the service dies before it confirms the deploy (it does after `confirm_deploy_after`, 60 seconds of running), the supervisor checks out the previous commit, syncs the environment, and starts the old code; the service then records the rollback in the audit log. Any other failure restarts with a growing delay; exit code 0 stops the supervisor. Ctrl+C or SIGTERM stops the supervisor only after the service: it gets one SIGTERM and 20 seconds, and a second Ctrl+C kills it. The service itself never waits on an open dashboard when it stops: its live event streams end at once, other connections get five seconds, and stopping the workers gets fifteen. Under systemd, run `am supervise` as the unit's command.

Database migrations run when the new code starts. A release never changes a repository's configuration by itself: when new defaults or packs change a repository's merged profile, the repository keeps its current lock and its maintainers get a candidate to approve (5.2).

### 5.13 Web dashboard and API

Authentication uses the GitHub CLI, the only way to sign in with GitHub: the person enters a one-time code on github.com to authorize it, and its token can do what that person can do on every repository they reach. Each sign-in runs `gh auth login` in a folder of its own under `<data>/sign-in/` (mode 0700, with git's global configuration redirected into it and `--insecure-storage`, so the token never lands in the host's own `gh` configuration or in the system keyring every `gh` on the host shares); the service reads the token, deletes the folder, and keeps the token under that login only. Folders left by a sign-in that never finished are removed when the server starts. `gh` is looked up on the server's `PATH` on every visit; when the host has none, the sign-in page and the setup page offer to install it (`ghcli.install`): the latest release of `cli/cli` for the platform is downloaded into `<data>/bin`, checked against the release's checksums file, and put first on `PATH`, with nothing else on the host changed and no administrator rights. The install route is open before sign-in, since nobody can sign in without it, and does nothing once `gh` is there. On the host, people can also sign in with one-time links that `am login` prints: a link signs in once, within ten minutes, as the GitHub account logged in to the GitHub CLI there, so only someone with access to the host can mint one. Sessions are HMAC-signed cookies. The user's permission on each configured repository is cached for 10 minutes. A user sees only the repositories where they have write access or higher. On those, they can view everything, approve proposals, override priority, arm merges, and chat. Changing anything in a repository's configuration (profile, autonomy, rubric, routing, labs, notification routes) goes through the configuration gate (2.3), which requires `access.configure` on that repository at the moment of the change. Changing the service settings goes through the same gate against the service's own repository. Operators, listed in `local.yaml`, can pause the service and see the host's health; they see a repository's data only if they also maintain it.

The passwordless sign-in `/auth/dev` exists only in `am serve --demo`, which runs on fakes. An instance that talks to a real forge never offers it, in dev mode included, because anyone who reached it could sign in as a maintainer and pass the configuration gate.

**Layout.** A top bar holds the pages in order of daily use (Home, Pull requests, CI health, Repositories, Approvals, Agent questions, Settings, then the rest, with Memory, Service, and the Audit log last): as many as fit the window, measured as it resizes and in the current language, with the rest under More; a search and command palette (Ctrl or Cmd+K) that reaches every page, repository, PR, and command, and hands anything else to the agent; a bell with what waits on the user; and the account menu (settings, theme, language, keyboard shortcuts, pause). Every list (PRs, proposals, questions, memory changes, CI failures, runs, the audit log) has the same controls: search, filters by facet, sorting by any column, grouping, paging, selection with bulk actions, and keyboard navigation (`j`, `k`, Enter, `x`, `/`). The view lives in the URL, so it can be shared and survives the back button. The agent is on every page (Ctrl or Cmd+J, or Ask agent): it opens beside the page and knows what the page shows (see Chat below).

Pages:

- **Home**: the control center, in one column. With several repositories, the page shows one at a time, chosen with a switch (`?repo=`, remembered in the browser and shared with the Pull requests page), because each repository's priorities come from its own rubric and do not compare or add up across repositories; questions and changes that belong to no repository show under every one. A status line (decisions waiting, PRs to review, top priority, agents working, the service). Until it is done or hidden, a getting-started checklist whose steps carry their own buttons, from adding a repository to the first reviews. **Agents' work**: the user's agent jobs (or everyone's) as needs you, working, and finished; each opens the job's live view. A repository's priority scoring shows as one line with how many PRs are scored, opening its progress on the Pull requests page, since one request scores many PRs. The PRs that need review, with checkboxes, an agent picker, and one button that reviews the selection; each row shows the PR's quality (its review's score, or the quick one at a glance), when it has one, and who is reviewing it with which agent and opens that review's live view. Under it, when reviews run in each repository (only when asked, every N hours up to M PRs, or on every update, and whether new commits are re-reviewed), changeable in place through the configuration gate; someone who does not pass it proposes the change instead. Last, one feed of every decision (questions for the user first, then other agents' questions, approvals, memory changes, and service updates), each answerable in place.
- **Pull requests**: the ranked review queue across the user's repositories, with saved views (all, needs review, mine, not evaluated, reviewed), a quality column (the review's score in its band's color, or before a review the quick score at a glance, outlined dashed; the agent's reason on hover; faded once new commits arrived since it was given; Score quality on a selection or, after a confirmation, the whole view scores it now: a reviewed PR from its review, any other quickly, and several at once in the background pool so they never hold up a review; Score priority is the same for priority), filters for repository, tier, review state, quality, author, label, and claim, one repository at a time behind the same switch as Home (so PRs rank only against their own repository's), review in bulk, Score again (a selection, or every PR in the view after a confirmation, since each is one agent run; while it runs, a bar above the list counts the PRs scored, and each agent's job opens its live view), and Sync with GitHub, which reads every repository's PRs now, in the background, instead of at the next poll.
- **PR**: the description and the service's reviews rendered as GitHub renders Markdown, in full; checks, files, timeline of the service's actions, and CI diagnoses, beside the quality (the review's accurate score, or the quick one at a glance before it, with its band, the agent's reason, the quick score kept beside the review's, and a button to score it again; the band also shows under the title) and the internal priority (with an override and reason, and Score again), the merge checklist, and hygiene results; claim, review with a chosen agent, fix the review's findings with an agent, reproduce it on a test machine, **Run CI**, arm a merge, read the PR from GitHub now (the refresh button). Run CI does the one step that gets CI going at the head (`domain/run_ci.py`): it adds the `ci_gate` label when the repository starts its full CI on one and the PR lacks it, and otherwise reruns the failed runs whose jobs are done; while CI runs, or once it passed, the button says so instead. The press is the person's approval: the action is drafted for them and approved at once, so it runs even where the level is `shadow`, while a pause, the gate budget, the rerun limit per commit, and the hard limits still apply, and the audit log names them. Above the tabs, **On this PR** lists every agent job on it, anyone's (reviews, fixes, reproductions, priority scoring; running ones first, then the last week's), each opening its live view, and every draft for it waiting for a decision, decidable in place. A draft can be edited in a full-width editor and saved without posting (the draft keeps the edit, marked edited); posting sends the saved version. **Discuss** on a review draft opens the conversation about that review, where the agent can revise it. Jobs that ran no agent and skipped (a follow-up for a PR with no review yet takes the same request as the review) are left out here and on Home, and so is scoring nobody asked for until an agent works on it (most PR updates only recompute the total).
- **Repositories**: onboarding; per repository, the approved profile with the hash of each layer, what the service does on its own (each action kind's level, `off`, record only, ask first, or automatic, changeable in place through the gate, beside its agreement score from shadow actions), a dedicated **Test machines** tab with the repository's `lab.reproduce` policy presets (`off`, record only, ask first, or automatic for team PRs and ask first for others) and each offered machine's connection check and inline environment settings (`setup`, `timeout_seconds`, `forbid`, `protected_paths`) (5.10), the pending candidate with its differences in plain words (each changed setting under its section with a readable label, was and now, unchanged sections folded, the YAML diff one click away; before a first approval, the whole profile by section, actions grouped by level), how its reviews read and score (the writing and quality guides, each the service's default until a maintainer changes it through the gate, and whether every open PR gets a quick score), per-repository pause, and the repository's settings layer, changed by asking the agent or by editing it. Saving opens a memory change that applies once a maintainer of that repository approves it; when the editor qualifies, saving is that approval.
- **Approvals** (proposals): every pending proposal, shown as it will look: Markdown rendered the way GitHub renders it (or as source), each inline comment under the diff lines it is about (and flagged when the line is not in the PR's current diff), a reproduction's script, a fix's diff, a plain sentence for labels, reviewers, and reruns. Each card has one main button that says what approving does (Post, Post as me, Run on <machine>, Open the PR as me); editing, rejecting, and handing a review to an agent to fix are secondary. Inline comments can be edited or removed one by one. Approvable in bulk.
- **The live view of a job** opens beside any page (`?job=<id>`, or the id of the request that starts it): who asked, which agent, its steps as they happen (messages, the commands and files it used, questions and how they were answered), the questions it waits on, answerable there, what it produced, answerable there too, and its outcome. The person who asked, or a maintainer of the repository, can stop it; its agent process ends and its checkout is released.
- **The conversation about a review** sits on the live view of a review or follow-up once it has a draft (`web/review_talk.py`), and every draft of a review has a **Discuss** button that opens it. Everyone who may see the repository shares it. A reviewer asks the agent why it found something, disagrees with a finding, has it change the draft, or has it check more (other files, a test, the docs). Each turn runs an agent, on the review's route and preferably the agent that wrote the review, in a checkout of the reviewed commit kept for the conversation, with the PR and its diff, the repository's review skill and format, the reproduction on record, the lessons, the review as structured data, the draft as it would be posted (reviewers' edits included), and the conversation so far; each turn carries all of it because not every CLI can resume a session. Its questions go to the reviewer who wrote the message and wait in the conversation, as the review's own questions wait on the live view. A turn that changes the review returns it whole; the service renders it as the playbook does and saves it on the pending draft through `ActionPipeline.save_edits`, as an edit by that reviewer, so the findings a fix or a follow-up reads change with the text, and the leak check reads the new text. A draft already posted or rejected is not changed; the agent still answers. Lessons the reviewer states or agrees to are saved as chat lessons are. One turn runs at a time per conversation. Nothing is posted from a conversation: the draft waits for a decision as before.
- **Agent questions**: what agents are waiting on (5.7), with what each wants, the PR it works on, and when the question expires; answer with allow, deny and a message, or answers to each question. A strip and a toast announce new questions, with an optional browser notification, and the bell counts them.
- **Memory**: memory changes waiting for approval (a change that locks a profile shows what it changes the same way), and a browser over the memory repository.
- **Settings**: the service settings (8.1) in one place: agents with their status, model, provider, and budget; model providers with their keys' status and a connection test that lists the endpoint's models without spending tokens; the default route per task; system values; test machines with a connection check, inline editing, and a repository picker over onboarded repositories; and the settings file itself. Each row shows its current value and whether it differs from the default. The intended way to change them is to tell the agent what should change: it drafts the edit, the page shows the diff, and a maintainer of the service's repository applies it. API keys go only into their key fields; they are never shown again.
- **CI health**, **Agents** (runs, outcomes, duration, cost), **Conversations** (every thread with the agent), **Service** (5.12), and the **Audit log**.

The UI is in Chinese by default, with a per-user toggle to English; public text on the forge always uses the repository's public language.

API sketch (JSON, all under `/api`, SSE for streams):

```
GET  /repos                              POST /repos   (onboard)
GET  /repos/{owner}/{repo}/profile       POST /repos/{owner}/{repo}/doctor
GET  /prs?repo=&tier=&state=&mine=       GET  /prs/{owner}/{repo}/{n}
POST /prs/{owner}/{repo}/{n}/override    POST /prs/{owner}/{repo}/{n}/claim
POST /prs/{owner}/{repo}/{n}/merge       DELETE /prs/{owner}/{repo}/{n}/merge
POST /reviews  (PRs, agent)              POST /prs/{owner}/{repo}/{n}/review|fix
GET|PUT /repos/{owner}/{repo}/review-automation
GET  /proposals?state=pending&repo=      POST /proposals/{id}/approve   (optional edited payload, as_me)
POST /proposals/{id}/reject              (reason required)
GET  /ci/flakes?repo=  GET /ci/runners   POST /playbooks/{name}/run
GET  /chat/threads  POST /chat/threads   POST /chat/threads/{id}/messages   (SSE reply)
GET  /memory/tree  GET /memory/file      GET  /settings  PUT /settings  POST /settings/check
PUT  /settings/secrets/{name}            POST /settings/providers/{id}/test
GET  /work?everyone=&repo=&number=       GET  /jobs/{id}   POST /jobs/{id}/stop
GET  /prs/{owner}/{repo}/{n}/diff        POST /prs/{owner}/{repo}/{n}/reproduce   POST /prs/{owner}/{repo}/{n}/ci
GET  /labs?repo=                         PUT /repos/{owner}/{repo}/labs/{name}   (how the repository uses it)
GET  /repos/{owner}/{repo}/agreement     PUT /repos/{owner}/{repo}/policy/{kind}   (an action kind's level)
PUT|DELETE /settings/labs/{name}         POST /settings/labs/{name}/check
GET  /agents  GET /runs/{id}             GET  /audit    GET /events (SSE)
POST /pause  POST /resume                (optional ?repo=)
GET  /healthz
POST /webhooks/github                    (outside /api, HMAC-verified)
```

**Chat.** Each thread belongs to one maintainer and runs on the `chat` route (ACP when available, otherwise exec with session resume). The agent receives the global and per-repository `MEMORY.md` indexes for the maintainer's repositories, the maintainer's role, and a snapshot of their queue, and it uses the service's MCP server:

- read tools: `list_repos`, `get_profile`, `search_prs`, `get_pr`, `get_evaluation`, `get_ci_status`, `list_proposals`, `read_memory`, `get_run_transcript`;
- propose tools: `propose_action`, `propose_memory_change`, `propose_override`, `run_playbook`.

Until the MCP server exists, the chat agent returns its proposals as structured output (`ChatAnswer.proposals`), which the service turns into the same proposals. Proposals created in a thread appear inline with approve and reject buttons, so the maintainer sees exactly what will be posted before it is. A configuration change proposed in chat applies only after a maintainer of that repository approves it, which the chatting maintainer can do if they qualify. A request the service cannot fulfill with a setting or a lesson becomes an `improve` request that changes its own code (5.12), shown as a request card that waits for a maintainer of the service's repository; `add_repo` offers to onboard a repository the same way, approved by someone who may configure it. Those who may approve a card see it on the Approvals page as well as the person who asked in the thread. The chat agent follows a guide to the service and the dashboard (`skills/maintainer-chat`), so it points to what exists instead of asking for code.

**The agent on every page.** A conversation works in a checkout of its repository (at the PR's head when it started on a PR); one outside any repository works in a checkout of the service's own code at the commit it runs, a worktree apart from the running checkout, so questions about the service are answered from its code and nothing the agent does there changes what runs. The panel sends only the page's identifiers (the page, a repository, a PR number); the service reads that page's data itself, so the agent sees what the maintainer may see and nothing the browser could invent. Each page keeps its own conversation for the browser session, and every conversation is also on the Conversations page. The agent receives the service settings, their defaults, and which secrets are set (never their values). It drafts configuration as a `configure` proposal holding the complete new document, either the service settings or a repository's settings layer; the service checks it and shows it in the thread with its diff and an Apply button that goes through the gate. Text that looks like a credential is removed from a message before it is stored or sent to the agent, and a drafted document that contains one is refused. While the agent answers, the panel shows its steps as they happen (its thinking where the CLI reports it, tool calls and their results), the same steps a job's live view shows; each answer keeps them behind "What the agent did". What an answer started stays under it as a card with its outcome: an action or configuration draft waiting for approval, a lesson (saved at once when the asker may configure the repository, else waiting for a maintainer), or a self-improvement job, which starts at once, opens in its live view for whoever asked, and runs one at a time per conversation.

### 5.14 Notifications

Adapters for Feishu/Lark bots, Slack webhooks, and email. Routes are configured per event type (a top-tier PR arrived, a proposal waiting more than N hours, a runner offline, a scope paused, a deploy failed, a digest) and per repository, so each repository's maintainers get their own channel. Quiet hours hold non-urgent messages.

### 5.15 Observability and audit

- Structured JSON logs with repository, job, run, and action IDs on every line.
- Agent transcripts stored per run under `runs/<id>/` with a retention period; secrets are scrubbed before writing.
- The audit log is append-only and records every action: decision, actor (service or maintainer), approver, payload hash, external reference.
- Metrics on `/metrics` (Prometheus format), labeled by repository: jobs by state, agent latency and usage, forge rate limit, proposals waiting, agreement scores.

## 6. Main flows

The flows use `acme/widget` with the profile from 2.2.

**Onboarding.** Covered step by step in 2.5: `am repo add`, the `onboard` playbook drafts a profile, the doctor checks it, the repository's maintainers approve it, and the repository starts in shadow mode.

**New PR.**

1. The poller sees PR #42 open and in scope, and emits `pr.opened`.
2. `triage` builds the snapshot, classifies `NEW_REVIEW`, asks the `pr-triage` agent for a summary and labels under the profile's rules, and searches for overlap. It emits `label.apply`, and `evaluate` and `hygiene` run. The PR appears in the queue, ranked.
3. A reviewer selects #42 (and any others) in the queue and chooses "Review selected". With `review.trigger: scan` the next due scan picks it instead; with `events` this step happens on `pr.opened`.
4. `review` creates a worktree at the head SHA with default-branch instructions, and runs the routed runtime with the profile's review skill, the repository's review lessons from memory, and the headless instructions. A verifier pass checks each finding against the code and drops what it cannot confirm.
5. The draft passes the leak guard and becomes a `review.post` proposal in the reviewer's inbox. The reviewer edits and posts it; the actuator posts one review with the marker and footer.
6. If the recommendation means changes are needed, `label.apply` with role `changes_requested` follows. If it means the PR is ready, `label.ci_gate` (when the profile maps that role) and `pr.request_reviewers` follow under their own policies.
7. The diff between the draft and the posted review becomes a signal.

**New commits on a reviewed PR.**

1. `pr.synchronized` reclassifies the PR as `FOLLOWUP_REVIEW` (or `APPROVED_NEW_COMMITS`), and the queue shows it.
2. When a reviewer selects it (or the scan or events trigger picks it), `followup` reads earlier findings from the service's comments and threads, diffs the last reviewed SHA to the head, and asks the agent for each finding's status plus new findings.
3. It drafts `review.update` (fixed findings marked resolved with the fixing SHA, open ones kept, new ones added under their own heading) and `thread.reply` on resolved threads. When everything is resolved, the update says so.

**Flaky CI.**

1. `checks.completed` with a failed job on the head SHA starts `ci_doctor`.
2. A known signature matches (for example, a package download timeout from a global pack); the rerun budget allows it; `ci.rerun_failed` executes after the backoff.
3. The rerun passes. The signature's hit count rises, and the journal records the run.

**Auto-merge.**

1. A maintainer approves the PR and comments `/am merge`.
2. `merge` evaluates the gate. Suppose the profile maps `ci_gate` and the full CI has not run: it proposes `label.ci_gate`, or executes it if policy allows.
3. CI fails on a flaky job; `ci_doctor` reruns it; CI passes.
4. The gate holds at the head SHA. `pr.merge` merges with the profile's message template and preserved trailers and posts a short merge note.

**Priority override.**

1. A maintainer moves #42 from P2 to P0 with the reason "partner integration due this week".
2. `calibrate` sends the PR, the before and after scores, the reason, the repository's rubric, and its recent overrides to the calibration agent, which returns `STAGE_GOAL_UPDATE` with a new stage goal and expiry.
3. The stage goal goes out as a T1 memory PR. It comes from the repository's own maintainer and `acme/widget` lets knowledge merge on its own, so it merges after the notice window. Every open PR in the repository is rescored, and the next `pr-priority` task renders the new stage goal.

**Convention change.**

1. A maintainer of `acme/widget` merges a PR that adds a `Signed-off-by` requirement to `CONTRIBUTING.md` and a DCO workflow.
2. The poller emits `repo.files_changed`; `profile_drift` proposes adding the `dco` pack and the DCO workflow to `ci.required_workflows`.
3. A maintainer of `acme/widget` approves the change; the memory gate merges it and the lock is updated. From the next PR on, hygiene checks sign-offs and squash merges keep the trailers.

**Chat request.**

1. A maintainer asks "what should I review today, and rerun CI on #40".
2. The agent calls `search_prs` with the maintainer's filter and answers with a short list; it calls `propose_action(ci.rerun_failed, acme/widget#40)`.
3. The proposal shows inline; the maintainer approves; the actuator reruns the jobs; the thread shows the result.

**Self-improvement.**

1. A maintainer says in chat that hygiene should also check that new config files are documented.
2. The chat agent sees that this needs a new kind of check rather than a setting and proposes `improve`; `self_improve` adds a built-in check type with tests and opens a PR on the code repo.
3. A maintainer reviews and merges it; the supervisor deploys the new SHA and confirms health; profiles can now enable the check.

## 7. Storage

### 7.1 SQLite (operational state)

Every table that refers to a PR, job, or action carries a `repo` column.

| Table | Holds |
|---|---|
| `repos` | configured repositories, poll cursors (ETags, last reconcile), pause state |
| `profiles` | approved lock per repository, pending candidates and their approvals, layer hashes, doctor results |
| `pr_snapshots` | latest snapshot per PR and head SHA (JSON), fetch time |
| `evaluations` | per PR: rubric version, dimension scores, boosts, tier, overrides, rationale, evaluator |
| `jobs` | playbook, key (unique per playbook), status, attempts, payload, result, timestamps |
| `agent_runs` | job, runtime, model, task kind, status, usage, cost, session ref, transcript path |
| `actions` | kind, target, payload, visibility, idempotency key (unique), decision, status, external ref, approver |
| `proposals` | action, state, superseded by, expires at, discussion |
| `signals` | kind, payload, source, consumed by learner run |
| `ci_observations` | run, job, step, category, signature, confidence, rerun outcome |
| `leases` | lab, resource, holder job, heartbeat, expiry |
| `chat_threads`, `chat_messages` | owner, runtime session ref, messages, linked proposals |
| `users` | login, name, avatar, permission cache per repository with expiry |
| `audit_log` | append-only record of every decision and execution |

### 7.2 Memory repo (behavior and knowledge)

```
auto-maintainer-memory/                private
├── MEMORY.md                          global index, at most 200 lines
├── config/                            service settings (8.1)
│   ├── service.yaml                   agents, model providers, default routes, system values
│   ├── runtimes.yaml                  older agent declarations; service.yaml overrides them
│   ├── schedules.yaml                 maintenance jobs
│   └── channels.yaml                  notification channels; credentials stay on the host
├── packs/<name>/                      deployment-specific packs
├── global/
│   ├── knowledge/review_lessons.md    lessons that hold across repositories
│   └── ci/flake_signatures.yaml       signatures seen in several repositories
└── repos/<owner>/<repo>/
    ├── MEMORY.md                      repository index, at most 200 lines
    ├── profile.yaml                   private profile layer (2.2)
    ├── profile.lock.yaml              approved merged profile and layer hashes (2.2)
    ├── rubric/
    │   ├── rubric.yaml                version, dimensions, weights, thresholds, rules
    │   └── stage_goals.yaml
    ├── knowledge/
    │   ├── labels.yaml                label rules
    │   ├── review_lessons.md          what maintainers corrected in drafts
    │   ├── contributors.yaml          partner organizations, frequent contributors (internal)
    │   └── areas/<area>.md            notes per area of the codebase
    ├── ci/flake_signatures.yaml
    └── journal/YYYY/MM/DD.jsonl       T0 entries
```

### 7.3 Host-local files

`~/.config/auto-maintainer/local.yaml` (port, data directory, proxy, operators, lab hosts, host paths), `gh/` (the GitHub CLI login the service acts as), `secrets.env` (mode 0600: the webhook secret, IM tokens), and `provider-keys.json` (mode 0600, written atomically: the API keys maintainers set on the Settings page; a key of the same name in `secrets.env` is used when the file has none). The data directory holds the SQLite database, run transcripts, mirrors, worktrees, release checkouts, and the one-time sign-in links (`sign-in-links.json`, mode 0600).

**Setup on the web.** Nobody edits these files by hand. When `am serve` starts without `local.yaml`, or cannot reach GitHub with the one it has, it serves only the setup page and prints a link with a one-time code (`am setup` prints it again); holding the code proves access to the host. The page asks for the address browsers use; the GitHub account the service acts as, through the GitHub CLI (a login made on the page with a one-time code on github.com, kept in `gh/` beside `local.yaml`, or, for whoever holds the setup code, the login already on the host, read with `gh auth token` and never stored), installing the GitHub CLI first when the host has none; the memory repository, created private if asked; the service's own repository, prefilled from the checkout; and the default agent, chosen among the agent CLIs found on the host (the first one found is preselected; CLIs that are not installed cannot be picked). Finishing writes the files, starts the service in the same process, and signs the person at the host in as the service's account when that account is theirs. Later, maintainers of the service's repository change the same things from Settings, and the service restarts in place on the new setup; if the new setup cannot start, the old one keeps running.

### 7.4 Importing prior state

A repository that already tracked priorities in another tool can bring them along:

- `am import evaluations <file> --repo <owner>/<repo>` loads PR evaluations from a JSON object keyed by PR number. Each entry has `scores` (dimension name to a number from 0 to 10) and optionally `summary`, `rationale`, `manual_boost`, `tier_override` with `override_reason` and `override_by`, and `evaluated_at`. Dimension names must match the repository's rubric; unknown ones are rejected.
- `am import rubric <file> --repo <owner>/<repo>` turns a rubric (dimensions, weights, thresholds, rule text, stage goals) into a memory PR. A rubric is configuration, so it applies only after a maintainer of the repository approves it.
- Imported records carry their source and import time in `evaluations` and the audit log. Earlier review comments are not imported; `comments.legacy_markers` lets the service recognize them in place.

## 8. Configuration

### 8.1 Service settings, host settings, and repository profiles

Three kinds of configuration, owned by different people:

- **Service settings** decide how the service runs everywhere: each agent's model and reasoning level (`agents.<name>.model`, `effort`, chosen on the Settings page among what its CLI offers), the default agent (`default_agent`: every task kind tries it first, ahead of its default route, except `verify`, which keeps looking for a second agent; unset, the one chosen during setup applies; routes in the settings or a profile still win), which agent CLIs it runs, with which models and, optionally, their own permission mode for every task, model providers and the names of their keys, budgets, the default route per task, system values (`poll_seconds`, `max_public_writes_per_hour`, `max_repo_public_writes_per_hour`), and test machines (`labs`: transport, SSH destination, description, work folder, setup, the only repositories it is offered to, extra forbidden patterns, a time limit, GPU idle thresholds; 5.10). Whether and how a repository uses a machine is in that repository's profile, not here. They live in one document, `config/service.yaml` in the memory repo, which lists only what differs from the defaults. Maintainers of the service's repository change them (2.3), on the Settings page or by asking the agent. A document that does not parse, names an agent or provider that does not exist, gives a provider to a CLI that cannot use it, or contains a credential is refused; one that reaches the memory repo some other way and does not check leaves the previous settings in force, and the Settings page says why. Keys are secrets named by the providers; their values stay on the host (7.3).
- **Host settings** describe the machine: `local.yaml` (address, port, data directory, the service's GitHub account, sign-in, the memory repository, operators, lab hosts, host paths, `code_repo`, the `default_agent` chosen during setup), `secrets.env`, maintenance schedules, and notification channels. The setup page writes them (7.3): on the first run whoever holds the code printed on the host, later maintainers of the service's repository.
- **Repository profiles** decide everything else, per repository: the layers of 2.2, merged over `config/defaults/` from the code repo and changed only through the configuration gate (2.3). A repository's routing overrides the service's default routes for that repository.

Every file has a schema version; the service refuses to load an unknown major version and keeps the previous valid one (4.10).


**Development instances.** `am serve --dev` (or `AM_ENV=dev`) talks to the real forge with two guards. With `sandbox_repo` set in `local.yaml`, the instance serves only that repository: `am repo add` and the dashboard refuse others, the poller and scheduler skip them, and the policy denies their actions, while the sandbox repository's actions follow its profile, so a test repository can be exercised for real. Without `sandbox_repo`, the instance serves every repository but holds every action that touches one at shadow.
### 8.2 Profile schema outline

```yaml
schema: 1
repo: <owner>/<repo>
extends: [<pack>, ...]
scope:        {min_pr, base_branches, include_drafts, ignore_authors, issues}
people:       {codeowners, team, review_identities}
contribution: {commit_convention, title_rule, signoff, template_sections, license_header}
checks:       [{id, type: builtin | command | agent, blocking, ...}]
review:       {trigger: manual | scan | events, scan: {schedule, max_prs, followups}, skill, template, recommendations, verify, cross_agent, max_inline_comments, suggest_labels, max_suggested_labels, writing_guide, quality_guide, quick_quality}
labels:       {roles: {<role>: <label>}, rules, never_remove_human_labels}
ci:           {provider, required_workflows, cancelled_is_failure, rerun, runner_groups, root_cause}
merge:        {enabled, method, arming, approvals, blocking_roles, message, update_branch, freezes}
priority:     {rubric, stage_goals}                    # memory layer only
timing:       {calendar, holidays, workdays, weekend, first_response_days, rereview_days, stale_days}  # dates are explicit; calendar is a label
labs:         {<machine>: {use, setup, timeout_seconds, protected_paths, guard: {forbid}, base_clone, worktree_root, env_rules, assets, templates}}   # only machines listed with use: true are used
languages:    {public, internal, style_guide}
confidentiality: {internal_entities}                   # memory layer only
policy:       {<action_kind>: {level, conditions}}    # memory layer; ceilings in the in-repo file
comments:     {command_prefix, footer, legacy_markers}
routing:      {<task_kind>: {primary, fallback, verifier, permissions, bypass_untrusted, question_timeout_seconds}}  # defaults in config/defaults/routing.yaml
notify:       {<event>: <channel>}
playbooks:    {<name>: {enabled}}
access:       {configure: maintain | admin | write}       # default maintain; never below write
memory:       {knowledge_auto_merge, use_global}
ceilings:     {<action_kind>: <level>}                    # in-repo file only
```

### 8.3 Default and recommended policy

Service defaults set every action kind that touches a repository to `shadow`. The only actions that run by default are internal ones that change nothing on the repository: journal entries and notifications to channels the repository configured. The table lists the levels the dashboard recommends once an action kind's agreement score is high enough; maintainers adopt them through the configuration gate. `initial` is the first step after shadow, and `target` is what we expect once agreement stays high.

| Action kind | Recommended initial | Recommended target | Notes |
|---|---|---|---|
| `label.apply` | `propose` | `auto` | labels from the profile's rules and roles, except `ci_gate`; never removes a label a human added |
| `label.ci_gate` | `propose` | `auto` for team members when ready | daily runner budget |
| `review.post` | `propose` | `propose` | first reviews always get a human read; requested drafts are proposals for the requester at any level |
| `review.update`, `thread.reply` | `propose` | `auto` | own comments only |
| `comment.upsert` (hygiene, CI diagnosis, merge note, conflict) | `propose` | `auto` | one per kind per head SHA |
| `ci.rerun_failed` | `auto` | `auto` | within `ci.rerun.max_per_sha` |
| `pr.request_reviewers` | `propose` | `auto` | from `CODEOWNERS` |
| `pr.update_branch`, `pr.merge` | `propose` | `auto` once armed | merge gate (5.5) |
| `pr.nudge` | `propose` | `auto` | at most once per `timing.rereview_days` per PR |
| `pr.convert_to_draft`, `pr.close` | `propose` | `propose` | never automatic |
| `pr.fix` | `shadow` | `shadow` | only a person's request creates one, as a proposal for them; it runs only when they approve it as themselves |
| `lab.reproduce` | `shadow` | `auto` for team authors, otherwise `propose` | a person's request is always a proposal for them, run once they approve the script; the level decides whether reviews reproduce their PRs on their own |
| `issue.label` | `propose` | `auto` | |
| `issue.comment` | `propose` | `propose` | |
| `lab.run` | `auto` for team, `propose` otherwise | same | lab rules (5.10) |
| `memory.commit` (T0) | `auto` | `auto` | |
| `memory.change` (T1) | `auto` after notice | same | 24-hour window |
| `memory.change` (T2), `code.pr` | `propose` | `propose` | a maintainer merges the PR |
| `notify` | `auto` | `auto` | quiet hours |

### 8.4 Example runtimes and routing

```yaml
# memory repo: config/service.yaml (maintainers of the service's repository)
schema: 1
default_agent: antigravity                 # every task tries it first; unset keeps the one chosen during setup
providers:
  deepseek:
    name: DeepSeek
    protocol: anthropic                    # anthropic | openai | openai-responses
    base_url: https://api.deepseek.com/anthropic
    api_key: DEEPSEEK_API_KEY              # a secret's name; its value is set on the Settings page
    models: [deepseek-chat]
agents:
  claude-code:
    provider: deepseek                     # uses the provider's first model unless model is set
    budget: {daily_usd: 80}
    command_prefix: [sudo, -u, am-agent, --preserve-env=PATH, --]   # confine agents to an unprivileged user
  codex:       {budget: {daily_runs: 200}, options: {approvals_reviewer: auto_review}}  # or user: every escalation to a reviewer
  antigravity:
    options: {sandbox: false}                                     # agy's sandbox server cannot start on this host
    approve_commands:                                             # approved without asking, on any PR: read-only repository scripts
      - python3 .agents/skills/docs-check/check_rst_markup.py
      - python3 .agents/skills/review-pr/lint_review.py lint
  # allow_bypass: true on an agent lets routes that ask for bypass mode use it
  dsh:
    adapter: generic
    enabled: true
    command: ["dsh", "run", "--cwd", "{workdir}", "--prompt-file", "{prompt_file}"]  # placeholder until the dsh CLI is confirmed
    output: {format: text}
routing:
  evaluate: {primary: claude-code, fallback: [codex]}               # instead of the code's default
system:
  poll_seconds: 300
```

```yaml
# code repo: config/defaults/routing.yaml; the service settings' routing, then a profile's, override it
# each default route ends with every built-in CLI, so a host with any one installed runs every task
review:       {primary: claude-code, fallback: [codex, antigravity, opencode, gemini], verifier: codex}
# every route runs in auto mode unless it says otherwise; in a repository's profile:
#   routing: {review: {primary: claude-code, permissions: restricted}}       # deny anything that would ask
#   routing: {self_improve: {primary: claude-code, question_timeout_seconds: 7200}}
followup:     {primary: claude-code, fallback: [codex, antigravity, opencode, gemini]}
evaluate:     {primary: antigravity, fallback: [claude-code, codex, opencode, gemini], effort: low}
calibrate:    {primary: antigravity, fallback: [claude-code, codex, opencode, gemini]}
ci_diagnose:  {primary: codex, fallback: [claude-code, antigravity, opencode, gemini]}
ci_root_cause: {primary: claude-code, fallback: [codex, antigravity, opencode, gemini]}
onboard:      {primary: claude-code, fallback: [codex, antigravity, opencode, gemini]}
chat:         {primary: claude-code, fallback: [codex, antigravity, opencode, gemini], transport: acp}
self_improve: {primary: claude-code, fallback: [codex, antigravity, opencode, gemini]}
```

## 9. Security and safety

- **Untrusted input.** PR titles, bodies, diffs, comments, issue text, and CI logs come from anyone who can open a PR. Prompts wrap them in delimited blocks marked as data. Agents that read them run read-only with no write credentials. Their outputs are schema-validated, and every field that names a target (a label, a reviewer, a file, a test template) is checked against the profile before it becomes an action.
- **Instructions from the default branch.** Agent instruction files, skills, and command-check scripts from a target repository are always taken from its default branch (5.7), so a PR cannot change how it is reviewed or checked. Command checks run in a sandbox with no credentials, no network unless the check declares it, and a timeout.
- **Credentials.** The service's GitHub CLI login lives in its own `GH_CONFIG_DIR`, other secrets in `secrets.env`, never in git remote URLs, command lines, prompts, or transcripts. Git operations use a credential helper that hands out the GitHub CLI's token.
- **Configuration integrity.** Branch protection on the memory repo lets only the service push. The memory gate checks the approvers' permissions on every affected repository, the service acts only on approved locks, and host overrides accept only paths.
- **Agent processes.** Allowlisted environment, a down-scoped read-only token, an empty `gh` config and isolated git config, a per-run worktree from the service's mirror, each CLI's auto mode with reviewers answering what it would ask a person, bypass mode only on a three-way opt-in (5.7), an optional confining `command_prefix`, and a timeout. Agents can read files the host user can read, so public text that contains a credential (tokens, API keys, private keys, URL credentials) is rejected by the leak guard. Transcripts are scrubbed of anything that looks like a token before storage.
- **Lab safety.** Template commands, or a reproduction script that a person approved or that the repository's `lab.reproduce` rule runs on its own (for its team's PRs), the command guard and the reproduction patterns, resource leases, approval for external code, no credentials on the machine (code arrives as archives), a run folder per run, idempotent cleanup (5.10).
- **Public writes.** Leak guard, mention allowlist, per-SHA comment limits, write caps per deployment and per repository with automatic pause, kill switch.
- **Dashboard.** Write access on a repository is required to see anything about it; CSRF tokens on mutating requests (the `am_csrf` cookie, echoed in a header, lives as long as the session, and a signed-in browser that lost it gets a new one with its next read, since phone browsers drop session-only cookies); webhook HMAC; the session secret is generated on first start with mode 0600.
- **Confidentiality.** Memory repos are private. Repository-private content stays in that repository's memory directory, never reaches global memory, and is visible only to that repository's maintainers. Digests and IM messages carry internal content only to channels configured for that repository.

## 10. Testing and evaluation

- **Unit tests** use `FakeForge` (in-memory REST and GraphQL with recorded fixtures), `FakeCiProvider`, and `ScriptedAgentRuntime` (plays back event streams). No network, no real CLIs.
- **Fixtures** are recorded from real PRs in at least two repositories with different conventions, such as one with a CI gate label, required sign-off, and squash merges, and one with none of these. Tokens and emails are scrubbed. `classify_review_state`, label rules, and the merge gate are tested against both.
- **Profiles** have tests for layer merging, `extends` resolution, additive keys, ceilings, and schema migration. Every built-in pack has a conformance test against a fixture repository.
- **Agnosticism.** `tests/test_layers.py` (4.9), plus a playbook suite that runs each playbook against two different profiles and checks that only the expected profile-driven differences appear.
- **Access** has table-driven tests for the configuration gate: every source of change (dashboard, chat, slash command, in-repo merge, memory PR, release with changed defaults or packs, shared knowledge) against users with each permission level on the affected repository and on another one.
- **Policy** has table-driven tests: every action kind at every autonomy level, with conditions, ceilings, limits, pause, and freezes.
- **Actuator** tests show that executing the same action twice produces one write, that a stale action is superseded, and that an unmapped role is a no-op.
- **Adapter contract tests** run each installed CLI on a tiny schema task (`pytest -m live_agent`), outside CI.
- **Replay evals** (`evals/<repo>/<playbook>/`) re-run `review`, `followup`, `evaluate`, `ci_diagnose`, and `onboard` on recorded cases. Review cases are scored on findings later confirmed (fixed by the author) against findings dismissed; evaluation on rank correlation with maintainer overrides and merge order; CI diagnosis on accuracy against rerun outcomes; onboarding on how much of a maintainer-corrected profile it got right. A PR that changes prompts, skills, packs, or rubric defaults reports before and after numbers.
- **Shadow agreement** in production (5.8) is the final check before raising autonomy.

## 11. Milestones

**Status of the first implementation.** Built and tested on fakes: everything in M0 and M1 except the replay evals, plus from M2 `ci_doctor`, `merge`, `hygiene`, CI gate management, reviewer requests from CODEOWNERS, `stale` nudges, `profile_drift`, and budgets; from M3 memory tiers with the memory gate, memory PRs on a remote memory repo, slash commands, chat with proposals (structured output instead of MCP tools), and the T0 journal. Checked live and read-only: GitHub sign-in, snapshots of a real repository's open PRs, and the five installed agent CLIs. Not built yet: the MCP server, the ACP transport, the learner beyond the journal and chat lessons, compaction, cross-repository knowledge, digest, overlap detection, runner health, flaky test report, reproduction agent, cross-agent review, issue triage, release assistant, `am import rubric`, and replay evals. Built since: agents' questions on the dashboard over each CLI's own protocol (the ACP transport included), self-evolution with the supervisor (5.12), the dashboard layout of 5.13 with the agent on every page, the service settings with model providers (8.1), changed through the agent and the gate, and setup on the web (7.3) with sign-in links from the host; first-run setup with the GitHub CLI's account was run end to end on a real host. Providers are tested on fakes and by their command lines and environments; no CLI has yet run against a custom provider live. Also built, and tested on fakes and in the demo only: reviews of a selection with a chosen agent, one review per commit and agent, interactive requested reviews that follow the repository's skills, posting a draft or opening a fix PR as the approving maintainer, the `fix` playbook, review schedules set from Home, and the getting-started checklist. Also built and tested the same way: the live view of agents' jobs with stopping, the conversation about a review that revises its draft, drafts rendered as GitHub renders them with comments on the diff, test machines in the service settings, and reproductions on them. Not built: cleanup of chat checkouts, editing a review that a maintainer posted as themselves (`review.update` still writes as the service), and steering a running agent with a message (it can be answered and stopped, not redirected).

| Milestone | Scope | Done when |
|---|---|---|
| M0 Foundation | Repo skeleton, domain, `RepoProfile` with layering, packs, locks, and the configuration gate, store, GitHub forge adapter and poller, engine, policy with shadow and propose, actuator for labels and comments, Claude Code, Codex, and Antigravity exec adapters, dashboard shell with login, pause switch. | The service runs in shadow on its first repository for a week and the dashboard shows what it would have done. |
| M1 First repository in production | `triage`, `review`, `followup`, `evaluate`, `calibrate`, `lab_test`, `onboard`; `am import` (7.4); inbox with edit-before-approve; Queue, PR, Repositories, and Settings pages; read-only chat. | The first repository's maintainers post their reviews through the inbox. |
| M2 CI, merge, and a second repository | `ci_doctor`, `merge`, `hygiene`, CI gate management, reviewer routing, SLA and stale, conflicts, runner health, budgets, `profile_drift`. Onboard a second repository with different conventions. | Flaky reruns are automatic, a week of merges went through the gate, and the second repository runs with no code change. |
| M3 Memory and conversation | Memory tiers, learner, compaction, cross-repository knowledge, chat with propose tools, slash commands, digest, reproduction agent, cross-agent review, ACP transport. | Calibration changes reach the memory repo as PRs and maintainers act from chat. |
| M4 Self-evolution and reach | Supervisor deploys and rollback, `self_improve`, replay evals as a merge requirement for prompt and pack changes, issue triage, release assistant, OpenCode, pi, dsh, and Gemini adapters. | A self-authored PR is merged and deployed, and a bad deploy rolls back on its own in a drill. |

## 12. Open questions

1. **Identity.** Decided: one GitHub CLI login per deployment, ideally a machine user made for the service; GitHub App and token setups were removed. Its account's permissions bound what the service can do, and its token cannot be down-scoped for agents.
2. **Memory repos.** One memory repo per deployment, or one per team so that GitHub access to private settings matches maintainership? Should the code repo be public?
3. **Second repository.** Which repository validates M2? It should differ from the first: no CI gate label, no sign-off, a different merge method.
4. **dsh and pi.** Which CLIs are these, and where are they installed? The generic adapter covers them until confirmed.
5. **Host.** The design assumes one host that runs the service and reaches labs over SSH. Is that host reachable from GitHub for webhooks, or is polling the only source?
6. **In-repo file name.** `.github/auto-maintainer.yml`, or a more neutral name such as `.github/maintainer.yml`?
7. **Notifications.** Which IM integration comes first: Feishu/Lark, Slack, or both?

## Appendix A. Worked example: a repository with demanding conventions

`acme/robotics` is a fictional repository that uses most extension points: required sign-off, Conventional Commits with a strict title rule, CI that starts on a label and runs on scarce GPU runners, a custom review template, bilingual docs, a shared GPU lab, and a private rubric.

### A.1 Conventions and where they go

| Convention of `acme/robotics` | Where it goes |
|---|---|
| Every commit needs `Signed-off-by:`, and squash merges keep the trailers. | `dco` pack |
| PR titles follow Conventional Commits with a description of at most 50 characters, checked by the repository's own script. | `conventional-commits` pack and a `command` check, `blocking` because the title becomes the squash subject |
| Full CI runs only with the `run-ci` label, on three scarce GPU runner groups. | `labels.roles.ci_gate`, `ci.runner_groups` with a daily budget |
| The CI workflow cancels superseded runs. | `ci.cancelled_is_failure: false` (the default) |
| A path-filter job fails when a changed file matches no filter. | `gha-paths-filter` pack |
| Reviews answer a fixed list of questions and end with one of three verdicts. | `review.template`, `review.recommendations` |
| A review that requests changes adds `action-required`. | `labels.roles.changes_requested` |
| Public text is English, internal reports are Chinese, and the repository has a style guide. | `languages` |
| English and Chinese docs change together. | `docs-bilingual` pack |
| Python files carry a license header. | `python` pack, `contribution.license_header` |
| Reviewers answer within 2 business days on the Chinese calendar. | `timing` |
| Tests run on a shared GPU host where one command can stop other users' jobs. | lab guard rules (A.3) |
| Partners and priorities are confidential. | `priority` and `confidentiality` in the memory layer |
| Only admins may configure the service. | `access.configure: admin` |
| Reviews posted earlier by a maintainer's script carry an HTML marker. | `comments.legacy_markers` |

### A.2 Profile

The in-repo file holds the public conventions:

```yaml
# acme/robotics: .github/auto-maintainer.yml
schema: 1
extends: [conventional-commits, dco, codeowners, github-actions, gha-paths-filter, python, docs-bilingual, gpu-lab]
scope: {min_pr: 1200, base_branches: [main, "release/v*"]}
contribution:
  template_sections: [Description, Checklist]
  license_header: {pattern: "# Copyright \\d{4} The Acme Robotics Authors.", paths: ["**/*.py"]}
checks:
  - {id: pr-title, type: command, run: "python3 tools/lint_pr.py --title {title} --body-file {body_file}", blocking: true}
review:
  skill: repo:.agents/skills/review/SKILL.md
  template: [Reproduction settings, Reproduction results, Documentation, Bugs and concerns,
             Design fit, Reuse, Tests and docs, Recommendation]
  recommendations: {ready: Ready for senior review, changes: Changes requested, blocked: Blocked}
labels:
  roles: {needs_review: "Needs Review", changes_requested: action-required, ci_gate: run-ci,
          reproduced: reproduced, needs_docs: needs-docs, community: community}
ci:
  required_workflows: ["CI Test"]
  runner_groups: {gpu-a: {scarce: true}, gpu-b: {scarce: true}, npu: {scarce: true}}
merge: {method: squash, arming: {who: write}}
timing: {calendar: CN, holidays: [2026-10-01, 2026-10-02], first_response_days: 2, rereview_days: 3}
languages: {public: en, internal: zh, style_guide: docs/STYLE_GUIDE.md}
access: {configure: admin}
ceilings: {review.post: propose}
```

The memory layer holds the private settings:

```yaml
# memory repo: repos/acme/robotics/profile.yaml
schema: 1
repo: acme/robotics
people: {review_identities: ["<app-slug>[bot]", lead-maintainer]}
priority: {rubric: rubric/rubric.yaml, stage_goals: rubric/stage_goals.yaml}
confidentiality: {internal_entities: knowledge/contributors.yaml}
comments: {legacy_markers: ["<!--\\s*review-bot:\\s*sha=([0-9a-fA-F]{7,40})\\s*-->"]}
memory: {knowledge_auto_merge: true, use_global: true}
policy:
  label.apply: {level: auto}
  ci.rerun_failed: {level: auto}
  review.post: {level: propose}
  label.ci_gate: {level: auto, conditions: {author: team, recommendation: ready}}
labs: {gpu-host: {use: true}}
```

The repository replaces the default rubric with its own dimensions:

```yaml
# memory repo: repos/acme/robotics/rubric/rubric.yaml
version: 3
dimensions:
  appeal:   {weight: 0.30, rule: "How strongly the change attracts the project's target users."}
  urgency:  {weight: 0.25, rule: "Severity and reach of the bug it fixes; highest for silent numerical errors."}
  partner:  {weight: 0.25, rule: "Alignment with hardware and robotics partners."}
  leverage: {weight: 0.20, rule: "Long-term architectural value and closeness to a clean merge."}
tiers: {P0: 78, P1: 65, P2: 52}
boost_cap: 5
```

### A.3 Lab

Operators declare the host; the profile binds it and adds the repository's parts.

```yaml
# local.yaml (operators)
labs:
  gpu-host: {transport: ssh, host: gpu-host, lock_dir: /srv/lab/locks,
             resources: {gpu: {max_mem_mib: 500, max_util_pct: 0}}}
```

```yaml
# profile: labs section
labs:
  gpu-host:
    base_clone: /srv/lab/robotics
    worktree_root: /srv/lab/robotics-worktrees      # one pr-<N> worktree per PR
    env_roots: [/opt/venv, /srv/lab/venvs]
    throwaway_env_root: /srv/lab/test-venvs         # removed after each run
    protected_paths: [/srv/lab/cache/uv]
    guard: {forbid: ["ray stop"], ray: {private_instance: true}}
    templates:
      unit:     "pytest tests/unit_tests/{file}"
      unit_cpu: "CUDA_VISIBLE_DEVICES= pytest tests/unit_tests/{file}"
      e2e:      "bash tests/e2e/run.sh {config}"
```

With `ray: {private_instance: true}`, a test that needs Ray gets its own instance with a private temp directory and a free port, and only that instance is stopped afterwards.
