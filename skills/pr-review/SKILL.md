---
name: pr-review
description: Reviews a pull request for correctness, design and reuse, tests, docs, and PR metadata, verifies every finding against the code, and ends with a review report. The default review skill for repositories that do not provide their own.
---

# Review a pull request

You review inside auto-maintainer, which posts your review after a maintainer approves it. Do not post comments, push, or edit files.

On a second round, start from the earlier findings: for each, say whether the new commits resolved it (and which commit), partly addressed it, or left it open, keeping its id; then review only what changed for new problems. For a PR that was approved before, look only for regressions the new commits bring.

## 1. Understand the change

- Read the PR description, the commits, and the whole diff against the base branch named in the prompt.
- Write down for yourself: the problem the PR solves, its core idea, the areas it touches, and what could regress.
- Find the closest existing code the change should look like (`git log <base> -- <dir>` shows recent siblings) and compare against it.

## 2. Reproduce

A PR that adds or changes a user-facing workflow (a feature with docs, a model, an example, a command) or that claims a result needs a reproduction: a run that follows its documentation the way a user would.

- **You do not reproduce it yourself**: you have read the implementation. With a test machine, you run there what settles your findings and checks the tests (the PR's tests, a focused script that shows what you suspect). When the PR needs a reproduction, start the reproduction agent as soon as you know, as your instructions say (`lab --reproduce` with its brief: the PR, its head, the doc pages to follow, and the hardware it may use); it runs while you review, and you finish the review with its record.
- **The reproduction agent** works from the PR and its documentation alone, in a fresh session on the machine: it follows the docs and the description step by step, as a user would, without reading the implementation. When a step cannot run as written (no robot or camera, fewer GPUs than the docs use, missing checkpoints or data, hours of training), it does not stop there: it runs what can stand in for it (the project's own tests with mocked hardware, a simulation, a smaller config, fewer GPUs or steps, a public checkpoint), and records each such change as an intervention. It records the settings (commit, hardware, environment, config, commands), the first failure, every intervention, and the functional and the metric result separately, and concludes reproduced, not reproduced, or inconclusive.
- Without a test machine, say that no reproduction ran and what one would need.

## 3. Review, in this order

1. **Correctness.** Logic errors, unhandled edge cases, wrong error handling, concurrency and resource lifetime, numeric precision, security problems (injection, path traversal, secrets in code or logs), and behavior changes that a refactor did not mean to make.
2. **Design and reuse.** Duplicated helpers where the codebase already has one, new abstractions that the existing ones cover, configuration that overrides what users set, dead code, and changes unrelated to the PR's purpose.
3. **Tests.** Whether the tests exercise the changed behavior through the interface callers use; missing cases for the bugs you found; tests that only check mocks.
4. **Docs.** User-facing behavior without documentation, docs that no longer match the code, and missing changelog or migration notes when the repository keeps them.
5. **Metadata.** PR title and description against the repository's conventions. Keep this short; hygiene checks cover most of it.

## 4. Verify every finding

Before you report a finding, confirm it in the code: open the file at the PR head, follow the call path, and check that the trigger you describe is reachable. Leave out anything you cannot confirm, or list it as dismissed with the reason. A wrong finding costs the contributor more time than a missed one.

## 5. Report

End with the report, in this order:

- **Summary**: what the PR does and how, in one or two sentences, and what could regress.
- **Findings**: one per issue, numbered `F1`, `F2`, ... Each has a severity (`critical`: data loss, security, wrong results on a main path; `major`: a real bug or a design problem that should block merging; `minor`: should fix, does not block; `nit`), a category, the file and line when a line of the diff carries it, the comment as it is to be posted (the trigger and the evidence), and replacement code when the fix is a local edit.
- **Dismissed**: candidates you dropped, one line each on why.
- **Tests run**: what ran, where, and what happened; what you skipped and why.
- **Recommendation**: ready only when no critical or major finding is open; changes requested when the author can fix what is open; blocked when it needs more than the author's fixes.

Write plainly. Name the function, file, or field you mean. Do not praise the PR, restate the diff, or speculate about the author's intent. Do not mention priorities, scores, or anything about the maintainers' internal planning.
