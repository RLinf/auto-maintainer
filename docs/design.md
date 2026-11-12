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
| People | who maintains what and who counts as the team; the roles people hold and how they change (2.6) | `CODEOWNERS` path, team members (an org team or a list), review identities, the governance record |
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
  skill: builtin:pr-review     # or service:pr-review, or repo:.claude/skills/review/SKILL.md
  instructions: |              # the maintainers' own words; every review follows them
    Changes to the scheduler need a benchmark in the description.
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
| Service settings (8.1), host setup (7.3), and the service's own code (5.12) | The deployment's one owner (`owner` in `local.yaml`), while they are an admin of the service's repository (`code_repo`), checked live on its forge (`ConfigGate.check_service`). A change arrives from the Settings page, from a conversation in which the agent drafts it, or as a memory PR; it applies when the owner makes or approves it. A host set up before owners has none: every admin of `code_repo` acts as owner until one takes ownership on the Settings page. Operators of the host have no say here unless they are the owner. |

- **Maintainers** of a repository are the members of its People record (2.6) who hold a role that `maintains`, as long as they can still see the repository on GitHub. When the record names none (no record yet, or no role marked), they are the users whose permission on it is at least `access.configure`: `admin` by default, so a repository's GitHub admins maintain it until its record says who does. A profile can lower that bar to `maintain` or `write`, never below `write`. Changing the record or the bar is itself a configuration change, so only the current maintainers can make it. The configuration gate (`profiles/gate.py`, `ConfigGate.check`) decides both, live, and every way of changing configuration, assigning reviewers, and deciding someone else's proposal goes through it (`web/api.py:is_maintainer`).
- **Reviewers**, collaborators with write access who are not maintainers and members of the People record with at least read access, use the dashboard for that repository: they ask for reviews and other work, claim PRs, talk with the agents, propose configuration changes, and arm merges where `merge.arming` allows it. They approve, edit, or reject only the proposals they asked for (a draft for them, or an action from their own request); the repository's maintainers decide every proposal (`web/api.py:may_decide`). The agent's questions in a job go to whoever asked for the job, and its maintainers answer any of them.
- **Learned knowledge** (T1, 5.11) is not configuration. It never edits a profile layer, the rubric's dimensions and weights, or the policy, and it merges without a review only when the repository's configuration allows it and the change comes from that repository's own signals.
- **The owner** of a deployment is one person, an admin of the service's repository: the fork or copy of auto-maintainer that the deployment runs from and proposes its own changes to (`code_repo`). Anyone may deploy auto-maintainer for themselves this way; they need no permission on the repository they forked. The owner alone changes the service settings (8.1): which agents run and on which model providers, their budgets, the default route per task, and system values such as the polling interval and the public write caps. They set API keys, which stay on the host, change the host setup, approve the service's changes to its own code, and merge and deploy them (5.12). They hand ownership to another person who signed in to the dashboard and is an admin of the repository, or move the service to another fork, from Settings → System → Owner (`web/ownership.py`). Whoever holds the setup link that `am setup` prints on the host lets an admin of the repository sign in and take ownership when the owner cannot, as when they left; the link alone changes nothing.
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
3. An agent drafts a profile: packs to extend, the role-label mapping, checks, CI and merge settings, the review skill (it is told the repository's skills) and any review instructions its docs give, a starter rubric, and the questions it could not answer. When none of the repository's skills reviews, the draft asks the maintainers how its PRs should be reviewed (`onboard.NO_REVIEW_SKILL`), and the repository's page asks the same on every tab until someone picks a skill or writes review instructions (`needs_choice`). The deterministic **profile doctor** then checks the draft against the live repository: labels exist, workflows exist, skill paths resolve, command checks run.
4. The draft and its first lock go out as a memory PR. Maintainers of the repository edit and approve it (2.3); until then the service only reads the repository.
5. The repository starts in shadow mode. The dashboard shows agreement scores per action kind, and maintainers raise autonomy when the scores justify it.
6. From then on, the `profile_drift` playbook watches the files from step 2. When `CONTRIBUTING.md`, a workflow, or the label set changes, it proposes the matching profile change.

A repository is supported when it can go through these steps without a code change. Milestone M2 tests this with a second repository whose conventions differ from the first one's (section 11).

### 2.6 People and their roles

Every repository runs its people differently: some have reviewers per area, maintainers, and an owner; some have committers voted in by a steering group; some write it down in `GOVERNANCE.md`, some on a website, some nowhere. The service keeps one record per repository, `repos/<owner>/<repo>/governance.yaml` in the memory repo (`domain/governance.py`, `profiles/governance.py`), with a fixed shape that each repository fills in its own way:

- **Roles**, each with a name, a level (higher means more responsibility), a description, responsibilities, privileges, the GitHub permission a holder is expected to have, whether it is held for areas (paths or components), and whether its holders **maintain** the repository in the service (`Role.maintains`; in the skeleton, maintainer and owner).
- **Transitions**: how people move from one role to another, join, or leave. Each has criteria in the repository's own words, with a measure an agent can check from GitHub activity where there is one (merged PRs, reviews, months in the role, months inactive, nominations, over a window), the steps of the process, the roles whose holders decide, and how many must agree.
- **Members**: who holds each role, for which areas, and since when; their score when the process keeps one (`score`, named by the record's `score_label`), what they need for their next role (`next_step`), and a short note.
- **History**: every change of someone's role, with the date, who decided, the reason, and the evidence. It only grows.
- **Sources** the record was taken from, a short summary, whether it is **established** (filled in from the repository's real process) or still the skeleton, and **open questions** for the maintainers.

A repository without a record shows the skeleton in `config/defaults/governance.yaml`: reviewer, maintainer, owner, and emeritus, with common transitions and the questions to answer.

Reviewers configure the record by talking with the agent that keeps it, on the repository's **People** tab (`web/people_talk.py`). The conversation is shared by everyone who may see the repository, and its thread (`ChatThread.topic`) stays out of personal chats. A reviewer tells the agent where the process is written (a file in the repository, a web page), states rules, names members, answers the agent's open questions (each has an Answer button that quotes it into the message), or asks it to check who should change role. Each turn runs the agent in a checkout of the default branch kept for the conversation, with the record it builds on and the conversation so far; reviewers' messages are the task, not untrusted data, while the repository's documents are what the agent reads. A message sent while a turn runs goes to the agent at work (`AgentRunner.tell`), and questions the agent asks go to the person who wrote the message, on the same tab. The agent answers in the conversation and, when the conversation changes the record, returns the whole revised record. The same work also runs as a job (`playbooks/governance.py`, `dashboard.command` with `command: governance`), which chat starts: in `edit` mode the agent reads the process and rewrites the record; in `members` mode it checks people's GitHub activity (through the read proxy) against the transitions' criteria and proposes who should change role with the evidence, and the playbook keeps the roles and transitions as they were.

Every revision builds on the newest unapproved draft when one waits, else on the record (`playbooks.governance.base_record`). It keeps the history as it was and adds an entry for every change of role it makes that the agent did not record (`domain.governance.with_history`), so no change of role goes unrecorded. It is a T2 memory change whose `MemoryChangePayload.replaces` names the draft it revises: when the actuator stages it, it closes that draft as replaced, with its staged branch and its memory PR, unless a maintainer has already approved it. Nothing applies until a maintainer of the repository approves (2.3), and the agent never decides a change of role; it only proposes one for the people the process names. While a draft waits, the People tab shows it as it would read once applied, with a switch to the current record. The tab opens on the role ladder (each role with how many hold it; a step filters the list) and four views: **People**, a searchable table with role, areas, score, next step, start date, and last change, filtered by role and area and sorted by any of them, where a row opens the person's areas, note, GitHub permission, and history with its evidence, and asks the agent about them; **Roles & process**, each role with its duties and how people get in and out; **History**, a searchable table of every change of role; and **Talk with the agent**.

The record decides two things in the service (2.3): the holders of a role that maintains are the repository's maintainers, and every member with at least read access on GitHub may use its dashboard. Everything else a role lists (privileges, expected GitHub permission) describes the process; GitHub still decides what a person may do on GitHub. A draft that changes who maintains is approved by the maintainers the current record names, so a repository's admins hand over to the record when they approve its first version. The People tab checks the record against GitHub when it opens: a member whose permission is below what the role expects, a member without access, and a code owner the record does not list.

## 3. Features

Every feature works on any repository; the profile decides its details. Each row names its default autonomy after the shadow period (5.8) and its milestone (section 11).

### 3.1 Requested features

| Feature | What it does | Default | Milestone |
|---|---|---|---|
| PR scan and triage | Lists open PRs in scope, classifies review state (`NEW_REVIEW`, `FOLLOWUP_REVIEW`, `APPROVED_NEW_COMMITS`, `UP_TO_DATE`, `SELF_PR`), writes a one-sentence summary, suggests labels from the profile's rules. | labels: `propose`, then `auto` | M1 |
| First review | Runs the profile's review skill in a worktree at the PR head, verifies each finding with a second pass, and drafts a review in the profile's template: a main comment plus inline comments with suggested fixes. A finding goes inline only on lines a hunk of the diff shows (`domain/diffs.py`), since the forge refuses a whole review over one comment elsewhere; any other finding is listed in the main comment with its `path:line`. When the review posts, its comments are checked again against the forge's own diff of the PR, and one that does not fit goes in the main comment under its place. By default it runs only when a reviewer selects PRs (dashboard queue, `/am review`, chat); the draft goes to that reviewer's inbox to edit and post. `review.trigger: scan` reviews a bounded batch on a schedule; `review.trigger: events` reviews as soon as a PR opens. | requested: draft for the reviewer; scan and events: `propose` | M1 |
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
| Reviewer routing, claims, and assignments | Suggests reviewers from `CODEOWNERS` and current load. A PR has three kinds of people. Its **owners** answer for the whole PR's progress; its **reviewers** review it (`PrAssignee.role`); anyone who uses the repository's dashboard claims either role, a maintainer assigns both by GitHub login, whether or not they ever signed in, and the PR's owners assign and take off its reviewers. Assigning a reviewer can also request their review on GitHub (`pr.request_reviewers`); assigning an owner asks GitHub for nothing. Its **code owners** are GitHub's: the reviews it requests by `CODEOWNERS`, whose approval a protected branch waits on; they hold no role here unless someone gives them one. | `propose` | M2 |
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
         │ HTTPS (am tls, or a reverse proxy)      │ ssh, template commands only
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

Agent worktrees come from mirrors the service owns, never from clones that people use. Lab worktrees follow each lab's own paths from its profile. The dashboard listens on every interface (`host: 0.0.0.0`) on port 443 with HTTPS by default; any other `port`, in `local.yaml` or `am serve --port`, serves plain HTTP (`config.serves_https`). With HTTPS, cookies go only over HTTPS; `cookie_secure: true` does the same for plain HTTP behind a TLS reverse proxy (bind `host: 127.0.0.1` behind it). `base_url` is the address browsers use and builds the sign-in links. The service does not run as root, so a host where unprivileged programs may not listen on 443 needs `net.ipv4.ip_unprivileged_port_start` lowered once; `am serve` says how and stops before listening. A host with a certificate of its own, such as one a public authority signed for its name, names it in `tls` (`cert`, `key`). Otherwise the first start makes one (`tls.py`): a private certificate authority in `~/.config/auto-maintainer/tls/` and a server certificate for the host in `base_url`, the host's network address, its name, and the loopback addresses. People install the authority's `ca.crt` once as a trusted root; the setup page and the dashboard offer it at `/tls/ca.crt`, and the log prints its SHA-256 fingerprint to compare. The authority carries name constraints, so a browser that trusts it accepts it only for those addresses, even if its key leaks. Every start renews a server certificate that expires within 30 days, under the same authority. `am tls [ADDRESS...]` makes it again by hand; a new address needs a new authority (`--new-authority`), which everyone installs again. `local.yaml` has `version: 2` since this default; a file without a version keeps the earlier defaults, port 7777 with plain HTTP.

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
| The service crashes or restarts during a job | The job returns to the queue when the service starts again (job store, 5.4) and runs again from fresh reads. Idempotency keys stop repeated writes. An agent run the restart cut off resumes: from the moment its CLI has a session, the job's progress keeps the runtime and session id (`domain/progress.py:agent_session_key`) until the run ends, and the job's next run of that task kind resumes it on the same runtime (Claude Code `--resume`, Codex `thread/resume`, ACP `session/load`, whose replayed history is not reported again), told that the service restarted and that whatever was running was stopped, with the whole task after that note in case the CLI cannot load its history; when the resumed run brings nothing back, a fresh one starts (`playbooks/base.py:PlaybookContext.ask`). A test machine session with a minute or more left survives too (below and 5.10). The start removes agent containers a killed process left running (`ContainerSandbox.remove_leftovers`). |
| The service crashes between a forge write and recording it | The actuator records an intent before each write. On startup, an intent without a result is reconciled by looking for the action's marker or reading the target's state, and it is retried only if the write did not happen. |
| The service stops while allowed actions wait in the actuator's queue | The queue lives in memory, so on startup the actions still `queued` are queued again, after the reconciled intents, in the order they were allowed. |
| The forge is down or rate-limited | Polling backs off, the actuator holds its queue, jobs wait, and the dashboard shows a banner. |
| An agent CLI fails, runs out of quota, or returns invalid output | Invalid output gets one repair turn (a final message with no result at all is asked to finish the work first, as an agent that ended its turn too early has not); after that, the router moves to the fallback runtime and opens the failing runtime's circuit breaker. A job that runs out of runtimes fails and shows on the dashboard. |
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
- `ReviewState`: `NEW_REVIEW`, `FOLLOWUP_REVIEW`, `APPROVED_NEW_COMMITS`, `UP_TO_DATE`, `SELF_PR`, plus `DRAFT` and `CLOSED`. `classify_review_state(snapshot, identities, markers)` is a pure function defined in 5.6; the profile supplies the review identities and any legacy comment markers. Only reviews and comments that carry a review marker count, so a review a person wrote by hand never does, even when the service acts as that person's GitHub account.
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

- **Poller** (always on; the source of truth). Per repository: lists open PRs and issues every 2 minutes with ETags, polls checks for PRs with pending runs, and runs a full reconcile every 30 minutes. It turns differences between the new snapshot and the stored one into events, so a missed webhook costs latency, not correctness; a snapshot that differs only in what makes no event (a commit's time, which review progress reads) replaces the stored one without any. After the service writes to a PR (an executed action: a label, a review, a comment), it refreshes that PR's snapshot at once, so the dashboard shows the write without waiting for the next poll.
- **Webhooks** (optional). `POST /webhooks/<forge>` (`/webhooks/github` for the default GitHub), checked as the forge's kind checks deliveries (HMAC on GitHub, the shared token on GitLab), for lower latency when the host is reachable from GitHub, directly or through a relay. A webhook triggers an immediate targeted poll rather than being trusted as state.
- **Slash commands.** `/am help`, `/am review`, `/am rerun`, `/am merge`, `/am merge cancel`, `/am label <names>`, `/am why`, `/am pause`. The parser checks the commenter's permission on that repository (write or above, except `help`; PR authors may `/am rerun` once per head SHA when the profile allows it). `/am why` explains the service's last public action on that PR and never mentions priority. The command prefix is a profile setting.
- **Dashboard and chat** emit `dashboard.command` and `chat.message` events through the same engine.

Network: forge calls honor `HTTPS_PROXY`.

### 5.4 Engine

- **Job store.** Jobs are rows with `repo`, `playbook`, `key`, `status`, `attempts`, `payload`, `result`. The key is unique per playbook, so a second event for the same PR and SHA joins the existing job. Jobs survive restarts. A stop (a deploy, `am stop`) cancels the running jobs and puts them back in `queued` without counting the attempt it ended (`Engine.stop`), and on startup any job a crashed process left `running` returns to `queued` before the engine starts, that attempt counted (`Engine.recover`), so unfinished reviews and other work run again at once from fresh reads instead of waiting for their lease to expire; questions their agents left pending are cancelled (`cancel_orphans`).
- **Scheduler.** Cron triggers from `schedules.yaml` (digest, reconcile, compaction, flake report, doctors), each producing a `schedule.tick` event.
- **Worker pools.** Separate concurrency limits for forge I/O, agent runs per runtime, and lab leases, plus a fair share per repository so one busy repository cannot starve the others. The quick quality scores of PRs nobody reviewed yet run in a pool of their own (`quality`, two at a time by default), so the background work never holds up a review someone waits for; a pool the host's settings leave out runs one job at a time. A pool claims a queued job only when it can start it, so a job shown as running runs. In the `agent` pool, the slots (`system.agent_slots`, else `pools.agent`) hold the jobs a person asked for (`Job.asked_by`: a dashboard or conversation request), which go first; the service's own work (scans, scoring, CI diagnosis) takes only slots nobody holds, so a person's request never waits behind it, and while that work finishes more jobs than slots can run. A queued job's view (`web/jobs.py:slot_view`) says how many slots are in use and how many jobs go first.
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
| `review` | a review request (dashboard, `/am review`, chat, scan) for a PR in `NEW_REVIEW`; PR events only with `review.trigger: events` | Worktree at head; the profile's review skill, or the repository's own when the profile keeps the built-in one; interactive for a person's request, headless otherwise. Reviewing, testing, and reproducing are one piece of work: when the repository uses a test machine that can take the PR, the review first asks for a session there (`lab.review`, on by default for every PR, `playbooks/review_session.py`) and waits until it opens, and the same agent then runs the PR's tests, follows its documentation, and runs experiments on the machine through `lab` as it sees fit while it reviews, and reports what it ran in the review; without a machine, or when the rule holds the PR back, nothing runs and the review says why. Verifier pass; public-text checks. The follow-up does the same. | `review.post`, `label.apply` (`changes_requested`, and the labels the review suggests), `label.ci_gate`, `pr.request_reviewers` |
| `followup` | the same requests and triggers, for a PR in `FOLLOWUP_REVIEW` or `APPROVED_NEW_COMMITS` | Diffs the last reviewed SHA to the head; status per earlier finding; new findings; regression spot-check for approved PRs. | `review.update`, `thread.reply`, `review.post` |
| `comment_check` | `comments.check` (Check all on a PR's Review progress tab, or Check on one comment; named comments are all read, `max_comments` per agent run), and from `comment_scan` on `review.comments.schedule` (`check: scan`, the default; never on each push) | Whether each open review comment (from anyone but the author) is addressed by the code now: one quick run reads only, per comment, the thread, its file's changes since the comment's commit, and the code around its line at the head, and sums it up in one line as `addressed`, `partly`, `open`, or `unclear`, with a reason and the fixing commit. A periodic check reads again only comments whose file changed since their last check, so an unchanged PR costs nothing. Results go to the store (`domain/review_progress.checks_key`), where review progress counts a comment found addressed as fixed. | none |
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
| `reproduce` | a person's reproduce request for a PR (dashboard, or the conversation about a review), naming a test machine | Agent plans a reproduction that follows the PR's documentation from a checkout of the head, or says what the machine lacks; waits for the requester to approve the plan; then an agent does it in a session on the machine and its result goes into the PR's review, or a comment for a PR with no review (5.10). Reviews do not start one: they test in their own session. | `lab.reproduce`, then the review's revision or `comment.upsert`, drafted for the requester |
| `stale` | daily tick | SLA timers; nudges; closing proposals for abandoned drafts. | `pr.nudge`, `pr.close` |
| `issue_triage` | issue opened | Template labels, missing information, duplicates, doc links. | `issue.label`, `issue.comment` |
| `digest` | daily and weekly tick | Builds the digest and metrics per maintainer. | `notify` |
| `learn` | signal batch, weekly compaction | Turns signals into memory changes (5.11). | `memory.commit`, `memory.change` |
| `self_improve` | `improve` request from chat, once a maintainer of the code repo approves it | Agent edits a worktree of the code repo; the playbook runs the tests and Ruff and has the diff described. | `code.pr` |
| `fix` | a maintainer's fix request for a review (dashboard), or a change in their own words (`request`: **Change with agent** on an open PR without a review's findings, or a conversation's `change` proposal, which starts at once and whose thread is told when the diff is ready) | Agent changes a worktree at the PR head for the review's findings or the request; the playbook takes the diff. The approver chooses where it goes: a PR into the PR's branch, or one commit straight onto that branch (which they must be allowed to push to); either way it is pushed with their own account. When the request asks for it, the draft also gives the PR itself a new title or description (`pr_title`, `pr_body`), shown on the draft and set on that PR after the push either way. | `pr.fix`, drafted for the requester |
| `merge_base` | a maintainer's Merge request on a PR that conflicts with its base branch (dashboard), or the owner's on a conflicting self-improvement PR on `code_repo` | Merges the base branch into a worktree at the PR head as the PR has it (the checkout's instruction files go back to the PR's own for the merge, then to the default branch's for the agent); an agent resolves each conflict, keeping what both sides meant, runs nothing, and may ask the maintainer. A conflict in a file that steers agents (`AGENTS.md`, skills) is left to a person, and a merge git refuses fails the job rather than reading as clean. On a watched repository, the draft (`pr.fix` with `merge_ref`) pushes a merge commit as the approver onto the PR's branch (in the contributor's fork when it lives there) or, when the fork does not let maintainers push (`PrSnapshot.maintainer_can_modify`), opens `am/merge-<n>-<id>` into the PR's branch. On `code_repo`, the draft (`code.update`) shows the resolution diff for the owner to approve and pushes the merge commit with a `Requested-by:` trailer onto the `am/self/*` branch. A PR that merges cleanly does not come here: the press updates the branch on the forge (`pr.update_branch`, or `code.update` on `code_repo`). | `pr.fix` with `merge_ref` (watched repo) or `code.update` (`code_repo`), drafted for the requester |
| `fix_ci` | a person's Fix CI request on a PR whose CI failed at its head (dashboard) | An agent reads the failed jobs' logs, their workflows, and the service's CI diagnoses, and changes a worktree at the PR head to fix what the PR causes (a flake, the runner, or a broken base it reports and leaves). With a test machine the person picked, their press also approves a session there (a `lab.reproduce` drafted for them and approved at once, its discriminator starting `fix-ci:` so it never counts as a reproduction): the agent copies its worktree to the machine's head folder with `lab --sync` (tracked and new files, deleted files removed, environments in the folder kept) and runs the failing steps the way the workflow does with `lab -- ...`, every command checked as in a reproduction, until they pass. The playbook closes the session and a second, read-only run describes the diff. | `pr.fix`, drafted for the requester |

**Review requests.** A review request names the PR and, unless it comes from the scan, the reviewer who asked (`Action.draft_for`). The service routes it by the PR's state: a first review for `NEW_REVIEW` (or the reviewer's own PR), a follow-up for `FOLLOWUP_REVIEW` and `APPROVED_NEW_COMMITS`, nothing for `UP_TO_DATE`. One of the PR's owners or reviewers on the dashboard who has no review of their own from the service (drafted for them, or posted as them) gets a first review of their own instead, whatever others' reviews cover (`web/api.py:may_review_own`, `ReviewRequest.own`); once they have one, the PR's state routes their requests as anyone's. A reviewer's request may target drafts and PRs outside `scope`; scans and events respect `scope` and `scope.backfill_reviews`. Every action a requested job produces is a proposal for the requester, even where the policy level is `shadow` or `auto`; when a maintainer approves it, it runs even under `shadow`. Dev mode, pauses, `off`, and the hard limits still apply. Drafts from scans and events follow the policy as usual.

**Skills, questions, and posting.** A review follows the skill the profile names in `review.skill`. With `builtin:<name>` (the default, `builtin:pr-review`), it follows the repository's own review skill when its default branch has one (in `.agents/skills/`, `.claude/skills/`, `.codex/skills/`, `.gemini/skills/`, or `.opencode/skills/`, with "review" in its name or path), else the service's skill `<name>`; with `service:<name>`, the service's skill even when the repository has its own; with `repo:<path>`, the repository's skill at that `SKILL.md`, whatever its name. The agent is also told about the repository's other skills. On top of the skill, `review.instructions` holds how the repository's maintainers want its PRs reviewed, in their own words (what to check, what matters most, what to leave alone); every review, follow-up, and revision follows them, and they win where they and the skill disagree (`playbooks.prompts.review_skill_block`). Both are set on the repository's Overview, under **How PRs are reviewed** (`GET|PUT /repos/{owner}/{repo}/review-guides`), which lists the repository's skills read from its default branch through the forge (`profiles/skills.py`, kept ten minutes) and the service's review skills, or by asking the agent there, which knows the same choices; a choice that names no skill found is refused. The built-in `pr-review` skill covers correctness, design and reuse, tests, docs, and PR metadata. Review skills are often written for an interactive session. For a review a person asked for, the prompt tells the agent it reviews for that person and may ask them when something blocks a sound judgment; the questions reach that person (5.7). Scans and events run headless: the agent decides on its own. In both cases the agent returns the review result of 5.6 and never posts: whether to post is the requester's decision on the draft, so the agent is told not to ask, and a question about posting or pushing is denied without reaching anyone (5.7).

**One review per commit and agent.** A request for a PR whose head already has an active review job, or a pending draft, from the same agent joins it instead of starting another: the requester sees who asked and which agent runs. A different agent at the same head is a separate review. The requester may pick the agent; that choice replaces the route's primary and fallbacks for the review (or fix) task only, and verification keeps its own route.

**Posting as yourself.** A maintainer can approve a `review.post` draft as themselves: the actuator posts it with the token they signed in with (5.9). The marker stays in the body, so the service still recognizes the review as its own when it classifies review state (`own_actions`), and later follow-ups treat it like any other review it posted.

**Fixes.** From a review with findings, a maintainer can ask an agent to fix them. The `fix` playbook checks out the PR head in the repository's folder, gives the agent the findings, the repository's skills, and an optional note, and lets it ask the maintainer. The agent edits the worktree and never commits, pushes, or posts. The playbook turns the diff into a `pr.fix` action drafted for that maintainer, who reads the diff on the proposal and approves it as themselves: the actuator commits it with them as author and a `Signed-off-by` from them, pushes `am/fix-<n>-<id>` with their token, and opens a PR into the reviewed PR's head branch (for a fork, a cross-repository PR). `pr.fix` cannot be approved any other way; the service's own account never pushes.

**A new title and description for the PR.** When what the person asked (on the PR page, in a conversation, or later in the conversation about the fix) includes the PR's own title or description, the agent says the new ones in its final message instead of writing them anywhere, and the short task that describes the diff returns them in `FixResult.pr_title` and `pr_body`, reading the PR's current title and description; they are left out otherwise. The `pr.fix` draft carries them (`FixPrPayload.pr_title`, `pr_body`) and shows them under its own title. They go on the PR that was changed, not the fix PR, whichever target the approver picks: `_pr_fix` passes them through the leak guard as `pr.edit` does (mentions the PR already had may stay) before it pushes anything, so blocked text pushes nothing, then sets them with the approver's own writer after the push. A forge that refuses that edit leaves the push done and records `edit_error` on the action rather than failing it, so a retry never pushes again. A conversation turn that changes the diff describes it again, told of the title and description the draft gives the PR; its answer keeps, replaces, or drops them.

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
4. Without a match, reuse the first look at the same failure seen before (`ci_doctor.fingerprint`: the workflow, the job, and its first error lines with times, numbers, hashes, and temporary paths taken out, kept on each observation): one at this head of this PR, whatever it found, or a `flaky` or `infra` one anywhere in the repository in the last seven days (`REUSE_DAYS`). Otherwise run the `ci_diagnose` task (log excerpt, PR description), once for all of a run's new failures (most of a call's tokens are the agent's own setup), on the quick budget (low effort, at most `QUICK_TIMEOUT_SECONDS`), told to judge only from what it is given (`ci_doctor.FROM_THE_LOG`): an agent with tools otherwise opens the full log, other runs, and code, at up to a hundred times the cost. It returns, per job, `category` (`flaky`, `infra`, `pr_caused`, `unknown`), `confidence`, evidence lines, an explanation, and an optional new signature. This is the first look, cheap and from the log alone.
5. `flaky` with budget left (`ci.rerun.max_per_sha`, default 2, with backoff): rerun failed jobs. A flake that still fails after every rerun is no longer treated as one.
6. **Root cause.** Every failure that is not a flake (and every flake that kept failing), except the runner's (`infra`: the code cannot explain it) and those whose cause was found at this head before (which keep it), goes, together with the others of the same head, to one `ci_root_cause` task (`ci.root_cause`, on by default): a read-only worktree at the head SHA, the PR diff against the base branch, each job's log excerpt and first look; it fetches a job's log only when the excerpt cuts off the failure, and never other runs or the base branch's history. Several jobs often fail for one reason, so the agent returns one cause per distinct reason (`domain/records.py:RootCause`): the jobs it explains, what fails (a test, a build step, a command), the mechanism, the culprit files and lines, the smallest fix, up to five log lines, a confidence, and its origin: `change` (this PR causes it), `base` (it fails without the change: the base branch is broken), `infra`, `flaky`, or `unclear` (the agent says so rather than guess). The playbook keeps only jobs that failed at this head, and only culprit paths that the PR changes or that exist in the checkout. A `change` cause becomes a comment for the author in the public language (what fails, why, where, the suggested fix; no confidence, no log lines); a `base` cause tells the author it is not theirs and notifies the maintainers (`ci.base`), as do `infra` (`ci.infra`) and `flaky` or `unclear` causes (`ci.unknown`). A job with no cause (the switch is off, or the agent gave no usable answer) keeps the first look: `pr_caused` explains it to the author in one line, `infra` and `unknown` notify.
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

A finding goes inline when a line of the diff carries it, with the suggested fix as a suggestion block when it is a code change. Other findings go in the main comment under their section. There is one review per head SHA per posting account: the service's own account posts at most one, and each person posting a draft as themselves at most one of their own (`policy/engine.py:ONE_PER_SHA`; a draft counts against the person it is for until they approve it as the service). A new finding on a line that already has a thread is posted as a reply in that thread.

**What a review reads.** Reviews, follow-ups, and root causes leave lock files, minified files, source maps, and images out of the diffs they read (`playbooks/prompts.py:without_generated`), naming them; the PR's file list keeps them. A follow-up reads the changes since the last reviewed commit and the PR's file list; the whole diff against the base is in its checkout (`git diff origin/<base>...HEAD`), which it reads when it needs more.

**Follow-up format.** A follow-up edits the service's main comment, or posts a follow-up review when the earlier review used inline threads. Each earlier finding gets a status (`✅ Resolved in <sha7>`, `⚠️ Still open`, or `◐ Partly addressed` with what remains), and new findings go under "New findings since <sha7>". Resolved threads get a reply naming the fixing commit. When everything is resolved and nothing new appeared, the comment says so in one line.

**Writing.** Every review, follow-up, and revision of one (a reviewer's turn in Discuss, a reproduction's result) is written by the repository's writing guide, `review.writing_guide`, or the service's default (`playbooks/prompts.py:DEFAULT_WRITING_GUIDE`) when it sets none. The default writes for a maintainer who has not read the PR and does not know that part of the code. It leads with what the PR does and whether it is ready, then says what must change and why, says each thing once, and keeps code out of the main comment: no code blocks, few identifiers, and few file or line references. A finding on a diff line is posted on that line, where it may name the code. Where a repository's review skill asks for more code or references, its sections stay and the guide wins.

**Quality.** The reviewing agent scores each PR's quality from 0 to 100 in its review (`quality`), by the repository's quality guide, `review.quality_guide`, or the service's default (`DEFAULT_QUALITY_GUIDE`). Before it scores, the agent fills `checks`: the basics of `playbooks/prompts.py:QUALITY_CHECKS` (the repository's requirements for every PR, the tests and docs the change needs, its code style, and the design), each met, not met, or not applicable; the dashboard shows them as a row of ticks and crosses. The default guide asks for a maintainer's judgment against the other PRs the repository gets. It checks the basics first, and each one missed is a fault even when it is easy to mend. It weighs design most: whether the change fits the repository's architecture, design, and style, solves the problem in the right place, and stays simple. Easy faults cost less than hard ones but add up, and several hard faults make a PR unacceptable whatever its strengths. It keeps 90 and up for PRs with nothing left but trivia, and uses the whole range. The guide describes five bands, and the dashboard colors the score by them: excellent (90 and up), good (75), fair (60), poor (40), and bad (below 40). Out-of-range or unreadable scores from an agent are clamped or dropped rather than failing the review, and a revision that does not score the PR again keeps the draft's score. A PR's quality is that of the newest review in the chain follow-ups build on (the posted reviews, then a newer draft or shadow run; a draft a maintainer rejected does not count) that has one. A later scoring of that review by the `quality` playbook (Score again, or the scan for reviews without a score) replaces it. Before a review, every open PR gets a **quick score** at a glance, so reviewers can choose what to take first: an agent reads the diff and as much of the code around it as it needs, by the same quality guide, without running anything or writing a review. It must cost far less than a review, so it reads a condensed diff (each file's diff up to 6,000 characters, sources before tests, without lock, binary, and data files, 24,000 in all), opens at most three other files, runs nothing, and runs at low effort for at most five minutes whatever its route and agent say (`AgentTask.budget: quick`). It holds the PR to the repository's own review standard through a checklist: once per repository, and again whenever they change, an agent draws from its review skill and contributor guide the dozen checks a reader can settle quickly (how docs are organized, naming and code style, the design rules and taste the repository asks for, the tests and docs a change must bring, what every PR must meet), and every quick score reads that checklist instead of the skill. It is also given what the service knows (the PR's size, whether it touches tests and docs, its CI, and the hygiene checks at its head), and, for calibration, PRs of the repository that a review scored, from low to high, and how the quick scores so far are spread (`quick_quality`, in the background, highest priority first; `review.quick_quality: false` turns it off). The review's score is the accurate one: once a review scores the PR, it shows instead, and the PR is not scored quickly again; the PR page keeps the quick score beside it for comparison. When a PR's head moves past its score (new commits after its quick score, or after the review that scored it), it gets a quick score of the new head once it has been quiet for an hour, unless the new commits leave the same files changed by about as many lines (within 10 lines or 5%: `domain/review.py:barely_moved`), when the score is carried to the new head with the commit an agent looked at (`scored_sha`), so a burst of pushes gets one score, and at the latest six hours after the scan first saw it move. Until a follow-up review scores it again, the dashboard shows that quick score, and the PR page shows the review's score beside it with the commit it covers; Score quality on such a PR scores the new commits quickly instead of the old review. A quick score without a reason in every dashboard language or without checks is scored again too.

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

Agents read GitHub as the service without holding its token: the service serves `forges/github/proxy.py` on a Unix socket each container mounts, and `gh` there sends every request through it (`http_unix_socket`). The proxy adds the service's token to requests for the GitHub API and passes only reads: GET, HEAD, and OPTIONS, and GraphQL queries (`gh pr view` reads through GraphQL); a mutation and every other method are refused with a message saying writes go through the service. Writes stay where they were: agents never post, push, or merge; the actuator does, after approval. Prompts still ask agents not to run a PR's tests or workflow (`prompts.NO_CODE_RULE`), since reproductions run on the repository's test machines; that is guidance, not a defense.

The Settings page shows the sandbox's state and lets the owner turn it on or off and set its network and limits; the change is kept in this host's `local.yaml` and applies to the next agent run (turning it off gives agents back to the host at once). A host without docker, or with `sandbox.enabled: false`, runs agents on the host as before: the CLI's own auto mode or `agents/approver.py`, with what it would ask going to the question desk. Some agents (Antigravity) end their turn when a request is refused, waiting for a person; the conversation then tells the agent what was refused and to finish without it, up to three times (`acp.CONTINUE`). Antigravity also asks a person with its own `ask_question` tool, which ends its turn: the conversation sends the question to the question desk as an input question (never answered by a rule or by bypass mode), and the answer starts the next turn of the same session, up to three questions a run (`acp.MAX_ASKS`). A CLI that silently cuts a long prompt declares `max_prompt_bytes` (Antigravity: 150 KB, as it cuts near 200 KB); a longer prompt goes as its start, up to a line break, with a pointer to the whole of it in the run folder (`ExecRuntime.fitted_prompt`). Prompts therefore put what the agent acts on first: in a conversation, the reviewers' messages come right after the instructions, before the PR, its diff (40 KB in a conversation turn; the checkout has the rest), and the draft. The event stream normalizes every runtime into `message`, `tool_call`, `tool_result`, `usage`, `permission_request`, and `final`; the final event carries an `AgentResult` with the validated structured output, the session reference, usage, and the transcript path.

Every built-in runtime talks back to the service while it runs, so that what an agent would ask a person reaches a reviewer (see **Agents' questions** below). Three transports, all over the CLI's standard input and output:

- **Claude Code's stream-json control protocol**: print mode with `--input-format stream-json`; the service sends an `initialize` control request and the prompt as a user message, and answers `can_use_tool` control requests.
- **Codex's app server** (`codex app-server`, JSON-RPC): `initialize`, `thread/start` or `thread/resume`, `turn/start`; the server sends approval and user-input requests.
- **ACP** (Agent Client Protocol, JSON-RPC): `initialize`, `session/new`, setup calls such as `session/set_mode`, `session/prompt`; the agent sends `session/request_permission`.

pi (`agents/adapters/pi.py`) is one-shot: `pi -p --mode json --no-session --offline` reads the prompt on standard input and streams its events as JSON lines (tool steps, usage, the answer). It cannot ask, and its `bash` tool runs anything, so outside bypass mode it gets only `read,grep,find,ls` (and `edit,write` for a task that may write); in a container it runs in bypass mode with every tool. A model provider goes into a `models.json` of the run's own (`PI_CODING_AGENT_DIR`) whose `apiKey` is the bare name of the key's environment variable (pi reads a value that names a set variable from it and sends any other value as it is), with what is known of the model (`contextWindow`, `maxTokens`, `reasoning`, `input`, and a `thinkingLevelMap` that hides the levels it does not take); otherwise pi uses its `/login` (`~/.pi`) or a provider key from its environment. The generic adapter is one-shot too: standard input carries the prompt and closes.

Adapters, checked against `--help` of the CLIs on the development host and against live runs where the account allowed (versions in parentheses):

| Runtime | Binary and transport | Auto mode | Structured output | Restricted mode | Bypass mode | Sessions |
|---|---|---|---|---|---|---|
| Claude Code (2.1.318) | `claude -p --input-format stream-json --output-format stream-json --verbose`, `--setting-sources ""` and `--strict-mcp-config` so neither a repository's nor the host user's settings, hooks, or MCP servers apply (verified live) | `--permission-mode auto --permission-prompt-tool stdio`: Claude's classifier approves what it judges safe, the rest arrives as `can_use_tool` (verified live) | `--json-schema` | `--permission-prompts none` with `dontAsk` (read-only) or `acceptEdits` and an `--allowedTools` list | `--dangerously-skip-permissions`; as root it also needs `IS_SANDBOX=1` | `--resume <id>` |
| Codex (0.162.1) | `codex app-server` (verified live) | sandbox `read-only` or `workspace-write`, `approvalPolicy: on-request`, `approvalsReviewer: auto_review` (Codex's reviewer subagent decides escalations); user-input requests reach reviewers | `outputSchema` on `turn/start` (strict: every property required) | `approvalPolicy: never` | sandbox `danger-full-access` | `thread/resume` |
| Antigravity (1.5.1) | `agy acp` (verified live) | the service's automatic rules over ACP mode `default` (read-only tasks, with `--sandbox` unless the runtime option `sandbox: false`) or `accept-edits` | prompt plus validation | automatic rules only, no reviewer | `--dangerously-skip-permissions` | none |
| OpenCode (2.0.32) | `opencode acp` (handshake verified; prompts not run, no quota) | the service's automatic rules; `OPENCODE_CONFIG_CONTENT` makes edits, shell, web fetches, and outside paths ask (edits denied for read-only tasks) | prompt plus validation | automatic rules only | every permission `allow` | none |
| Gemini CLI (0.9.0) | `gemini --experimental-acp` (not run: the account needs `GOOGLE_CLOUD_PROJECT`) | the service's automatic rules over `--approval-mode default` (read-only) or `auto_edit` | prompt plus validation | automatic rules only | `--approval-mode yolo` | none |
| DeepSeek Harness (0.2.2-rc.1, npm `@deepseek-ai/dsh`, Node 26; it does not start under Node 24.16) | `dsh --profile acp` (handshake and `session/new` verified; prompts not run, no key) | the service's automatic rules over ACP; no modes, config options `model` and `reasoning_effort` | prompt plus validation | automatic rules only | none | ACP sessions |
| pi | `pi -p --mode json`, checked against the docs of 0.73.1 | none: the CLI cannot ask | prompt plus validation | `--tools read,grep,find,ls` | all its tools | none |

Codex's sandbox and Antigravity's `--sandbox` both failed to start on the development host. Under Codex that sends every command to the reviewer subagent; operators who set `approvals_reviewer: user` would send each one to a reviewer instead. Only Claude Code reports cost, so `daily_usd` budgets bind only for it; `daily_runs` works for every runtime.

The **generic adapter** covers any CLI from config alone: a command template with `{prompt_file}`, `{workdir}`, and `{schema_file}` placeholders, an output format (`text`, `json`, `jsonl`), and a path to the final message. A runtime moves to a native adapter when it needs sessions or a structured event stream.

**Structured output** is tried in this order: the runtime's schema flag; JSON extracted from the final message with one repair turn (the router adds the schema to the prompt for runtimes without a flag). Pydantic validates the result in every case; a second invalid result fails the run and triggers fallback. Tasks that edit a worktree at length (`fix`, `self_improve`) ask for no fixed form: the diff is the result, and a short read-only task describes it.

**Models and reasoning levels.** Each runtime reports what it offers (`AgentRuntime.choices`): ACP CLIs are asked directly, by opening a session and reading its `model` and `thought_level` options without sending a prompt (no tokens; kept for an hour); others list what their CLI accepts (Claude Code's model aliases and `--effort` levels, Codex's reasoning levels). The health probe carries them, the Settings page shows each agent's model and thinking level with its CLI's defaults, and an agent's own `model` applies when a route names none, while its own `effort` wins over the route's, as its own permission mode does.

**Router.** The route for each task kind (primary runtime, fallbacks, optional verifier, model and effort, permission mode, question timeout) comes from the repository's profile, falling back to the service settings and then to `config/defaults/routing.yaml`. The service settings (8.1) declare which runtimes exist, their models, model providers, and budgets; disabling a runtime, or not installing its CLI, moves its tasks to the next runtime in each route; a missing CLI opens no circuit breaker, so it is used as soon as it is installed. A change to the settings rebuilds the runtimes without a restart; runs in progress finish on the runtime they started with. A runtime that fails with auth expiry, quota exhaustion, or a crash is skipped for a cool-down period (circuit breaker); a timeout, or two invalid answers in a row, pause only that task kind on that runtime, so a reproduction that runs out of time, or a CLI that cannot answer one kind of task, leaves the others running. An agent whose account is not signed in and that has no model provider is passed over (`AgentRouter.signed_in_check`, from `Service.signed_in`; DeepSeek Harness counts as signed in with a key, Gemini CLI with a key or its Google sign-in in `~/.gemini`, and OpenCode with credentials it stored, a provider key, or one of OpenCode Zen's `opencode/...` models chosen as its model, since without them it would run on a free model nobody chose), except for the tasks of a person who runs it on their own account; the service sweeps every agent's sign-in when its settings apply (`check_sign_ins`). Pickers, the default agent, and labels name the agent a route runs on now (its first usable agent: installed, and signed in or given a provider), not its first entry. Budgets are tracked per runtime per day from reported usage, with optional limits per repository.

**Model providers.** A runtime can use a model endpoint instead of its CLI's own sign-in. A provider has a protocol (`anthropic` for Anthropic Messages, `openai` for Chat Completions, `openai-responses`, `google` for Google's Gemini API), a base URL, its models, and the name of the secret that holds its key. Each model (`ModelSpec`) may say how much it reads and writes (`context_window`, `max_output`), the reasoning levels it takes, and, once tested, the APIs it answered a conversation on (`interfaces`) and whether it calls tools, reads images, and streams (`tools`, `images`, `streaming`, `tested_at`); a settings file may name a model by its id alone.

Nobody fills those in by hand. On the provider forms (Settings → Model providers), **Find models** reads the endpoint's model list with the key (`web/model_apis.py:list_models`; no tokens): OpenRouter, vLLM, and Google's Gemini API say each model's limits and whether it reasons, OpenAI and Anthropic only the ids. **Test** (`examine`) talks to one model for real: a two-turn conversation (it must recall a code word from the first turn) on each API the address might serve (Chat Completions, Responses, and Anthropic Messages at the same base URL, or the Gemini API), then, on the provider's own API (else the first that answered), a round trip through a tool (`add_numbers`, whose result it must use), a small red image it must name, a request with a reasoning setting that must come back with reasoning, and a streamed answer. It learns that an OpenAI endpoint wants `max_completion_tokens`, and falls back to Anthropic's adaptive thinking when a model refuses a thinking budget. Each check reports what it saw; the model's row takes the results, and the form shows which agents can run it (`agents_for`). Both follow no redirects and echo at most a short, redacted reason from the endpoint.

An agent reaches a provider over an API its adapter talks (`PROVIDER_PROTOCOLS`): the provider's own protocol, else one the model answered on when tested (`protocol_for`, applied when runtimes are built, `agents/registry.py:bind`), so a gateway that serves the OpenAI and Anthropic APIs at one address works for Codex and Claude Code alike; with no API in common the agent keeps its own sign-in, and the settings check names the problem. The adapter points the CLI at it: Claude Code through `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` (with an inherited `ANTHROPIC_API_KEY` removed, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, and `CLAUDE_CODE_SUBAGENT_MODEL` pinned to the effective model so auxiliary requests and subagents never call built-in Anthropic model ids, and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` set to a known output limit); Codex through `-c model_provider=...` and a `model_providers.<id>` entry whose `env_key` is `AM_PROVIDER_API_KEY`, over the Responses API only (Codex 0.167.0 no longer loads `wire_api = "chat"`), with `model_context_window` when known; OpenCode through an inline provider in `OPENCODE_CONFIG_CONTENT` whose key is an `{env:AM_PROVIDER_API_KEY}` reference and whose models carry their `limit`; pi through its run's `models.json` (above), including the Gemini API (`google-generative-ai`); Gemini CLI through `GOOGLE_GEMINI_BASE_URL` (the address without `/v1beta`) and `GEMINI_API_KEY`, which its configuration reference documents for API-key sign-in (not run end to end); the generic adapter through `AM_PROVIDER_*` and the protocol's usual variables. Antigravity talks only to its own service, so the settings refuse a provider for it. The key travels only in the agent's environment, never on a command line or in a file, and the exact value is scrubbed from transcripts and errors. A runtime with a provider and no model of its own uses the provider's first model; its reasoning level is picked among the levels that model takes.

The owner adds or changes the service's providers from their form (`PUT /api/settings/providers/{id}`, a settings change like any other, so anyone else's waits as a proposal); a key typed there goes straight into the secret store under the provider's `api_key` (`<ID>_API_KEY` unless named), which only the owner may do, and never into the settings. Finding and testing models may spend the service's keys, so they too are the owner's (`/api/settings/providers/query` and `/examine`). Each agent row picks what the agent runs on (its sign-in or a provider whose models it can run), the model, and the reasoning level.

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

**Installing agents.** The owner installs an agent CLI that is missing from the Settings page (`agents/install.py`, `POST /api/settings/agents/{name}/install`, gated like every service setting). A CLI comes from npm whenever its publisher ships it there, where npm checks every package against the registry's integrity hash (Codex `@openai/codex`, Claude Code `@anthropic-ai/claude-code`, DeepSeek Harness `@deepseek-ai/dsh`, pi `@mariozechner/pi-coding-agent`, OpenCode `opencode-ai`, Gemini CLI `@google/gemini-cli`), else from the publisher's latest GitHub release, checked against the SHA-256 digest GitHub lists for the asset (Antigravity, `google-antigravity/antigravity-cli`; npm's `antigravity-cli` is somebody else's package). Only these names can be installed. Each version goes into a folder of its own, `<data>/agents/<name>/<version>/` (an npm install builds in a hidden folder first, so a failed one leaves nothing), and `<data>/bin/<binary>` links to it once it is complete; `<data>/bin` is first on the service's `PATH`, so the router finds it at once. **Upgrading** is installing again: the Settings page shows each agent's installed version (from its `--version`) beside the latest its publisher offers (`AgentInstaller.latest`: npm's `latest` tag or the latest GitHub release, read at most every six hours, `GET /api/settings/agents/latest`, and at once from the agent's menu for the owner), and the owner's **Update to** installs it. Runs that started before keep the version they started with, whose folder stays; the next run takes the new one. Two versions are kept, the current and the one before; older ones are removed. In a container an installed CLI runs in place from its folder, mounted read-only, with the host's `node` mounted beside the other host tools, since the image's Node is older than these packages need. The install runs in the background; the Settings page shows its last lines and the audit log records who installed what.

**Agent accounts.** An agent CLI signs in from the dashboard: for the service, by the owner (Settings → Agents, behind the service gate), into the service user's home folder; and for oneself, by anyone signed in (Settings → Agents → My agents), into `<data>/people/<login>/home/` (`store/people.py`, folders mode 0700). `agents/login.py` runs the CLI's own sign-in for a host without a browser and relays it: Codex `codex login --device-auth` prints a page and a one-time code; Claude Code `claude auth login` and Antigravity `agy acp --login` print a page whose result is a code the person pastes back, which goes only to the CLI's standard input. pi signs in with `/login` inside its interactive UI, which also runs any shell command typed after `!`, so nobody is ever shown that UI or any terminal: the person picks what to sign pi in to (Claude Pro/Max, ChatGPT Plus/Pro, or GitHub Copilot; `PiRuntime.login_options`, the only values accepted), and `agents/pi_login.mjs` calls the sign-in that UI calls (`AuthStorage.login` from pi's own package) and prints only the page to open, a device code, and a request to paste the code or the localhost address the browser landed on, which is all it reads; pi keeps the sign-in in the account's `.pi/agent/auth.json`. Whether an account is signed in comes from `codex login status`, `claude auth status`, whether `agy -p /usage` answers, and whether pi's `auth.json` holds anything. DeepSeek Harness has no sign-in and runs on a model API (Settings shows whether it has a key, with no sign-in button; My agents asks for an OpenAI Chat Completions API of one's own); OpenCode and Gemini CLI, whose sign-ins need their interactive UIs (Gemini CLI's also runs shell commands after `!`), run on model APIs too. A person may also keep model APIs of their own (Settings → Model providers → My model APIs, `/api/me/providers`), made with the same form as the service's and tested the same way: each a `Provider` in `<data>/people/<login>/providers.json`, with its key in `<data>/people/<login>/keys.json` (mode 0600, as `provider:<id>`), never shown again; APIs kept the earlier way, inside one agent's setup, move there on first read. Finding and testing a person's models goes out from the host, so it needs a repository here, and an address that is not the host itself or link-local (`model_apis.refused_address`; private networks stay allowed, where lab machines serve models). A person who turns their own account on for an agent picks what it runs on: their CLI sign-in, or one of their own APIs with a model the agent can run and a reasoning level the model takes (`PersonalAgent`). The tasks they ask for (`AgentTask.requested_by`) then run on it, and nothing else does: the router runs such a task on a copy of the runtime whose container mounts that person's sign-in instead of the service's, or gets their API with their key, model, and level, which win over the route's (`AgentRouter._account`). Such a task tries that person's own agents first (`AgentRouter.own_agents`), then its route, so their chat, review conversations, and the reviews they start run on their own accounts by default; the route's order stays when they pick the agent (`AgentTask.agent_chosen`, the review form's and conversation composers' pickers, which list their own agents with "my API" or "my own sign-in"), when the task resumes a session (a conversation keeps an established session sticky on the agent that holds it while that agent is ready, `web/chat.py:turn_route`, even after another agent's breaker cools down; switching the picker to another agent clears the prior session and starts a fresh CLI session seeded with the conversation history in the prompt), and for verification, which wants a second opinion from the route's agent. A run on a person's own account opens only that person's breakers for the agent (`<agent>@<login>`) and counts against none of the service's budgets. Nobody else's run ever gets a person's API or key. That happens only where every run gets a container of its own, since on the host every run could read every person's sign-in; there the setting is refused and tasks run on the service's accounts. Runs record the account they ran on (`AgentRunRecord.account`), and My agents shows the person's own tokens and their own accounts' limits.

**Usage and limits.** The Agents page shows each agent's tokens over a period, from its recorded runs, and what its CLI's own `/usage` says is left of its account's limits (`ExecRuntime.quota`, read on the host in an empty folder, kept for five minutes; a request for a fresh reading still gets one at most 30 seconds old). Antigravity answers `agy -p /usage --output-format json` with groups of buckets (`remaining_fraction`, `reset_time`); Claude Code answers `claude -p /usage` with lines such as `Current week (all models): 39% used · resets ...`. Neither runs a turn or spends tokens. A CLI that cannot report its limits without running a task (Codex, whose `/status` is interactive) shows tokens only. Tokens count the runs in repositories the viewer may see; operators and maintainers of the service's repository see every run (`/api/agents/usage`).

**Doctor.** `am doctor` (and a nightly job) probes each runtime with a tiny schema task and records version, auth state, latency, and whether structured output works. A new CLI version that breaks an adapter disables that runtime and opens an issue on the code repo.

### 5.8 Policy

The policy engine decides, for each action, one of `execute`, `propose`, `shadow` (record only), or `deny` (with a reason). Inputs: the action, the autonomy level for its kind in its repository, conditions on the target (author association, team membership, labels, paths), rate limits, the pause state, freeze windows, and whether the host stands by for another (8.1, standby hosts).

Policy is a pure function of its inputs: it takes the `PolicyConfig` from the repository's approved profile, plus rate counters read from the store, and it performs no I/O itself. An approved proposal is decided again at execution time, so a pause, a hard limit, or a freeze still applies to it.

Autonomy levels per action kind: `off`, `shadow`, `propose`, `auto`. Conditions narrow a level, for example `auto` for `label.ci_gate` only when the author is a team member and the review recommends the profile's "ready" value. A ceiling from the in-repo profile file caps the level whatever later layers say.

Hard limits that no config can raise:

- At most one review and one hygiene comment per PR per head SHA; later changes edit the existing comment.
- The service edits and deletes only its own comments and threads, found by its marker.
- No `@` mentions in public text except the PR author and the `CODEOWNERS` of touched paths.
- `INTERNAL` content never reaches a public action.
- No commits or pushes to target repositories by the service's own account, and no approving reviews by it. The one push into a target repository, `pr.fix`, runs only when a maintainer approves it as themselves, with their token and their sign-off, and the one approval, `pr.approve` (the Approve button on a PR), only as the person who pressed it, with their own token: the service's writer refuses to approve (`GitHubWriter.approve_pr` without `person`), and the executor and the pipeline refuse to run either kind without `post_as` (`executor.ONLY_AS_PERSON`), nor `pr.edit` (a PR's title and description saved from its page). A merge from the PR page (`pr.merge` with `post_as`) is the person's own too; the service's account merges only an armed PR that passed the merge gate.
- A global cap on public writes per hour, and one per repository; exceeding either pauses the scope and notifies maintainers.

The **leak guard** builds `PublicText`. It rejects text that contains internal fields (scores, tiers, rubric terms, the profile's internal entity names) and text that is not in the profile's public language, then runs the style checks the profile names (by default a short list of filler phrases; a pack or the repository's style guide can add more). Tier names (`P0`, `P1`, ...) are refused only in text an agent that knew the PR's tier could have written: reviews and their revisions (`leak_guard.TIER_BLIND`) come from agents never shown a PR's priority, and review guides often rank findings on the same `P0`-`P3` scale, so there they are the findings' own labels; scores stay refused everywhere. All of an action's public text passes the guard before the actuator writes anything, so a refusal writes nothing: a proposal refused when it is approved, or failing for any other reason before its handler starts, goes back to its reviewers with the reason among its warnings (`not done: ...`), to edit and approve again. So does an approved proposal the policy then refuses (one review per head commit, the write cap, an action turned off); one held while the service is paused stays approved and runs when it resumes.

Rollout: a new action kind or a new repository starts in `shadow`. The dashboard shows the **agreement score** per action kind and repository: for each shadow action, whether a maintainer later did the same thing (applied the same label, reran the same job, merged at the same SHA). Maintainers raise the level in settings; the change goes through the configuration gate (2.3) and records who approved it. Service defaults keep every action that touches a repository at `shadow` (8.3), so nothing reaches `propose` or `auto` without the repository's maintainers.

### 5.9 Actuator and identity

The actuator executes allowed actions with one handler per action kind. A repository's actions run one at a time, in the order they were allowed, except those that hold a test machine (`lab.reproduce`, `lab.run`, `executor.ON_TEST_MACHINES`), which can take an hour: they run one at a time in a lane of their own, so the repository's reviews, labels, and comments go out meanwhile. Each handler:

1. Checks the idempotency key against the `actions` table and returns the earlier result if it already ran.
2. Re-reads the target (for example, the PR head SHA) and refuses stale actions, marking their proposals `superseded`. A review draft is the exception once a person approves it: it posts on the commit it reviewed, which GitHub shows with the review and uses to mark inline comments outdated. A draft a reviewer asked for also stays pending when the author pushes (`supersede_stale`); one nobody asked for gives way to the new head.
3. Resolves profile references: a role label becomes the repository's label name; a missing mapping makes the action a no-op with an audit entry.
4. Records an intent, executes, then records the external reference (comment ID, URL, run ID) and writes the audit log. When the forge refuses the action's only write (a 4xx answer, so nothing was written; `NotWritten`), its proposal goes back to the reviewers with the forge's reason among its warnings, to edit and approve again, instead of ending as failed. An intent without a result after a crash is reconciled before any retry (4.10).
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

**Acting as a person.** Signing in to the dashboard (authorizing the GitHub CLI) keeps that person's token in a file only the service user can read (`user-tokens.json`), keyed by their login; signing out deletes it, and a token kept from the removed GitHub App sign-in is forgotten once it expires. One reviewer's token is used only for what that reviewer approves as themselves (`post_as` must be the approver), never for the service's own reads or writes and never for another reviewer. The file guards against other users of the host, not against processes of the service's own user: an agent runs as that user and can read it unless its runtime has a confining `command_prefix` (5.7). When a maintainer approves `review.post`, `pr.fix`, `pr.update_branch`, `pr.approve`, `pr.merge`, or `pr.edit` as themselves, the actuator builds a forge writer, and for `pr.fix` a publisher, from that token, and records `posted_as` with the result. Someone signed in another way (a one-time `am login` link) has no token and approves as the service.

**Identity.** The service acts as one account on each forge, through that forge's CLI login made on the setup page (`cli_config_dir` of the forge in `local.yaml`) or, without one, the host user's own. For a team, that is an account made for the service (a machine user), so what it posts is not under a person's name. It needs write access on each target repository (to post reviews, label, and rerun CI) and on the memory repository. GitHub App and pasted-token setups were removed: a host set up with one opens the setup page on its next start, with its other answers kept, and must connect the GitHub CLI before the service starts, so that it never switches silently to whatever account the host's own GitHub CLI is logged in to (`config.legacy_github`). The profile's `people.review_identities` lists every login whose reviews count as the service's own when classifying review state, which lets a repository carry over reviews made under an earlier account, such as an old App's bot.

### 5.10 Labs

A lab, or test machine, is a host where the service may run PR code, reached locally or over SSH. The machine and its use are owned separately. **The machine** belongs to whoever owns the host's access: **test machines** in the service settings (`labs:` in `config/service.yaml`, 8.1), which maintainers of the service's repository add on the Settings page or by asking the agent, or hosts in `local.yaml`. A machine is edited in three parts: its name; how to reach it, either this host or over SSH as an `~/.ssh/config` entry (`HostName`, `User`, `Port`, `ProxyJump`, `IdentityFile`; `parse_ssh_config` refuses every other keyword, `ProxyCommand`, `LocalCommand`, and `Match exec` among them, since they run commands on the service's host); and optionally the only repositories it is offered to. Its environment and rules are written in plain language (`description`): the hardware, how to set up the environment, where data lives, what runs must not do, how long they may take. An agent drafts from that text what the service enforces (`POST /api/settings/labs/<name>/draft`): the run folder, the setup run before every reproduction, the time limit, patterns no script may match, and the GPU thresholds, plus the rules none of these can enforce. The page shows the draft and the settings diff before Save applies it through the gate, and the reproduction agent reads the description itself and follows the rest. A check connects and reports the machine's system and idle GPUs. The service reaches it as its own system user, with `-o` options built from the checked fields, and the host comes after `--`, so nothing can be read as an ssh option, which is why a repository's maintainers cannot add machines themselves: the connection uses the service host's keys. **The use** belongs to each repository's maintainers, in its profile's `labs.<machine>` and through the configuration gate (2.3): a repository uses only the machines its profile lists with `use: true` (opting in, since its environment is its own), and adds its environment setup (run after the machine's), a shorter time limit, extra forbidden patterns, protected paths, and for template runs the base clone, worktree paths, environment rules, and test templates (Appendix A.3 has an example). The repository's page lists every machine offered to it, with a switch and these settings, and the Reproduce button offers only the machines the repository uses (or to turn one on). The profile doctor flags a machine the profile uses that does not exist or is not offered to the repository.

- **Resource lease.** A GPU (or another declared resource) is leasable when it is idle by the lab's thresholds: by default less than 500 MiB of memory used, 0% utilization, and no compute processes. The lease is an `flock` on the lab's lock directory with a heartbeat and a TTL, and the idle check runs again immediately before the command starts. If nothing is free, the run falls back to the CPU-only variant of the template when one exists, and the review says GPU tests were deferred.
- **Command guard.** Lab commands come from templates, not free text. The guard rejects patterns from the lab, packs, and profile (for example `ray stop`, which stops every Ray job on a host), `rm` outside the run's worktree and throwaway environment roots, writes to protected paths, and writes to the base clone.
- **Worktrees.** One git worktree per PR under the profile's worktree root, created from the base clone, never by modifying the clone itself.
- **Cleanup** is idempotent: remove the worktree, remove throwaway environments, release the lease. It runs on success, failure, timeout, and at startup for leftovers.
- **Untrusted code.** Running an external contributor's code is `lab.run` at `propose`: a maintainer looks at the diff first, like GitHub's approval for first-time contributors' workflows.
- **Reviews test on the machine.** A review or follow-up runs in a session of its own on the first machine the repository uses that can take the PR, when the repository's `lab.review` rule lets the PR run (`auto` by default for every PR, the one repository-code action on by default, `domain/actions.py:ON_BY_DEFAULT`; a repository sets it to its team's PRs, or off, on its Test machines tab). The session is a `lab.review` with the code before the PR and at its head, which leases an idle GPU when there is one and otherwise hides the GPUs (`wants_gpu`); the reviewing agent's run lasts at least the session's time (`AgentTask.min_timeout_seconds`). A session the policy only records or refuses never holds the review: the engine runs a job again at once when every action it waits for was shadowed or denied on submission.
- **Reproductions.** A person asks for one on a PR and picks a machine, or asks in the conversation about a review. An agent reads a checkout of the head (read-only, and it may ask the maintainer who asked) and plans the reproduction: the steps in plain words, which sides to run, what each result would mean, whether it needs a GPU, and how long it may take. It reproduces the way a user would: for a PR that adds or changes a documented workflow or claims a result, it follows the PR's documentation step by step without changing the code, noting the first failure and anything the docs left out; for a plain fix, the smallest check that settles it, on the code before the PR and at its head. When the machine cannot run it at all (missing hardware such as GPUs, an accelerator, or a robot, or assets or access the steps need), the agent says so instead (`blocked`). What is approved is that plan, as `lab.reproduce`, drafted for the person who asked. Once it is approved, the actuator opens a **session** on the machine (`LabExecutor.open_session`): it checks the PR out on the service host and streams the code before the PR (the merge base) and at its head to the machine as archives over the SSH session, so the machine never gets a credential and private repositories work, into a run folder of its own under the machine's `workdir`, leasing an idle GPU first when the plan needs one. Then an agent does the reproduction itself, one command at a time: inside its container it has a `lab` command (`agents/lab_cli.py`, written into the run only when the task carries `AgentTask.lab_session`), which sends each command with the session's token over a Unix socket (`labs/sessions.py`) to the service. The service refuses a command that matches the built-in patterns (`domain/labs.py`: sudo, shutting down, `ray stop`, killing others' processes, writing to devices or system directories, removing a root or home directory), the machine's or the repository's `forbid` lists, or the repository's protected paths, and runs the rest in a fresh shell in that side's folder after the machine's and the repository's `setup`, within the session's time (the shorter of the machine's and the repository's limits). Each line of output streams back to the agent and, with each command, to the job's live view as it comes. The agent reads each output and decides the next step; when a step fails for a reason in the machine's environment rather than the PR, it finds the cause and fixes it inside the run folder, and when it cannot, it asks the maintainer who asked what to do (a review's agent writes what the machine needs in its notes). When it is done the playbook closes the session (`actuator/lab_sessions.py`), which removes the run folder and releases the GPU; a session also closes at its time limit. A restart does not end a session that is ready and has a minute or more left: the service records each open session (its folder, sides, GPU lease, setup, rules, deadline, job, and the commands run so far, never its token), and on start it adopts those under new tokens, so the job's agent resumes in the same folders with what it installed and built, and its output still reaches the job's live view; it cleans up the rest (`LabExecutor.recover_sessions`). The command that was running when the service stopped is lost: its connection belonged to the old process. The agent's report (reproduced, not reproduced, or inconclusive) goes into the PR's review: into the review that asked for it, or the PR's newest review, while it waits for a decision (an agent revises the review from the session's record, as the conversation about a review does, and the service saves it without counting it as a person's edit), and as an edit of the posted review (`review.update`, for whoever posted it to approve) once it was posted; only a PR with no review gets the result as a comment of its own. A review's reproduction that did not happen (blocked, not approved, no free GPU) is added to the draft the same way, with what a run would need. Each command runs in a login shell, so the machine's profile applies; what the profile prints (a welcome banner, notices) comes before a marker the command prints first and is dropped (`labs/hosts.py:START`), so the agent sees only its command's output. An agent that works through a session runs each `lab` command in the foreground and never ends its turn while one runs, since the end of its turn ends the session; its run may last the session's whole time plus ten minutes to report. These checks keep an agent from hurting other users of a shared machine by accident; they do not confine it. The approval, or the rule that trusts the repository's team, is the guard against a reproduction that means harm, and machines that need more isolation run sessions under a user of their own or in a container (`setup`).

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

The **learner** (`playbooks/learn`) batches signals per repository and asks an agent for memory changes. Priority overrides go through calibration (5.6). A rejection with a reason is learned from as it happens (`playbooks/learn.py`). The PR remembers it first: the review, follow-up, and triage agents for that PR read every proposal reviewers turned down there and why (`playbooks/feedback.py`), and the labels in a rejected label proposal, or removed from one before it was approved (`Proposal.declined`; the dashboard lets a reviewer leave single labels out), are not proposed for that PR again. Then an agent decides whether the reason holds for the repository's future PRs (`LESSON`), such as what a label means there, or only for that PR (`PR_SPECIFIC`), or is known already. A lesson is appended to `knowledge/review_lessons.md` as a T1 change that carries nobody's approval, since the agent wrote its words: the memory gate merges it after the notice window where the repository lets knowledge merge on its own, else once a maintainer approves it. The reviewer gets a chat note in the internal language. Review, follow-up, triage, and quality agents read the lessons. Knowledge that holds in more than one repository is proposed for `global/`. A repository uses global knowledge only if its configuration opts in (`memory.use_global`), and opting in is its maintainers' consent to receive those updates under their own T1 rules. Content from a repository's private files never moves to global memory or to another repository.

Each knowledge entry records `id`, text, source signals, `added_by`, `added_at`, optional `expires_at` (stage goals expire after 30 days by default), and confidence. A weekly **compaction** job merges duplicates, flags contradictions, drops expired entries, and keeps every `MEMORY.md` index under 200 lines.

Skills whose content depends on memory, such as the rubric section of `pr-priority`, are rendered per repository from memory at task time rather than edited in place.

**Each person's notes** (`playbooks/person_notes.py`). Besides what holds for a repository, the service keeps, per person, a short list of what they want from its agents. Each note is about the reviews drafted for them, about how the agents talk with them, or both; there are at most sixteen learned notes, plus any they write. Everything the person says and does teaches it:
- every message they write in a conversation: the chat, a review's discussion, the People conversation (`conversation`, `review_discussion`);
- their edits to review and follow-up drafts before posting (`draft_edited`);
- what they turned down and why (`proposal_rejected`);
- what they ask to remember (`chat_instruction`).

What a person writes in a conversation is kept as a signal for their notes alone, consumed at once (`consumed_by: person_notes`), so the journal never writes it to the memory repo. The first scan also keeps the conversations people had before (`backfill_said`), each message once (its signal's id is the message's).

On each review scan, a person with five new signals, whose notes last learned at least six hours ago, gets one quick agent run (`person_notes`, in the `quality` pool, `budget: quick`). It turns the new signals and the current notes into the notes as they should be. A note the person wrote, or changed, is theirs: learning keeps it as it is. Every review, follow-up, and revision drafted for them reads their notes about reviews; every conversation with them (chat, review discussions, the People conversation) reads those about conversations. The repository's lessons win where the two differ. Scans and events work for nobody and read none.

The notes shape only what the agents do for that person, so they belong to the person and need nobody's approval: they live in the service's store (`person_notes:<login>`). Each person sees and edits their own, choosing what each is about, and turns their learning off, under Settings → Agents → **My preferences** (`/api/me/notes`). The same section shows how many of the review drafts they posted they edited first, over the last 30 days and the 30 before. A conversation saves a preference the person states (`preference` proposals, with what it is about). The owner of the service can start learning now for everyone with something new to learn from (`POST /api/me/notes/everyone/learn`), which respects each person's choice to turn learning off. A note about reviews that the agent finds other people hold too, and that holds for every review of a repository rather than one person's taste, is proposed as that repository's review lesson, which waits for a maintainer.

### 5.12 Self-evolution and self-update

The service grows in two ways. Its memory grows from what reviewers teach it (5.11). Its code grows from what they ask for: a reviewer asks in chat for something the service cannot do yet, the service changes its own code, and its maintainers decide whether that code is merged and when the service runs it.

**From a conversation to a PR.** When a request needs code rather than a setting or a lesson, the chat agent proposes `improve` with what the person asked for, at the size they asked for it, and what it found that the implementing agent needs; the design is left to that agent (5.13). The answer shows it as a request card; nothing starts until the owner clicks **Approve and start**, on the card or under **Requests from conversations** on the Approvals page, where the owner sees every such card (the person who asked may decline it). A conversation has at most one such request pending or running. The PR's commit carries `Requested-by:` the person who asked, and its description names who approved the start. Then the `self_improve` playbook starts:

1. It prepares a worktree of the service's repository (`code_repo` in `local.yaml`) at the tip of its default branch (`self_update.branch`, `main` by default).
2. An agent on the `self_improve` route, in `workspace_write` mode and auto permissions, implements the request following that repository's `AGENTS.md`, adds tests, and updates the docs it changes. Whatever it would ask a person goes to the question desk like any other agent run (5.7), answered by maintainers of the code repository.
3. The playbook collects the change as one patch (`Workspace.changes`) and runs the checks itself in the sandbox, whatever the agent reported: the tests (`uv run --frozen pytest -q -x`), `ruff check`, and `ruff format --check`, the same as the code repository's CI (`.github/workflows/ci.yml`). The agent runs only the tests of what it changed; when a check fails, the end of its output goes back to the same agent, resuming its session (`PlaybookContext.ask(after=...)`), at most twice (`MAX_REPAIRS`), and not again once a turn leaves the same failures (each turn reads the whole session again). The checks run in a fresh container of the agents' image (`agents/container.py:ContainedSandbox`, sharing a package cache) when the host runs agents in containers, since the code is the agent's. The agent changes nothing, and proposes how to split the request, when it needs more than one PR's worth of change. A change whose checks still fail opens no PR: the thread is told which checks failed, with their output, and the files changed, and the job ends as failed. The editing agent does not have to answer in a fixed form (long edits often end without one): the diff is what it did, and a second, read-only task on the same route describes the diff as a PR title and summary, falling back to the request and the agent's closing message.
4. It emits a `code.pr` action. The actuator applies the patch to a fresh worktree of the base commit, commits it as the service with a `Requested-by:` trailer, pushes the branch `am/self/<slug>-<id>`, and opens a PR on the default branch whose description holds the request, the summary, the check results, and the agent's own account. The chat thread gets a link.

A request the agent declines, or one that ends with no change, opens no PR; the thread is told why. A run whose agent fails for a reason a retry would not fix ends the job as failed; a timeout, a quota, a crash, or every agent paused is retried with backoff like any job.

**Only the owner updates, merges, and deploys.** Separate approvals, each by the owner, checked live (an admin of the code repository) at the moment of the click:

- **Update with base.** On the Service page (`POST /api/service/prs/{number}/merge-base`), each self-improvement PR (`am/self/*`) shows whether it conflicts with its base branch and offers **Merge `<base>`** or **Resolve conflicts with `<base>`**. Without conflicts, `code.update` updates the branch on the forge at once. With conflicts, `merge_base` runs the conflict-resolving agent on the branch and proposes `code.update` with the resolution diff; once the owner approves it, the actuator pushes the merge commit (with `Requested-by:`) onto the PR's `am/self/*` branch.
- **Merge.** The Service page lists open PRs on the code repository, marking self-improvement PRs; **Approve and merge** refuses a conflicting PR with a clear message and otherwise submits a `code.merge` action, which the actuator executes with a squash merge. Merging on GitHub itself works the same way, under that repository's own branch protection.
- **Deploy.** The Service page shows the commit the service runs and the commits on the default branch it does not run yet. **Deploy** submits a `service.deploy` action for the tip of the branch. A merge never deploys by itself.

All three actions carry the approver in `requested_by`, and their records and outcomes show on the Service page and in the audit log.

**Deploying.** The `ServiceUpdater` (`selfupdate/updater.py`) runs the service from its own git checkout and only moves it to commits on the default branch of the service's repository. It fetches that branch from `code_repo` itself, as the service's account there (into `refs/remotes/am-code/`), whatever remote the checkout was cloned from, so a host cloned from the upstream code runs what is merged in its owner's fork. A deploy:

1. refuses a checkout with local changes and a commit that is not on the default branch of the service's repository;
2. checks the commit out in a staging worktree under the data directory, runs `uv sync --frozen` and the test command there (`self_update.test_command`, the unit tests by default), and stops on any failure, leaving the running code untouched;
3. fast-forwards the checkout's branch to the commit (or checks it out detached), syncs the environment, and rebuilds the dashboard when Node is available (a failed dashboard build keeps the previous build);
4. records the previous commit in `deploy.json` and asks the service to restart: `am serve` exits with code 75.

`am supervise` runs `am serve` and starts it again after exit code 75. `am start` runs the supervisor in the background in a session of its own (its process id in `<data>/am.pid`), with `--log-file`: the supervisor then writes everything the service prints, and its own messages, to `<data>/logs/service.log`, rotated by size; being the only writer, it rotates safely. `am logs [-f]`, `am status`, and `am stop` (one SIGTERM, which the supervisor passes on gracefully) work on that background service, and the Service page shows the end of the log, scrubbed of credentials, to maintainers of the service's repository. If the service dies before it confirms the deploy (it does after `confirm_deploy_after`, 60 seconds of running), the supervisor checks out the previous commit, syncs the environment, and starts the old code; the service then records the rollback in the audit log. Any other failure restarts with a growing delay; exit code 0 stops the supervisor. Ctrl+C or SIGTERM stops the supervisor only after the service: it gets one SIGTERM and 20 seconds, and a second Ctrl+C kills it. The service itself never waits on an open dashboard when it stops: its live event streams end at once, other connections get five seconds, and stopping the workers gets fifteen. Under systemd, run `am supervise` as the unit's command.

Database migrations run when the new code starts. A release never changes a repository's configuration by itself: when new defaults or packs change a repository's merged profile, the repository keeps its current lock and its maintainers get a candidate to approve (5.2).

### 5.13 Web dashboard and API

Authentication uses the GitHub CLI, the only way to sign in with GitHub: the person enters a one-time code on github.com to authorize it, and its token can do what that person can do on every repository they reach. Each sign-in runs `gh auth login` in a folder of its own under `<data>/sign-in/` (mode 0700, with git's global configuration redirected into it and `--insecure-storage`, so the token never lands in the host's own `gh` configuration or in the system keyring every `gh` on the host shares); the service reads the token, deletes the folder, and keeps the token under that login only. Folders left by a sign-in that never finished are removed when the server starts. `gh` is looked up on the server's `PATH` on every visit; when the host has none, the sign-in page and the setup page offer to install it (`ghcli.install`): the latest release of `cli/cli` for the platform is downloaded into `<data>/bin`, checked against the release's checksums file, and put first on `PATH`, with nothing else on the host changed and no administrator rights. The install route is open before sign-in, since nobody can sign in without it, and does nothing once `gh` is there. On the host, people can also sign in with one-time links that `am login` prints: a link signs in once, within ten minutes, as the GitHub account logged in to the GitHub CLI there, so only someone with access to the host can mint one. Sessions are HMAC-signed cookies. The user's permission on each configured repository is cached for 10 minutes. A user sees only the repositories where they have write access or higher, or a place in the People record and at least read access (`web/api.py:may_view`). On those, they can view everything, override priority, arm merges, claim PRs, and chat, and decide the proposals they asked for; a repository's maintainers (2.3) decide every proposal and assign reviewers. Changing anything in a repository's configuration (profile, autonomy, rubric, routing, labs, notification routes, the People record) goes through the configuration gate (2.3), which checks at the moment of the change that the user maintains that repository. Changing the service settings goes through the same gate against the service's own repository. Operators, listed in `local.yaml`, can pause the service and see the host's health; they see a repository's data only if they also maintain it.

The passwordless sign-in `/auth/dev` exists only in `am serve --demo`, which runs on fakes. An instance that talks to a real forge never offers it, in dev mode included, because anyone who reached it could sign in as a maintainer and pass the configuration gate.

**Layout.** A top bar holds the pages in order of daily use (Home, Pull requests, CI health, Repositories, Approvals, Agent questions, Settings, then the rest, with Memory, Service, and the Audit log last): as many as fit the window, measured as it resizes and in the current language, with the rest under More; a search and command palette (Ctrl or Cmd+K) that reaches every page, repository, PR, and command, and hands anything else to the agent; a bell with what waits on the user; and the account menu (settings, theme, language, keyboard shortcuts, pause). Every list (PRs, proposals, questions, memory changes, CI failures, runs, the audit log) has the same controls: search, filters by facet, sorting by any column, grouping, paging, selection with bulk actions, and keyboard navigation (`j`, `k`, Enter, `x`, `/`). The view lives in the URL, so it can be shared and survives the back button; for Home and Pull requests it is also remembered in the browser (`ui/src/viewState.ts`, leaving `?repo=` to `useRepoScope` and dropping `page` when the repository changes), so returning from a PR through breadcrumbs, top navigation, keyboard shortcuts, or the command palette keeps filters, search, tabs, grouping, paging, and sorting, while a link that carries explicit view parameters wins and replaces the remembered view. The agent is on every page (Ctrl or Cmd+J, or Ask agent): it opens beside the page and knows what the page shows (see Chat below). Popovers and menus (`ui/src/popover.ts`) measure their trigger and clamp or flip to stay inside the visible window and clear of the open agent panel. On narrow viewports (`640px` and below), multi-column grids, sidebars, form rows, and definition lists collapse to a single column, tables with a primary column render each row as a stacked card with a full-width title while other wide tables and diffs scroll horizontally in their own boxes, and tabs scroll horizontally without wrapping.


**Live updates.** Pages stay current through the server-sent event stream `/api/events` (`ui/src/events.ts`): each event reloads the queries it touches, and an approval, a write, a settings or memory change, or a finished install or sign-in reloads everything on screen, at most once every three seconds. The bus hands an event to the streams after its handlers, one event at a time, so a handler's slow step (reading a PR from the forge again after the service wrote to it) runs beside it (`EventBus.later`) and holds back no other page's update. Some changes start no playbook but still show: a poll that saved any PR change sends `pr.refreshed` once per repository (a resolved thread, a review asked for), and installs, sign-ins, and the sign-in sweep send `service.changed`; only dashboards see either. The stream sends a `ping` event every 15 seconds; a page that hears nothing for 45 connects again, and after every reconnect reloads what it shows, for the events it missed. Every action a person takes on the dashboard reloads the queries on screen when it succeeds, except those that start agent CLIs (`/api/agents` probes, the CLIs' limits); the poller reads every open PR every `poll_seconds` (120 by default), so approvals and comments made on GitHub show within that.

**Markdown.** Descriptions, reviews, comments, and agents' messages render as GitHub renders them (`ui/src/markdown.tsx`): `marked` turns GitHub-flavoured Markdown, with its HTML, into HTML; an inert document parses it, and only allowlisted tags and attributes become React elements, so no HTML is injected: scripts, styles, event handlers, ids, and other tags' markup are dropped, links open only http(s), mailto, and in-page URLs, and images load only over http(s), without a referrer.

Pages:

- **Home**: the control center, in one column. With several repositories, the page shows one at a time, chosen with a switch (`?repo=`, remembered in the browser and shared with the Pull requests page), because each repository's priorities come from its own rubric and do not compare or add up across repositories; questions and changes that belong to no repository show under every one. A status line (decisions waiting, PRs to review, top priority, agents working, the service). Until it is done or hidden, a getting-started checklist whose steps carry their own buttons, from adding a repository to the first reviews. **Agents' work**: the user's agent jobs (or everyone's) as needs you, working, and finished; each opens the job's live view. A repository's priority scoring shows as one line with how many PRs are scored, opening its progress on the Pull requests page, since one request scores many PRs. The PRs that need review, with checkboxes, an agent picker, and one button that reviews the selection; each row shows the PR's quality (its review's score, or the quick one at a glance), when it has one, and who is reviewing it with which agent and opens that review's live view. Under it, when reviews run in each repository (only when asked, every N hours up to M PRs, or on every update, and whether new commits are re-reviewed), changeable in place through the configuration gate; someone who does not pass it proposes the change instead. Last, one feed of every decision (questions for the user first, then other agents' questions, approvals, memory changes, and service updates), each answerable in place.
- **Pull requests**: the ranked review queue across the user's repositories (ranked by tier first, since a maintainer's override sets the tier and leaves the score, then by score; `domain/scoring.py:priority_key`, which the review and quality scans also follow), with saved views (all, needs review, owned by me, I review, not evaluated, reviewed). A PR's owners and reviewers (3.2) are who claimed the role or was assigned it on the dashboard, and who was given it on the forge itself: the poller copies a review request a person made there into the reviewer role and an assignee into the owner role (`engine/poller.py:sync_roles`, shown as asked on GitHub). It copies only what changed on the forge since its last copy, so a role removed here stays removed, and it removes a role from the forge that the forge withdraws unless its reviewer reviewed (a review ends GitHub's request). The requests GitHub makes on its own for code owners (`asCodeOwner`) give no one a role, so a code owner's lists hold only the PRs given to them. Each row of the PR list and of Home's PRs to review has a line of its own naming its owners and reviewers; the PR list's columns are the tier with the score under it, quality, the PR (title with its labels, number and author, people, summary), its review (state and progress), and its activity (updated and opened, each sortable), and both have **Owned by me** and **I review** filters (on Home they look through every open PR, not only those waiting for a review, since an owner follows a PR to its merge) and filters for PRs with or without an owner or reviewers; each row also shows its review progress, the same numbers as the PR page (`web/api.py:_progress_row`: the share of review threads addressed, counting those a comment check found fixed, changes requested, how many of its reviewers reviewed) and its approvals (`domain/approvals.py:approval_summary`: approvals that count under the base branch's rules against those required, how many more are needed, at least as many as the fewest owners whose approvals cover the code-owner parts still waiting, and how many of its assigned reviewers approved; the PR page's Approvals names who is still waited for), sortable by the share addressed, a quality column (the review's score in its band's color, or before a review the quick score at a glance, outlined dashed; the agent's reason on hover; faded once new commits arrived since it was given; Score quality on a selection or, after a confirmation, the whole view scores it now: a reviewed PR from its review, any other quickly, and several at once in the background pool so they never hold up a review; Score priority is the same for priority), filters for repository, tier, review state, quality, author, label, and claim, one repository at a time behind the same switch as Home (so PRs rank only against their own repository's), review in bulk, Score again (a selection, or every PR in the view after a confirmation, since each is one agent run; while it runs, a bar above the list counts the PRs scored, and each agent's job opens its live view), and Sync with GitHub, which reads every repository's PRs now, in the background, instead of at the next poll.
- **PR**: the description and the service's reviews rendered as GitHub renders Markdown, in full; a **Review progress** tab for its owners (`GET /api/prs/{repo}/{n}/progress`, `domain/review_progress.py`): how much of the feedback is addressed (review threads resolved on the forge or found fixed in the code by a comment check; each reviewer's row opens to their comments, each summed up in one line with whether it is done, its reason, a link to it, a Check button that checks it against the code now, and a Resolve button that resolves it on the forge as the person who presses it; **Check all** checks every open comment again, even those checked at this head, the profile's `max_comments` at a time so each batch shows as it ends; **Resolve all** resolves on the forge, as the person (`POST /api/prs/{repo}/{n}/threads/resolve`), either the comments the latest check found fixed or every open one they may resolve, and names those it left and why; a thread whose lines changed but nobody resolved counts as open, shown apart; threads the author opened count nowhere), the approvals against what the branch requires, who requested changes, its reviewers (those assigned here first, then anyone else who reviewed or opened a thread; someone GitHub only asks, who has not reviewed, is not listed) with their latest verdict, whether it was given at the current head, whether they are code owners, and their threads resolved, the code owners' approvals (each owner's verdict and how many parts of the change an owner approved), and a timeline of the opening, the roles people took here, each review with the threads it opened and how many are resolved now, and the pushes between (from each commit's `committed_at`); checks, files, timeline of the service's actions, and CI diagnoses, beside the quality (the review's accurate score, or the quick one at a glance before it, with its band and band thresholds on hover, the agent's reason, the quick score kept beside the review's, and a button to score it again; the band also shows under the title) and the internal priority (with an override and reason, and Score again), the merge checklist, and hygiene results; the **People** card with **Owners** and **Reviewers** groups (everyone who holds either role here, with their GitHub avatars and who assigned them; anyone claims either role or leaves it, and **+ Assign** opens the login field and People record suggestions so a maintainer assigns anyone to either and the PR's owners assign its reviewers, optionally requesting their review on GitHub; who assigns takes off), the **Code owners** card (kept apart from both: whom to ask for the approvals a merge waits on; `GET /api/prs/{repo}/{n}/code-owners` reads CODEOWNERS from the PR's base branch, groups the changed files by the owners of their last matching rule, and shows for each part whether a code owner other than the author approved it, each owner's latest verdict and whether GitHub asks them, the files no rule owns, whether the branch requires code-owner reviews (`RepoInfo.requires_code_owner_reviews`, from branch protection or rulesets), and the others GitHub asks to review), **Approve** (approves the PR on GitHub as the person, at the head they are looking at, with an optional comment; it shows Approved by you once their newest review approves that head, and is absent on their own PR), a **Review** / **Follow up** split-button whose caret picks the agent, an **Actions** menu holding the secondary actions inline (**Fix with agent**, **Reproduce**, **Fix CI**, **Run CI** when actionable, **Merge <base>** / **Resolve conflicts with <base>**), **Open on <forge>** as a button of its own beside it (a frequent action), contextual **Run CI** and **Fix CI** (when a job failed at the head: the agent, a test machine or none, whether it needs a GPU, and a note; the fix comes back as a draft the person pushes as themselves) on **Checks** and **CI diagnoses**, **Merge <base>** / **Resolve conflicts** beside **Arm merge** in the **Merge** card (**Merge <base>** only while the branch lacks commits of its base: `GET /api/prs/{repo}/{n}/merge-now` says how many, `ForgeReader.behind_by`), **Merge now** in the same card, **Edit** beside **Approve**, and the refresh button (read the PR from GitHub now). **Merge now** (`POST /api/prs/{repo}/{n}/merge-now`) merges the PR into its base at once as the person who presses it, with their own sign-in: it offers only the methods the repository allows (`RepoInfo.merge_methods`, from GitHub's `allow_squash_merge`, `allow_merge_commit`, and `allow_rebase_merge`), starting with the profile's `merge.method` when allowed, and pre-fills the commit title and message from the profile's merge settings (`merge_gate.squash_message`), both editable and absent for a rebase. The press is the approval, so it runs at any `pr.merge` level but `off`; it skips the service's merge gate and needs only that GitHub reports the PR mergeable at the head the person saw. **Edit** (`POST /api/prs/{repo}/{n}/edit`) saves the PR's title and description on GitHub as the person (`pr.edit`, only as them); mentions the description already had may stay, and new text passes the leak guard. Run CI does the one step that gets CI going at the head (`domain/run_ci.py`): it adds the `ci_gate` label when the repository starts its full CI on one and the PR lacks it, and otherwise reruns the failed runs whose jobs are done; while CI runs the button says so, once it passed **Checks** shows a passed pill, and when there is no CI it hides. When GitHub holds the PR's workflows until a maintainer approves them (a first-time contributor's fork PR: a check suite whose conclusion is `ACTION_REQUIRED` and that has no jobs, read as a check that has not run, `CheckRun.awaiting_approval`), CI has neither passed nor failed, the merge gate waits, and the button is **Approve and run CI**, which approves those runs on GitHub as the person who pressed it (`ci.approve_run`, only with their own sign-in). The press is the person's approval: the action is drafted for them and approved at once, so it runs even where the level is `shadow`, while a pause, the gate budget, the rerun limit per commit, and the hard limits still apply, and the audit log names them. Above the tabs, **On this PR** lists every agent job on it, anyone's (reviews, fixes, reproductions, priority scoring; running ones first, then the last week's), each opening its live view, and every draft for it waiting for a decision, decidable in place. A draft can be edited in a full-width editor and saved without posting (the draft keeps the edit, marked edited); posting sends the saved version. **Discuss** on a review draft opens the conversation about that review, where the agent can revise it. **Translate to …** on a review draft, and on each review in the Reviews tab, has an agent translate its main comment and inline comments into the language the dashboard shows (`POST /actions/{id}/translate`, task kind `translate`, quick and read-only, the review as untrusted data, kept per text and language), for reading there only: the draft and what is posted stay in the repository's public language. Jobs that ran no agent and skipped (a follow-up for a PR with no review yet takes the same request as the review) are left out here and on Home, and so is scoring nobody asked for until an agent works on it (most PR updates only recompute the total).
- **Repositories**: onboarding; per repository, the approved profile with the hash of each layer, what the service does on its own (each action kind's level, `off`, record only, ask first, or automatic, changeable in place through the gate, beside its agreement score from shadow actions), a dedicated **Test machines** tab with the repository's `lab.reproduce` policy presets (`off`, record only, ask first, or automatic for team PRs and ask first for others) and each offered machine's connection check and inline environment settings (`setup`, `timeout_seconds`, `forbid`, `protected_paths`) (5.10), the pending candidate with its differences in plain words (each changed setting under its section with a readable label, was and now, unchanged sections folded, the YAML diff one click away; before a first approval, the whole profile by section, actions grouped by level), how its PRs are reviewed (the review skill, chosen among the repository's own and the service's, the maintainers' review instructions, and a **Work them out with the agent** button; while nobody has chosen and the repository has no review skill, every tab asks), how its reviews read and score (the writing and quality guides, each the service's default until a maintainer changes it through the gate, and whether every open PR gets a quick score), per-repository pause, a **Usage** tab (agent runs, tokens, and reported cost over a chosen period, per person each run worked for: `AgentRunRecord.requested_by`, else the requester of its job, else the service; maintainers see everyone, others the total and their own row), and the repository's settings layer, changed by asking the agent or by editing it. Saving opens a memory change that applies once a maintainer of that repository approves it; when the editor qualifies, saving is that approval.
- **Approvals** (proposals): every pending proposal, shown as it will look: Markdown rendered the way GitHub renders it (or as source), each inline comment under the diff lines it is about (and flagged when the line is not in the PR's current diff), a reproduction's script, a fix's diff, a plain sentence for labels, reviewers, and reruns. Each card has one main button that says what approving does (Post, Post as me, Run on <machine>, Open the PR as me); editing, rejecting, and handing a review to an agent to fix are secondary. Inline comments can be edited or removed one by one. Approvable in bulk.
- **The live view of a job** opens beside any page (`?job=<id>`, or the id of the request that starts it): who asked, which agent, its steps as they happen (messages, the commands and files it used, questions and how they were answered), the questions it waits on, answerable there, what it produced, answerable there too, and its outcome. The person who asked, or a maintainer of the repository, can stop it; its agent process ends and its checkout is released.
- **The conversation about a review** sits on the live view of a review or follow-up (`web/review_talk.py`), and every draft of a review has a **Discuss** button that opens it. While the agent writes the review, a message goes to it at work: the review and follow-up tasks set `AgentTask.messages`, the runtime opens the job's box in the router's `Inbox` for the run, and `AgentRunner.tell` hands the message, wrapped by `playbooks.prompts.told_block`, to the CLI's `Conversation.tell`. An ACP agent's turn is cancelled (`session/cancel`) and the message starts the next turn of the same session, so the agent keeps what it read; Claude Code reads it as the next user message once its turn ends; a CLI without a two-way conversation, or a moment between agent steps, takes none, and the dashboard says so. The review the agent finishes answers the message, and later turns read it in the conversation. Everyone who may see the repository shares it. A reviewer asks the agent why it found something, disagrees with a finding, has it change the draft, or has it check more (other files, a test, the docs). Each turn runs an agent, on the review's route and preferably the agent that wrote the review, in a checkout of the reviewed commit kept for the conversation, with the PR and its diff, the repository's review skill and format, the reproduction on record, the lessons, the review as structured data, the draft as it would be posted (reviewers' edits included), and the conversation so far; each turn carries all of it because not every CLI can resume a session. Its questions go to the reviewer who wrote the message and wait in the conversation, as the review's own questions wait on the live view. A turn that changes the review returns it whole; the service renders it as the playbook does and saves it on the pending draft through `ActionPipeline.save_edits`, as an edit by that reviewer, so the findings a fix or a follow-up reads change with the text, and the leak check reads the new text. A rejected draft is not changed; the agent still answers. For a review already posted, the revision becomes a `review.update` draft (`revises` names the `review.post` action, `head_sha` stays the reviewed commit, so its marker does too), and later turns change that draft while it waits. GitHub lets only a review's author edit it, so the draft names who posted it (`author`): the service's review is edited as the service, and a review a maintainer posted as themselves only by that maintainer, as themselves (`ActionPipeline.approve` refuses anyone else, and an edit cannot change `target_id`, `revises`, or `author`). Once it runs, the posted text changes, and the `review.post` action and the review record take the revised body and findings, so follow-ups and scores read the revision. Comments already posted on lines of the diff stay as they are. Lessons the reviewer states or agrees to are saved as chat lessons are. When the reviewer asks for a run on a test machine, or agrees to one, the agent says what it should check (`reproduce`), and the service starts a reproduction for that reviewer on the first machine the repository uses, as the Reproduce button does; its script comes back to them to approve before it runs. One turn runs at a time per conversation. Nothing is posted from a conversation: the draft waits for a decision as before.
- **The conversation about a change** sits on the live view of a fix job (`web/fix_talk.py`, on the same endpoints as a review's), and a `pr.fix` draft's **Discuss** button opens it; on a PR's list of agent work, a review, follow-up, or fix that ended says **Talk**. While the agent makes the change, a message reaches it at work as above. Once the job ends, the requester or a maintainer of the repository talks with it, and each turn runs an agent on the fix route (preferably the agent that made the change) in a checkout of the change as it stands: the draft's base commit with its diff applied (`Workspace.apply`), kept for the conversation while it still holds exactly that, so a session resumes there. The prompt carries the conversation, the first request, the change's description and diff, the review skill, and the PR. The agent answers, and may edit the files. A diff that changed replaces the pending draft's through `ActionPipeline.save_edits`, as that person's edit, and a short read-only task writes the PR title and description again, as the fix playbook does. When the draft was already decided, or the job drafted nothing, the change becomes a new `pr.fix` draft for whoever asked, carrying the job in its provenance: on the PR's head when the earlier change was pushed or opened as its own PR (the turn's checkout is that head) or nothing was drafted, else on the earlier draft's base with its diff. Only that person pushes it, as themselves.
- **Agent questions**: what agents are waiting on (5.7), with what each wants, the PR it works on, and when the question expires; answer with allow, deny and a message, or answers to each question. A strip and a toast announce new questions, with an optional browser notification, and the bell counts them.
- **Memory**: memory changes waiting for approval (a change that locks a profile shows what it changes the same way), and a browser over the memory repository.
- **Display names** (`web/aliases.py`): the name each login goes by on the dashboard, such as a person's real name, in place of the login wherever one shows (avatars, people's cards, code owners, search on Home and the PR list), with the login on hover. A person sets or clears their own from the account menu (`PUT /api/me/alias`); the service's owner may name a login whose person has set none (`PUT /api/aliases/{login}`, Settings → System → Display names), which the person's own replaces and the owner cannot then change. Two people never share a name, and a name is never another person's login. Names are internal: nothing the service posts uses them.
- **Settings**: the service settings (8.1) in one place: agents with their status, what each runs on (its sign-in or a provider), model, reasoning level, and budget, and below them the signed-in person's own agents; model providers (5.7), the service's and the person's own, each added and changed through one form that finds the endpoint's models and tests each by talking to it, with their keys' status; the default route per task; system values; test machines with a connection check, inline editing, and a repository picker over onboarded repositories; and the settings file itself. Each row shows its current value and whether it differs from the default. The intended way to change them is to tell the agent what should change: it drafts the edit, the page shows the diff, and a maintainer of the service's repository applies it. API keys go only into their key fields; they are never shown again.
- **CI health**, **Agents** (runs, outcomes, duration, cost), **Conversations** (every thread with the agent), **Service** (5.12), and the **Audit log**.

The UI is in Chinese by default, with a per-user toggle to English; public text on the forge always uses the repository's public language.

API sketch (JSON, all under `/api`, SSE for streams):

```
GET  /repos                              POST /repos   (onboard)
GET  /repos/{owner}/{repo}/profile       POST /repos/{owner}/{repo}/doctor
GET  /prs?repo=&tier=&state=&mine=       GET  /prs/{owner}/{repo}/{n}
POST /prs/{owner}/{repo}/{n}/override    POST|DELETE /prs/{owner}/{repo}/{n}/claim
POST /prs/{owner}/{repo}/{n}/assignees   DELETE /prs/{owner}/{repo}/{n}/assignees/{login}
POST /prs/{owner}/{repo}/{n}/approve     GET  /repos/{owner}/{repo}/assignable
GET  /repos/{owner}/{repo}/usage?days=
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

**Chat.** Each thread belongs to one maintainer and runs on the `chat` route (ACP when available, otherwise exec with session resume), or on the agent chosen in the conversation's picker (`ChatThread.agent`, changeable before or during a conversation across chat, review conversations when not actively `while_running`, and People conversations; each assistant `ChatMessage` records `runtime`). The agent receives the global and per-repository `MEMORY.md` indexes for the maintainer's repositories, the maintainer's role, and a snapshot of their queue, and it uses the service's MCP server:

- read tools: `list_repos`, `get_profile`, `search_prs`, `get_pr`, `get_evaluation`, `get_ci_status`, `list_proposals`, `read_memory`, `get_run_transcript`;
- propose tools: `propose_action`, `propose_memory_change`, `propose_override`, `run_playbook`.

Until the MCP server exists, the chat agent returns its proposals as structured output (`ChatAnswer.proposals`), which the service turns into the same proposals. It reads the service's records as freely as the person it talks with: every conversation's agent (the chat, a review's conversation, the People record's) gets `am-read`, a read-only door to the dashboard (`web/records.py`). `am-read /api/<path>` asks the door, over a Unix socket mounted into the agent's container, for that path of the dashboard's API; the door answers it through the dashboard's own routes and permission checks with the person's session, which the agent never holds, for a token that lives for that turn only. Only `GET` of `/api/...` passes, never the event stream; answers are cut at 400 KB and stripped of anything that looks like a credential. So the agent sees everything the person may see, on any page, not only the page they have open, which the prompt names only as where to look first; `am-read --index` lists every path, and three exist for agents above all: a PR's record (`/api/prs/<repo>/<n>/record`), a repository's failures, and the log lines about an id (operators and the owner). The service guide (`skills/maintainer-chat`) says what to read for which question and what each state means, so the prompt does not carry every record up front. The review queue and the service settings (about 18 KB together) go into a chat message's prompt only from the pages that show them or when the person's recent messages are about them (`web/chat.py:QUEUE_WORDS`, `SETTINGS_WORDS`); otherwise the prompt names the paths to read them. A structured task's schema is in its prompt once: the router adds only the instruction to end with it for an agent without a schema flag (`agents/structured.py:schema_instructions`). A run whose model's safeguards stopped a request and whose CLI then failed on its fallback model says so in its error (`RunState.refusal`) and counts as an ordinary failure, not a sign-in one, so no breaker opens on the account. Proposals created in a thread appear inline with approve and reject buttons, so the maintainer sees exactly what will be posted before it is. On a PR, a chat answer can propose labels, a rerun, review requests, a comment, a nudge, and closing the PR (`pr.close`, with an optional comment posted first, such as why it duplicates another); each waits for its card's approval even where its level would run it (`ActionPipeline.submit(force_propose=True)`). A configuration change proposed in chat applies only after a maintainer of that repository approves it, which the chatting maintainer can do if they qualify. A request the service cannot fulfill with a setting or a lesson becomes an `improve` request that changes its own code (5.12), shown as a request card that waits for a maintainer of the service's repository; `add_repo` offers to onboard a repository the same way, approved by someone who may configure it. Those who may approve a card see it on the Approvals page as well as the person who asked in the thread. The chat agent follows a guide to the service and the dashboard (`skills/maintainer-chat`), so it points to what exists instead of asking for code.

**The agent on every page.** A conversation works in a checkout of its repository (at the PR's head when it started on a PR); one outside any repository works in a checkout of the service's own code at the commit it runs, a worktree apart from the running checkout, so questions about the service are answered from its code and nothing the agent does there changes what runs. Every task about the service may read that code too: conversations (chat, and the discussions of a review or a change, `activity` `*_talk`) and configuring it (`governance`, `onboard`; `agents/router.py:reads_service_code`). The service exports the commit it runs with `git archive` into `<data>/service-code/<commit>` (tracked files only, so no git settings that could hold credentials, read-only), and the router adds it to those tasks' read-only folders (`AgentTask.reference_dirs`, mounted read-only in a container) and says in the prompt where it is (`AgentRouter.service_code`), so an agent can read how the service did what it is asked about. Work on a target repository (reviews, CI diagnosis, scores) is not offered it: agents offered it read it whatever the task, and on 2026-11-11 most CI diagnoses spent their tokens there. The panel sends only the page's identifiers (the page, a repository, a PR number); the service reads that page's data itself, so the agent sees what the maintainer may see and nothing the browser could invent. Each page keeps its own conversation for the browser session, and every conversation is also on the Conversations page. The agent receives the service settings, their defaults, and which secrets are set (never their values). It drafts configuration as a `configure` proposal holding the complete new document, either the service settings or a repository's settings layer; the service checks it and shows it in the thread with its diff and an Apply button that goes through the gate. Text that looks like a credential is removed from a message before it is stored or sent to the agent, and a drafted document that contains one is refused. While the agent answers, the panel shows its steps as they happen (its thinking where the CLI reports it, tool calls and their results), the same steps a job's live view shows; each answer keeps them behind "What the agent did". What an answer started stays under it as a card with its outcome: an action or configuration draft waiting for approval, a lesson (saved at once when the asker may configure the repository, else waiting for a maintainer), or a self-improvement job, which starts at once, opens in its live view for whoever asked, and runs one at a time per conversation.

### 5.14 Notifications

Adapters for Feishu/Lark bots, Slack webhooks, and email. Routes are configured per event type (a top-tier PR arrived, a proposal waiting more than N hours, a runner offline, a scope paused, a deploy failed, a digest) and per repository, so each repository's maintainers get their own channel. Quiet hours hold non-urgent messages.

### 5.15 Observability and audit

- Structured JSON logs with repository, job, run, and action IDs on every line.
- Agent transcripts stored per run under `runs/<id>/` with a retention period; secrets are scrubbed before writing.
- The audit log is append-only and records every action: decision, actor (service or maintainer), approver, payload hash, external reference.
- Metrics on `/metrics` (Prometheus format), labeled by repository: jobs by state, agent latency and usage, forge rate limit, proposals waiting, agreement scores.

### 5.16 Forges

A forge is where repositories live: GitHub (github.com or GitHub Enterprise Server) and GitLab (gitlab.com or self-managed) now, others (Gitea and Forgejo, Bitbucket) later. One service serves repositories on several forges at once. Core code knows forges only through the ports below; nothing outside a forge's adapter package names one.

**Forges on the host.** `local.yaml` lists them (`forges`). Each has an id, a kind, its web and API addresses, and the CLI login the service acts as there. The first is the default forge. Without an `api_url`, a self-hosted forge's API is under its web address (`/api/v3` for GitHub Enterprise Server, `/api/v4` for GitLab), and each other forge's CLI login is in its own folder (`gh-<id>`, `glab/<id>`), never the default forge's `gh`. A host set up before forges keeps working: its `github` block reads as the default forge `github` (`local.yaml` version 3). Version 4 names the deployment's `owner` and drops `self_update.approve_permission` and `self_update.remote`; a version 3 file reads with no owner, so every admin of `code_repo` acts as owner until one takes ownership (2.3).

```yaml
forges:
  - id: github                          # the default forge
    kind: github
    web_url: https://github.com
    api_url: https://api.github.com
    cli_config_dir: ~/.config/auto-maintainer/gh
  - id: corp
    kind: gitlab
    web_url: https://gitlab.corp.com
    api_url: https://gitlab.corp.com/api/v4
    cli_config_dir: ~/.config/auto-maintainer/glab/corp
```

**Names.** A repository is named `<forge>:<path>`, where the path is the forge's own full project path: `owner/repo` on GitHub, `group/subgroup/project` on GitLab. On the default forge the prefix is left out, so every name from before forges stays as it was (`RLinf/RLinf`); elsewhere it is part of the name (`corp:infra/tools/builder`). People are named the same way, `alice` on the default forge and `corp:alice` elsewhere, because a login means something only on its own forge: alice on GitHub is not alice on a company GitLab. `domain/forges.py` holds the rules (`RepoRef`, `parse_repo`, `person`), and every check of a name goes through it. The memory repo keeps the default forge's repositories under `repos/<path>/` and the others under `forges/<forge>/<path>/`. The API names a repository as one path parameter (`/api/repos/{repo:path}/...`), and the dashboard's routes the same.

**One set of ports.** Every forge kind implements the same ports, `ForgeReader`, `ForgeWriter`, `CiProvider`, and `BranchPublisher`, plus a `ForgeKind` entry in `forges/registry.py` that describes the rest:

- `capabilities` (`domain/forges.py:ForgeCapabilities`): what the forge can do, such as draft changes, requested reviewers, a verdict that blocks a merge, CI that waits for a maintainer's approval, updating a branch (merge or rebase), and suggestion blocks. A playbook asks before it proposes what a forge cannot do, and the actuator refuses such an action.
- `terms`: the forge's own words for what the service posts and shows (a merge request on GitLab), handed to prompts and the dashboard; the domain keeps one vocabulary.
- `links`: web addresses of a repository, a change, a commit, a file, and a person, and a person's avatar.
- `cli`: the CLI the service acts through (`gh`, `glab`): how it is installed, how it signs in (a device code, or a token on standard input), how to read its token, and how git borrows it (a credential helper; never a token in a remote URL).
- `webhooks`: how a delivery is verified (HMAC on GitHub, a shared token on GitLab) and which repository and change it is about.
- `roles`: the forge's roles mapped to `Permission` (read, triage, write, maintain, admin), so the configuration gate and every permission check stay forge-neutral.
- `read_proxy`: the read-only door through which agents in their containers use the forge's CLI as the service, with the token outside (5.7).

`forges/router.py:ForgeRouter` implements each port by handing the call to the forge the repository is on, so services and playbooks call `reader.get_pr(repo, number)` as before and do not change when a forge kind is added.

**One vocabulary.** The domain keeps forge-neutral terms. A PR is any change request (a GitLab merge request, numbered by its `iid`). A review is a set of comments with a verdict (on GitLab, a submitted review or an approval, with diff discussions). Checks are CI results (GitLab pipeline jobs and commit statuses). Comments are notes.

| Concept | GitHub | GitLab |
|---|---|---|
| change request | pull request, `number` | merge request, `iid` |
| review | review with a state | approval or a request for changes, and diff discussions |
| inline comment | review comment (path, line, side) | diff discussion with a position (base, start, and head SHAs; old or new line) |
| suggestion | ` ```suggestion ` | ` ```suggestion:-0+0 ` |
| checks | check runs and commit statuses | pipeline jobs and commit statuses |
| rerun failed CI | rerun failed jobs | retry the pipeline's failed jobs |
| CI held for approval | a fork PR's workflow run | a pipeline for a fork's merge request, run by a maintainer |
| update a branch | merge the base into the head | rebase onto the target |
| draft | draft PR | draft merge request |
| reviewers | requested reviewers | reviewers |
| roles | read, triage, write, maintain, admin | Guest, Reporter, Developer, Maintainer, Owner |
| CODEOWNERS | `CODEOWNERS`, `.github/`, `docs/` | `CODEOWNERS`, `.gitlab/`, `docs/` |
| webhook check | HMAC (`X-Hub-Signature-256`) | shared token (`X-Gitlab-Token`) |
| CLI | `gh` | `glab` |

**People and sign-in.** Each forge has its own service account, the CLI login made on the setup page. People sign in on the forge their repositories are on, and a dashboard session belongs to one forge account: its login is forge-qualified as above, and a permission is checked only on that person's own forge. On GitHub, people authorize the GitHub CLI with a one-time code (5.13). On GitLab, `glab auth login --device` asks for a one-time code on the GitLab site (GitLab 17.9 or later); on an older self-managed instance, a person pastes a personal access token once instead. The service checks either through the forge's API and keeps the token under that person's forge-qualified login, for what they approve as themselves (5.9).

**Setting a forge up.** The main forge is the one the setup page sets up with the service's repository (7.3). In the dashboard, Settings → System → Forges lists the forges with the account the service acts as on each, and the owner (`service_gate`, as for every service setting) adds another there: kind, id, address, then the forge CLI's one-time code or a GitLab personal access token (`web/forge_setup.py`, `/api/settings/forges` and `.../poll`), into the service's own folder for that CLI (`~/.config/auto-maintainer/glab/<id>`, `gh-<id>`), never the host user's. The service restarts in place with the new forge and writes it to `local.yaml` only once that succeeds; a sign-in that ends without the forge kept is deleted. There is no command for it. When the host has no GitLab CLI, it is installed into the data folder from GitLab's own release, checked against the release's checksums (`glabcli.py`), as the GitHub CLI is (`ghcli.py`). A self-managed GitLab's device sign-in needs an OAuth application there, named by `oauth_client_id`; without one, people paste a personal access token on the sign-in page (`/auth/forges/<id>/token`), and with one they use the code (`/auth/forges/<id>/start`, `.../poll`). Webhooks arrive at `/webhooks/<id>`.

**What GitLab cannot do here yet.** Agents in their containers have no read-only door to GitLab (the GitHub CLI's proxy has no GitLab counterpart, since `glab` cannot be pointed at a Unix socket), so on a GitLab repository they work from what their prompt includes and the checkout (`prompts.no_code_rule`). GitLab's API offers no review verdict that blocks a merge, so a request for changes posts as a comment (`blocking_verdict`), and no pipeline held for a maintainer's approval (`held_ci`). Approvals carry no commit, so they count for the head they were read at. These are capabilities, not exceptions: playbooks ask before proposing.

**Adding a forge kind.** A package `forges/<kind>/` with its reader, writer, CI, publisher, webhooks, read proxy, and CLI; its `ForgeKind` in `forges/registry.py`; recorded API responses for tests (scrubbed of tokens and addresses); a live read-only test; and a column in the table above.

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

`~/.config/auto-maintainer/local.yaml` (port, data directory, proxy, operators, lab hosts, host paths, the HTTPS certificate), `gh/` (the GitHub CLI login the service acts as), `tls/` (the private authority and the server certificate for HTTPS, keys mode 0600 in a folder mode 0700; 4.7), `secrets.env` (mode 0600: the webhook secret, IM tokens), and `provider-keys.json` (mode 0600, written atomically: the API keys maintainers set on the Settings page; a key of the same name in `secrets.env` is used when the file has none). The data directory holds the SQLite database, run transcripts, mirrors, worktrees, release checkouts, and the one-time sign-in links (`sign-in-links.json`, mode 0600).

**Setup on the web.** Nobody edits these files by hand. When `am serve` starts without `local.yaml`, or cannot reach its forge with the one it has, it serves only the setup page and prints a link with a one-time code (`am setup` prints it again); holding the code proves access to the host. The page (`web/setup.py`, `hostsetup.HostSetup`) asks for:

1. The address browsers use.
2. The service's repository, by its link: the person's own fork or copy of auto-maintainer, prefilled from the checkout's `origin`. Its forge becomes the main forge, GitHub (github.com or Enterprise Server) or GitLab (gitlab.com or self-managed, with its OAuth application for the one-time code, else a personal access token); for another host the page asks which it is.
3. Signing in there, with the forge CLI's one-time code or a GitLab personal access token, as an admin of that repository, who becomes the deployment's owner. The service acts as the same account unless the owner signs another one in (an account made for the service) or, with the setup link, picks the CLI login already on the host. A login made on the page is kept in the service's own folder beside `local.yaml` (`gh/`, `glab/<id>/`), never the host user's; it is made in a staging folder and moved there only once its account checks out. A person who signed in but is not an admin of the repository, such as one who cloned the upstream code, makes their own copy from the page (a fork on the same forge, else an import, `ForgeAccount.copy`) and owns that.
4. Checks of the repository, so the service can update itself and change its own code later (5.12): the owner is an admin there, it holds the commit this host runs, and the service's account can push to it. Its default branch becomes `self_update.branch`.
5. The memory repository, created private if asked, which the service's account must be able to write.
6. The default agent, chosen among the agent CLIs found on the host (the first one found is preselected; CLIs that are not installed cannot be picked).

Anyone who reaches a new host's page may begin, but only an admin of the repository the checkout came from may claim it without the setup link, since anyone could fork the code; with the link, any repository of theirs. From then on only the claimer's browser continues. Finishing needs the owner signed in and every check passing; it writes the files, starts the service in the same process, and signs the owner in. Later, the owner changes the same things from Settings → Change host setup, and the service restarts in place on the new setup; if the new setup cannot start, the old one keeps running. A changed repository needs the owner to sign in again as an admin of it. The setup link from `am setup` also opens the page on a set-up host, where only an admin of the repository who signs in can save, and becomes the owner.

### 7.4 Importing prior state

A repository that already tracked priorities in another tool can bring them along:

- `am import evaluations <file> --repo <owner>/<repo>` loads PR evaluations from a JSON object keyed by PR number. Each entry has `scores` (dimension name to a number from 0 to 10) and optionally `summary`, `rationale`, `manual_boost`, `tier_override` with `override_reason` and `override_by`, and `evaluated_at`. Dimension names must match the repository's rubric; unknown ones are rejected.
- `am import rubric <file> --repo <owner>/<repo>` turns a rubric (dimensions, weights, thresholds, rule text, stage goals) into a memory PR. A rubric is configuration, so it applies only after a maintainer of the repository approves it.
- Imported records carry their source and import time in `evaluations` and the audit log. Earlier review comments are not imported; `comments.legacy_markers` lets the service recognize them in place.

## 8. Configuration

### 8.1 Service settings, host settings, and repository profiles

Three kinds of configuration, owned by different people:

- **Service settings** decide how the service runs everywhere: each agent's model and reasoning level (`agents.<name>.model`, `effort`, chosen on the Settings page among what its CLI offers, or among a provider's models and the levels each takes), and a faster model for its quick tasks on the service's account (`quick_model`: triage, priority and quick quality scores, CI classification, comment checks, translation, and hygiene, `agents/router.py:QUICK_KINDS`, when the route names no model; a person's own account keeps their choice), the default agent (`default_agent`: every task kind tries it first, ahead of its default route, except `verify`, which keeps looking for a second agent; unset, the one chosen during setup applies; routes in the settings or a profile still win), which agent CLIs it runs, with which models and, optionally, their own permission mode for every task, model providers with what is known of each model (`providers.<id>.models`: an id, or `{id, context_window, max_output, reasoning, interfaces, tools, images, streaming, tested_at}`, filled in by testing on the provider form, 5.7) and the names of their keys, budgets, the default route per task, system values (`poll_seconds`, `max_public_writes_per_hour`, `max_repo_public_writes_per_hour`, `agent_slots`: how many agent jobs run at once for people's requests, set on the Settings page and applied at once; unset, `pools.agent` in `local.yaml`), token limits per person (`token_limits`: a `default` for everyone and `people` by login, each `{daily_tokens, weekly_tokens, monthly_tokens}` over a rolling 24 hours, 7 days, and 30 days; a person's own entry replaces the default whole, `{}` is no limit, and with nothing set nobody is limited), and test machines (`labs`: transport, SSH destination, description, work folder, setup, the only repositories it is offered to, extra forbidden patterns, a time limit, GPU idle thresholds; 5.10). **Token accounting and limits.** Every agent run records who asked for it (`requested_by`: the person a playbook names, else whoever started its job on the dashboard or in a conversation, `Job.asked_by`, which `PlaybookContext.ask` passes on), whose account it ran on (`account`), its task kind, what the person was doing where the kind does not say (`activity`: `review_talk` for a review's discussion, `fix_talk` for a change's, `people_talk` for the People conversation), and its tokens: `input_tokens` is every token the model read, `cached_input_tokens` the part read from the provider's prompt cache (each adapter maps its CLI's usage so the totals compare across agents; before 2026-11-11 ACP runs left cache reads out), and `output_tokens` includes reasoning. A person's limit counts the tokens of the runs that worked for their requests (`TokenLedger.whose`, which also reads the job of a run recorded without `requested_by`) on the service's own agent accounts and model APIs (`engine/ledger.py:TokenLedger`, read from the store once, then only what was recorded since); their own accounts and the service's own work nobody asked for never count. The router checks it before each attempt on the service's account (`AgentRouter.person_limit`): past it, the service's agents are passed over without opening a breaker, the person's own agents still run, and the task ends with the limit as its error (`error_kind: limit`; a conversation answers 429 with it). A run that starts under the limit finishes. Settings → **Usage** (`GET /api/people/usage`, rows by task kind, agent, repository, and UTC day) shows each person's tokens as one bar on a shared scale split into groups of task kinds with fixed colors, with or without their own accounts, their limit and what they used of it, and on opening a person every task kind (fresh, cached, and output tokens, runs, cost), day, agent, and repository; the service's own work has a section of its own (totals, cache share, tokens by day, and ranked task kinds, repositories, and agents); the owner and operators see everyone and the service's own work, others themselves; the owner edits the limits there (`PUT /api/settings/token-limits`). Whether and how a repository uses a machine is in that repository's profile, not here. They live in one document, `config/service.yaml` in the memory repo, which lists only what differs from the defaults. Maintainers of the service's repository change them (2.3), on the Settings page or by asking the agent. A document that does not parse, names an agent or provider that does not exist, gives a provider to a CLI that cannot use it, or contains a credential is refused; one that reaches the memory repo some other way and does not check leaves the previous settings in force, and the Settings page says why. Keys are secrets named by the providers; their values stay on the host (7.3).
- **Host settings** describe the machine: `local.yaml` (address, port, data directory, the service's GitHub account, sign-in, the memory repository, operators, lab hosts, host paths, `code_repo`, the `default_agent` chosen during setup), `secrets.env`, maintenance schedules, and notification channels. The setup page writes them (7.3): on the first run whoever holds the code printed on the host, later maintainers of the service's repository.
- **Repository profiles** decide everything else, per repository: the layers of 2.2, merged over `config/defaults/` from the code repo and changed only through the configuration gate (2.3). A repository's routing overrides the service's default routes for that repository.

Every file has a schema version; the service refuses to load an unknown major version and keeps the previous valid one (4.10).


**Development instances.** `am serve --dev` (or `AM_ENV=dev`) talks to the real forge with two guards. With `sandbox_repo` set in `local.yaml`, the instance serves only that repository: `am repo add` and the dashboard refuse others, the poller and scheduler skip them, and the policy denies their actions, while the sandbox repository's actions follow its profile, so a test repository can be exercised for real. Without `sandbox_repo`, the instance serves every repository but holds every action that touches one at shadow.

**Standby hosts.** A host that runs beside another serving the same repositories from the same memory repo (a migration target before the cut-over) sets `standby: true` in `local.yaml`. Its policy then records at shadow every forge write its profiles would make on their own (`auto`, nobody approved it; `PolicyFacts.standby`, rule `standby records <kind>`), so only the other host reruns CI, labels, and comments on its own, and two hosts never race to edit the same comment. What a person asks for or approves on the standby host's dashboard or in its conversations still runs (a button's draft, a chat request, an approved proposal), and test machines' sessions run as on any host, since they write nothing to the repository. The dashboard shows a strip saying so, and Settings → System shows the mode. Dev mode, when also on, wins.
### 8.2 Profile schema outline

```yaml
schema: 1
repo: <owner>/<repo>
extends: [<pack>, ...]
scope:        {min_pr, base_branches, include_drafts, ignore_authors, issues}
people:       {codeowners, team, review_identities}
contribution: {commit_convention, title_rule, signoff, template_sections, license_header}
checks:       [{id, type: builtin | command | agent, blocking, ...}]
review:       {trigger: manual | scan | events, scan: {schedule, max_prs, followups}, comments: {check: scan | manual, schedule, max_prs, max_comments}, skill, template, recommendations, verify, cross_agent, max_inline_comments, suggest_labels, max_suggested_labels, writing_guide, quality_guide, quick_quality}
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
| `ci.approve_run` | `shadow` | `shadow` | only a person's Approve and run CI press creates one, run at once as them: it lets GitHub run a first-time contributor's workflows |
| `pr.request_reviewers` | `propose` | `auto` | from `CODEOWNERS`; a maintainer's assignment on the dashboard is drafted for them and runs at once |
| `pr.approve` | `shadow` | `shadow` | only a person's Approve press creates one, run at once as them; `off` removes the button's effect |
| `thread.resolve` | `shadow` | `shadow` | only a person's Resolve press on a review comment creates one (maintainers, the PR's owners, or the comment's author), run at once as them; the service's account never resolves a comment |
| `pr.update_branch`, `pr.merge` | `propose` | `auto` once armed | merge gate (5.5); a person's Merge now press runs at once as them, without the gate, unless `pr.merge` is `off` |
| `pr.edit` | `shadow` | `shadow` | only a person's Edit on a PR page creates one, run at once as them; `off` removes its effect. A `pr.fix` that gives the PR a new title or description sets it as part of the fix, under `pr.fix`'s level |
| `pr.nudge` | `propose` | `auto` | at most once per `timing.rereview_days` per PR |
| `pr.convert_to_draft`, `pr.close` | `propose` | `propose` | never automatic |
| `pr.fix` | `shadow` | `shadow` | only a person's request creates one, as a proposal for them; it runs only when they approve it as themselves |
| `lab.reproduce` | `shadow` | `shadow` | a person's request is always a proposal for them, run once they approve the plan |
| `lab.review` | `auto` | `auto`, or `auto` for team authors and `off` otherwise | the review's own session on a test machine the repository uses; on by default (the one exception, `ON_BY_DEFAULT`): it writes nothing to the repository and runs only on a machine its maintainers turned on |
| `issue.label` | `propose` | `auto` | |
| `issue.comment` | `propose` | `propose` | |
| `lab.run` | `auto` for team, `propose` otherwise | same | lab rules (5.10) |
| `memory.commit` (T0) | `auto` | `auto` | |
| `memory.change` (T1) | `auto` after notice | same | 24-hour window |
| `memory.change` (T2), `code.pr`, `code.update`, `code.merge`, `service.deploy` | `propose` | `propose` | a maintainer or the service's owner approves |
| `notify` | `auto` | `auto` | quiet hours |

### 8.4 Example runtimes and routing

```yaml
# memory repo: config/service.yaml (the owner of the deployment)
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

**Status of the first implementation.** Built and tested on fakes: everything in M0 and M1 except the replay evals, plus from M2 `ci_doctor`, `merge`, `hygiene`, CI gate management, reviewer requests from CODEOWNERS, `stale` nudges, `profile_drift`, and budgets; from M3 memory tiers with the memory gate, memory PRs on a remote memory repo, slash commands, chat with proposals (structured output instead of MCP tools), and the T0 journal. Checked live and read-only: GitHub sign-in, snapshots of a real repository's open PRs, and the five installed agent CLIs. Not built yet: the MCP server, the ACP transport, the learner beyond the journal, chat lessons, and lessons from rejections, compaction, cross-repository knowledge, digest, overlap detection, runner health, flaky test report, reproduction agent, cross-agent review, issue triage, release assistant, `am import rubric`, and replay evals. Built since: agents' questions on the dashboard over each CLI's own protocol (the ACP transport included), self-evolution with the supervisor (5.12), the dashboard layout of 5.13 with the agent on every page, the service settings with model providers (8.1), changed through the agent and the gate, and setup on the web (7.3) with sign-in links from the host; first-run setup with the GitHub CLI's account was run end to end on a real host. Providers are tested on fakes and by their command lines and environments; no CLI has yet run against a custom provider live. Also built, and tested on fakes and in the demo only: reviews of a selection with a chosen agent, one review per commit and agent, interactive requested reviews that follow the repository's skills, posting a draft or opening a fix PR as the approving maintainer, the `fix` playbook, review schedules set from Home, and the getting-started checklist. Also built and tested the same way: the live view of agents' jobs with stopping, the conversation about a review that revises its draft, drafts rendered as GitHub renders them with comments on the diff, test machines in the service settings, and reproductions on them. Also built and tested on fakes and in the demo: each repository's People record (2.6), filled in and checked by an agent. Also built: editing a posted review from its conversation (as the service, or as the maintainer who posted it), and messages to the agent while it writes a review, checked live against Antigravity's `session/cancel`. Also built and tested on fakes: GitLab as a forge beside GitHub, read live against gitlab.com (5.16); deployments owned by one admin of their own fork, set up on GitHub or GitLab from the setup page, with ownership handed on or moved to another fork from Settings (2.3, 7.3). Not built: cleanup of chat checkouts, messages to a running agent on Codex or the one-shot CLIs, a read proxy that lets agents use `glab`, and a GitLab-first setup tried against a live self-managed GitLab.

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
4. **pi on the hosts.** pi's adapter follows its documentation; a run against the installed CLI on a host is still to be done.
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
