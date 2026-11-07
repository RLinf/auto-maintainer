# AGENTS.md

This file tells coding agents how to build, test, and change auto-maintainer. `CLAUDE.md` is a symlink to it. The design is in [docs/design.md](docs/design.md): read section 2 (repo profiles and who may change them) and section 4 (system structure) before adding a module, and the section for the module you touch before changing it. Section 11 says what is built and what is not. When this file disagrees with the code, the code is right; fix this file in the same change.

auto-maintainer is a service that helps maintainers run GitHub repositories. It polls repositories for PRs and CI; runs playbooks (triage, review and follow-up on request or on a periodic scan, priority evaluation, calibration, hygiene, CI diagnosis, merge, slash commands, onboarding, drift, stale, lab tests, journal) that call coding agents (Claude Code, Codex, Antigravity, OpenCode, Gemini CLI; pi and dsh through the generic adapter) for judgment; and turns the results into actions that a policy engine executes, proposes to a maintainer, or only records. Each repository is described by a profile that only its maintainers can change. Maintainers use a web dashboard to approve proposals, chat with the service, and configure it. Profiles and learned knowledge live in a private memory repo.

## Working with a human submitter

- A human submits and answers for every change. Push, open or edit PRs and issues, and post comments only when the user asks for that action and has seen the content.
- Never point a development instance at a real repository with any action above `shadow`. Use `am serve --dev` (or `AM_ENV=dev`), which caps every repository action at shadow unless `sandbox_repo` in `local.yaml` names a test repository (then it serves only that one, for real), or `am serve --demo`, which runs on fakes. Only the demo offers the passwordless `/auth/dev` sign-in.
- When a requirement is ambiguous or a constraint blocks the change, stop and ask. Do not leave `TODO(agent)` placeholders; record known limitations in the PR description.
- Report results as they are: which tests ran, against which CLIs and versions, and which failed or were skipped. State in the PR description that an AI assistant was used.

## Setup and commands

Python 3.11 or newer through uv; Node 20 or newer for the dashboard.

```bash
uv sync                                   # create .venv with dev dependencies
uv run ruff check . && uv run ruff format --check .
uv run pytest                             # unit and flow tests: no network, no agent CLIs
uv run pytest -m live_agent               # adapter contract tests against installed CLIs (costs money)
uv run am serve                           # first start: prints a link to the setup page, which writes the config
uv run am setup                           # print that link again; AM_CONFIG_DIR/AM_DATA_DIR point a trial at empty folders
uv run am login                           # one-time sign-in link for the GitHub account logged in on the host
uv run am doctor [--live]                 # probe forge auth, memory repo, agent CLIs, labs
uv run am serve --demo                    # the whole service on fakes with sample data, on :7777
uv run am start [serve options]           # production, in the background: am supervise with a rotated log
uv run am logs -f | am status | am stop   # follow the log, check, stop the background service
uv run am supervise [serve options]       # the same in the foreground: restarts after a deploy, rolls back a failed one
uv run am serve --dev                     # real forge: only sandbox_repo, or every repository at shadow
uv run am repo add <owner>/<repo>         # start onboarding (needs your gh identity to pass the gate)
uv run am repo rename <old> <new>         # fix a repository's letter case to GitHub's (stop the service first)
uv run am profile show|doctor <owner>/<repo>
uv run am import evaluations <file> --repo <owner>/<repo>
cd ui && npm ci && npm run dev            # dashboard dev server against am serve --demo
```

Pre-commit runs Ruff and the commit-message checks:

```bash
pre-commit install --hook-type commit-msg
pre-commit run --all-files
```

## Repository map

