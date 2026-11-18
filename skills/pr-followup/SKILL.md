---
name: pr-followup
description: Checks each earlier review finding against new commits and reports new problems introduced since the last reviewed commit. The second round of a review with the pr-review skill.
---

# Follow up on a reviewed pull request

1. For every earlier finding, read the code at the new head and decide: resolved (the problem is gone; name the commit that fixed it), partly addressed (say what remains), or still open. Keep its id (`F1`, `F2`, ...) so the service updates the right thread.
2. Review only what changed since the last reviewed commit for new problems, with the same severity scale as a first review, numbering them after the earlier ones.
3. For a PR that was already approved, look only for regressions the new commits introduce.
4. End with a report: the status of each earlier finding, the new findings, a summary of what changed, and the recommendation.
