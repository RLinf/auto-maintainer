---
name: pr-followup
description: Checks each earlier review finding against new commits and reports new problems introduced since the last reviewed commit. Used by the followup playbook.
---

# Follow up on a reviewed pull request

1. For every earlier finding, read the code at the new head and decide: `resolved` (the problem is gone; give the fixing commit in `fixed_in`), `partial` (some of it remains; say what in `note`), or `open`.
2. Review only the incremental diff for new problems, and report them as `new_findings` with the same severity scale as a first review.
3. For a PR that was already approved, look only for regressions the new commits introduce.
4. Keep earlier finding IDs (`F1`, `F2`, ...) so the service can update the right threads.
