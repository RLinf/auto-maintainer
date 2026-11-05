---
name: pr-priority
description: Scores a pull request on the repository's rubric dimensions for the maintainers' private review queue, and calibrates the rubric from maintainers' overrides. Used by the evaluate and calibrate playbooks.
---

# Score and calibrate priorities

Scores are internal. They never appear in public text.

- Score each dimension from 0 to 10 using only that dimension's rule from the prompt. Use the whole range: 5 is an ordinary PR, 9 to 10 is rare.
- The rationale names the evidence for each score in one short clause.
- When calibrating an override, never turn a one-off reason into a rule. Prefer a time-boxed stage goal over a rubric change, and ask a question when the reason is vague or contradicts recent overrides.
