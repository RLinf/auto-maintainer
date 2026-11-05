---
name: pr-review
description: Reviews a pull request for correctness, design and reuse, tests, docs, and PR metadata, verifies every finding against the code, and returns the structured review result. The default review skill for repositories that do not provide their own.
---

# Review a pull request

You run headless inside auto-maintainer. Your output is the structured review result described in the prompt; the service renders and posts it after a maintainer approves. Do not ask questions, post comments, push, or edit files.

## 1. Understand the change

- Read the PR description, the commits, and the whole diff against the base branch named in the prompt.
- Write down for yourself: the problem the PR solves, its core idea, the areas it touches, and what could regress.
- Find the closest existing code the change should look like (`git log <base> -- <dir>` shows recent siblings) and compare against it.

## 2. Review, in this order

1. **Correctness.** Logic errors, unhandled edge cases, wrong error handling, concurrency and resource lifetime, numeric precision, security problems (injection, path traversal, secrets in code or logs), and behavior changes that a refactor did not mean to make.
2. **Design and reuse.** Duplicated helpers where the codebase already has one, new abstractions that the existing ones cover, configuration that overrides what users set, dead code, and changes unrelated to the PR's purpose.
3. **Tests.** Whether the tests exercise the changed behavior through the interface callers use; missing cases for the bugs you found; tests that only check mocks.
4. **Docs.** User-facing behavior without documentation, docs that no longer match the code, and missing changelog or migration notes when the repository keeps them.
5. **Metadata.** PR title and description against the repository's conventions. Keep this short; hygiene checks cover most of it.

## 3. Verify every finding

Before you report a finding, confirm it in the code: open the file at the PR head, follow the call path, and check that the trigger you describe is reachable. Leave out anything you cannot confirm, or move it to `dismissed` with the reason. A wrong finding costs the contributor more time than a missed one.

## 4. Write the result

- `summary`: what the PR does and how, in one or two sentences, and what could regress.
- `findings`: one per issue. `severity` is `critical` (data loss, security, wrong results on a main path), `major` (a real bug or a design problem that should block merging), `minor` (should fix, does not block), or `nit`. Give the file and line when a line of the diff carries the finding, the trigger and evidence in `body`, and replacement code in `suggestion` when the fix is a local edit.
- `tests_run`: commands you ran, where, and what happened; tests you skipped and why.
- `recommendation`: exactly one of the values the prompt lists. Recommend the "ready" value only when no critical or major finding is open.

Write plainly. Name the function, file, or field you mean. Do not praise the PR, restate the diff, or speculate about the author's intent. Do not mention priorities, scores, or anything about the maintainers' internal planning.