- `src/auto_maintainer/domain/` – types (`RepoProfile`, `Event`, `Action`, `Proposal`, `PrSnapshot`, `Visibility`, `Rubric`, `MemoryChange`, `ServiceConfig`), pure rules (review state, scoring, merge gate, markers, CODEOWNERS, calendar), and ports (`ForgeReader`, `ForgeWriter`, `CiProvider`, `AgentRuntime`, `AgentRunner`, `MemoryStore`, `Workspace`, `Sandbox`, `LabHost`, `Store`, `SecretStore`, `Notifier`, `EventSink`). No I/O.
- `src/auto_maintainer/service.py` – application wiring over a set of adapters (`Adapters`, `ServiceSettings`, `Service`); imports no adapter.
- `src/auto_maintainer/app.py` – composition root that builds the real adapters; with `cli.py`, the only code that imports adapters.
- `profiles/` – layer merging, packs, locks, the configuration gate (`ConfigGate`), knowledge reads, hygiene checks, the profile doctor.
- `policy/` – `decide()` over `PolicyFacts`, hard limits, the leak guard.
- `playbooks/` – one module per playbook, `base.py` (contract and context), `prompts.py` (`untrusted()` and prompt blocks).
- `actuator/` – `pipeline.py` (policy, proposals, approvals), `executor.py` (per-repository queues, intents, staleness), `handlers.py` (one per action kind), `memory_gate.py`.
- `engine/` – event bus, jobs and waits, poller, scheduler, the question desk (`questions.py`) where agents' questions wait for reviewers, and each job's live agent steps (`progress.py`).
- Adapters: `github/`, `agents/` (`exec.py` with two-way `Conversation`s, `acp.py`, `approver.py`, `adapters/`, `router.py`), `memory/`, `workspace/` (worktrees, and `publish` for the actuator), `labs/`, `store/` (SQLite, and `secrets.py` for API keys), `notify/`, `selfupdate/` (`updater.py` deploys the service's own checkout, `supervise.py`), plus `subproc.py`.
- `hostsetup.py` – the steps of the setup page (the GitHub CLI login the service acts as, memory repo), which write `local.yaml`; `config.py` reads and writes it. `ghcli.py` installs the GitHub CLI into the data folder when the host has none.
- `web/` – FastAPI app, sign-in (the GitHub CLI's one-time code, and one-time links), the dashboard API, chat, `jobs.py` (agents' work, live), `settings.py` (the service settings and test machines), `setup.py` (the setup page). `testing/` – fakes of every port, `Harness`, the demo data.
- `packs/<name>/pack.yaml` – built-in packs. `skills/<name>/SKILL.md` – skills the service gives its runtime agents. `config/defaults/` – default profile (every repository action in `shadow`), routing, schedules.
- `ui/` – React, TypeScript, Vite dashboard; `npm run build` writes into `src/auto_maintainer/web/static/`, which is not in git. `am serve` runs that build at startup whenever it is missing or older than `ui/` (`web/dashboard.py`), so Node 20+ is needed wherever the service runs from a checkout.
- `tests/` – flows, rules, adapters (`tests/github/`, `tests/agents/`), and `test_layers.py`.

## Architecture rules

These hold everywhere. Reviewers reject changes that break them; `tests/test_layers.py` enforces the first four.

- **Repositories are data.** Core code never names a repository, label, workflow, path, or person of a target repository; those come from its profile.
- **Imports follow the layers** (design 4.4 and 4.9): `domain` imports nothing; adapters import only `domain` and `subproc`; `policy` and `profiles` import only `domain`; `playbooks` never import `actuator` or an adapter; `engine` may import `playbooks` and `actuator`; only `app.py` and `cli.py` import adapters.
- **Visibility is typed.** Public text is `PublicText`, built only by `policy.leak_guard`, the only module besides `domain/visibility.py` that touches `GUARD_KEY`. Scores, tiers, rubric content, internal entities, and anything matching `domain/secrets.py` never reach a public action.
- **Defaults keep repository actions in shadow.** `config/defaults/profile.yaml` sets every action that touches a repository to `shadow`; only maintainers raise it.
- **Configuration belongs to the repository's maintainers.** Every change to a profile layer or lock goes through `ConfigGate.check`, which reads the user's live permission against `access.configure` (`maintain` by default). The service acts only on approved locks and cannot approve its own changes: pass `person=True` to the gate only where a person signed in to the dashboard or runs the CLI, never for a login the forge reports. Never add a way around the gate, for operators or for the service itself (design 2.3).
- **The service settings belong to the maintainers of the service's repository.** Agents, model providers, default routes, and system values live in `config/service.yaml` (`domain/settings.py`); every change goes through `ConfigGate.check_service`, live against `code_repo`. API keys go only into the `SecretStore` and the agent's environment: never into the memory repo, a command line, a prompt, a transcript, or an API response. The host setup (`web/setup.py`) needs the same gate once the host is set up; before that, only the one-time code printed on the host unlocks it.
- **Agents propose, the core acts.** Only `actuator/` writes to the forge, runs lab commands, or pushes (to the memory and code repos, and a fix branch as the maintainer who approved it). Playbooks return `Action`s and never receive a `ForgeWriter`. Agents never get write credentials; their environment hides the host's `gh` and git credentials (`agents/exec.py:isolation_env`).
- **Agents run in auto mode; reviewers answer what they ask** (design 5.7). Every route defaults to `permissions: auto`: the CLI's own auto mode (or `agents/approver.py` for ACP CLIs), with what it would ask a person sent through `ExecRuntime.resolve` to the question desk; a read-only task nobody asked for (`requested_by` unset) is denied instead, since nobody waits on it. A new adapter for a CLI that can ask implements `conversation()`; one that cannot is one-shot and never stalls on a prompt. Bypass applies only when it is asked for and allowed (the route asks and the agent sets `allow_bypass: true`, or the agent's own `permissions: bypass` in the service settings, which is both), and the input is trusted or the route sets `bypass_untrusted: true` (`effective_permissions`). An agent's own `permissions` and `effort` win over the route's. Playbooks set `ctx.trusted_input` once they know the author.
- **Playbooks never block on the outside world.** A playbook that needs CI, a rerun, or an approval first returns `Wait(...)` or `retry_after`, and every run starts from fresh reads (design 4.3).
- **Every action has a deterministic idempotency key** (`Action.idempotency_key`: kind, repository, number, head SHA, discriminator). Submitting an action twice must produce one external write.
- **Hard limits stay hard.** No approvals; no commits or pushes to target repositories by the service's own account; edits only to the service's own comments (found by marker); one review per head SHA; mentions only of the PR author and `CODEOWNERS`. Do not add config that loosens them.
- **Acting as a person is explicit.** A maintainer may approve `review.post` or `pr.fix` as themselves (`ActionRecord.post_as`); the executor then writes with the token they signed in with (`UserTokens`), and `pr.fix` runs no other way, so the service never pushes an agent's change under its own name. Never use a person's token for anything they did not approve, and never accept `post_as` for a login other than the approver's.
- **Untrusted text is data.** PR content, comments, issues, and CI logs go into prompts through `playbooks.prompts.untrusted()`. A value from agent output that names a target (label, reviewer, path, template) is checked against the profile before it becomes an action. Instructions, skills, and scripts from a target repository come from its default branch (`workspace/git.py` restores them).
- **Agent output is validated** against a Pydantic model defined next to the playbook that asks for it; the router uses the runtime's schema flag when it has one and one repair turn otherwise.
- **Secrets never leave the host config.** No tokens in git remote URLs, command lines, prompts, logs, or transcripts; git reads them from `AM_GIT_TOKEN` through a credential helper. Agent environments come from the allowlist in `agents/sandbox.py`.
- **Async all the way.** No blocking calls on the event loop; adapters run subprocesses through `subproc.run`. Times are UTC; business days go through `domain.calendar`.
- **Labs follow their guard rules** (design 5.10): template commands, or a reproduction script that passes `domain/labs.py:script_refusals` and that its requester approved or, for a review, that the repository's `lab.reproduce` rule runs on its own; one worktree or run folder per run; lease only idle GPUs; never modify the base clone; no credentials on a machine (code goes over as archives); cleanup in `finally`.
- **Schemas are versioned.** A table change needs an Alembic migration in `store/migrations/versions/`; a profile or lock schema change needs a version bump and a loader for the previous version.

