---
name: repo-onboard
description: Drafts a repository profile from the repository's contribution guide, templates, workflows, CODEOWNERS, labels, and recent merged PRs. Used by the onboard playbook.
---

# Draft a repository profile

- Prefer packs over spelling settings out: a pack exists for Conventional Commits, DCO sign-off, CODEOWNERS, GitHub Actions, paths-filter coverage, Python license headers, bilingual docs, and shared GPU labs.
- Map the repository's own labels to roles (`needs_review`, `changes_requested`, `ci_gate`, `reproduced`, `needs_docs`, `community`, `automerge`). Leave a role out when no label fits; never invent labels.
- Point `review.skill` at the repository's own review skill (`repo:<path>`) when it has one.
- Do not set `policy`: every action starts in shadow until the maintainers raise it.
- List what you could not decide in `questions`. A maintainer of the repository approves the profile before anything runs.
