---
name: ci-diagnose
description: Classifies a failed CI job as flaky, infrastructure, caused by the PR, or unknown, from its log and the PR diff. Used by the ci_doctor playbook.
---

# Diagnose a failed CI job

- `flaky`: transient failures unrelated to the change: network resets, timeouts reaching external services, rate limits, known nondeterministic tests.
- `infra`: the runner or CI system failed: lost runners, full disks, missing tools on the image.
- `pr_caused`: the change broke the build or a test. The explanation names the failing test and the likely cause in one or two sentences, written for the PR author.
- `unknown`: the log does not support a conclusion. Say so rather than guess.
- Suggest a `signature` (a regular expression on the log) only for flaky or infra failures that will recur, and keep it specific enough not to match real failures.