## The service changing itself

- A reviewer's `improve` request in chat becomes a request card; once a maintainer of `code_repo` approves it, `playbooks/self_improve.py` runs: an agent edits a worktree of `code_repo`, the playbook runs the tests and Ruff, and the actuator pushes `am/self/...` and opens a PR (design 5.12). Never start an agent on the service's own code without that approval. Write that agent's instructions here, in this file: it reads `AGENTS.md` first.
- Merging (`code.merge`) and deploying (`service.deploy`) need `self_update.approve_permission` (`maintain` by default) on `code_repo`, checked live in `web/api.py:code_maintainer`. Never add a path that merges or deploys without that check, for operators, for chat, or for the service itself.
- A deploy only moves the service's checkout to a commit on the default branch, after that commit's tests pass in a staging worktree; `am supervise` rolls back a deploy whose code fails to start.

## Extending

| Add | Where | Also |
|---|---|---|
| Agent runtime | `agents/adapters/<name>.py` subclassing `ExecRuntime`, registered in `agents/registry.py` | Truthful `Capabilities`; a `Conversation` when the CLI can ask, tested with the `talking_cli` fixture; argv tests per permission mode, a `-m live_agent` test, the adapter table in design 5.7. If the CLI can use a custom endpoint, `provider_env` (and argv) plus its protocols in `PROVIDER_PROTOCOLS`. Verify every flag against `--help` of the installed version. |
| Playbook | `playbooks/<name>.py`, listed in `playbooks/__init__.py` | Triggers, `key()`, output models, the actions it emits, a flow test in `tests/test_flows.py` or a focused test. |
| Action kind | `ActionKind` and a payload model in `domain/actions.py`, a handler in `actuator/handlers.py` | `shadow` in `config/defaults/profile.yaml`, a recommended level in design 8.3, hard limits, public text fields in `PUBLIC_TEXT_FIELDS`, idempotency and staleness tests. |
| Pack | `packs/<name>/pack.yaml` | Only keys a pack may set (`profiles/layers.py`); a test in `tests/test_profiles.py`. |
| Built-in check | a function in `profiles/checks.py`, listed in `BUILTIN` | Passing and failing cases in `tests/test_runtime.py`. |
| Profile field | `domain/profile.py` | Default, merge rule (additive or not), doctor check, design 8.2. |
| Service setting | a field in `ServiceConfig` (`domain/settings.py`), applied in `Service._apply_settings` | A check in `check_settings`, its row on the Settings page, the agent's outline in `web/chat.py:_settings_block`, design 8.1. |
| Dashboard page | `ui/src/pages/` and `web/api.py` | The permission it requires (view needs write; configuration needs the gate) and whether it shows internal data. |
| Notification channel | `notify/` | Config in the memory repo's `config/channels.yaml`. |

## Tests

- Unit and flow tests use `testing.fakes` (`FakeForge`, `FakeMemory`, `ScriptedRunner`, `FakeWorkspace`, `FakeLabHost`) and `testing.harness.Harness`, which builds the whole service. They make no network calls and start no agent CLIs.
- Test through the contract a caller uses: a playbook through events and the fake forge's recorded writes, policy through decision tables, the gate through every source of change.
- A flow that writes to the forge must first raise the action kind above shadow in the test profile; the default profile writes nothing.
- Fixtures recorded from real repositories are scrubbed of tokens and email addresses.
- Delete a test when you cannot name the change that would make it fail.

## Writing

- Text the service posts uses the repository's public language and the style rules in its profile. Code never hardcodes a language or wording outside `skills/`, `config/defaults/`, and prompt helpers.
- Docs and comments use plain, specific sentences that say what exists and why. Name the symbol you mean, and leave out filler such as "seamlessly", "robust", "powerful", and "leverage".
- Comments and docstrings state the contract, invariants, ownership, and any non-obvious reason. They do not narrate the implementation or record the history of a fix.
- Every Python file starts with the Apache-2.0 header `# Copyright 2026 The RLinf Authors.` and the license notice (Ruff `CPY001`). Public APIs have docstrings and type hints.

## Commits and PRs

- Commit subjects follow Conventional Commits (`<type>(<scope>): <description>`, imperative, about 72 characters). Every commit a person makes has a `Signed-off-by:` trailer from that person (`git commit -s`), never an agent's identity. Commits the service makes on its own `am/self/*` branches carry `Requested-by:` instead and no sign-off; the maintainer who approves the merge answers for them, and the audit log records who that was (design 5.12).
- PR titles use the same form; the description after `: ` is at most 50 characters, starts with a lowercase letter, has no trailing period, and is plain ASCII.
- The PR description lists the commands you ran and their results.

## Maintaining agent instructions

- This file is loaded on every agent request. Keep it under 200 lines, and add a rule only when it applies across the repository and an agent would get it wrong without being told.
- Put guidance for one area in that area's design section, in a skill, or in an `AGENTS.md` inside the directory with a `CLAUDE.md` symlink beside it.
- Skills for agents developing this repository go in `.agents/skills/<name>/SKILL.md` with a symlink in `.claude/skills/`. Skills the service hands to its runtime agents go in `skills/`.
- When a new rule supersedes an old one, remove the old one in the same change.
