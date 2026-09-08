---
name: resolve-review-threads
description: Address PR review feedback by verifying findings, fixing confirmed problems, replying with evidence, and resolving completed threads.
---

# Resolve Review Threads

Verify each finding before editing. A reviewer's assertion alone is
insufficient evidence. Respect the requested scope: assessment-only requests
do not authorize edits or GitHub changes; commit and push only when authorized.

## Verdicts

| Verdict | Evidence | Action |
|---|---|---|
| Confirmed | Observed behavior or code establishes the claim. | Fix the root cause and verify the result. |
| Disproved | Evidence shows the claim does not apply. | Reply with evidence; do not change code for that finding. |
| Inconclusive | Evidence does not establish or refute the claim. | State what remains unknown and leave open. |

Judge suggestions by evidence and impact, not reviewer type or round.
Preference-only suggestions can be declined with a brief reason; they are not
disproved defects.

## Loop

1. **Inventory.** Read unresolved threads and their full discussions using
   the paginated commands below.
2. **Assess.** Read the current code and apply the `code-review` skill,
   including governing ADRs. Reproduce nontrivial failures where possible;
   otherwise state the code evidence and what remains unverified. Failure
   to reproduce alone does not disprove a finding.
3. **Check related occurrences.** Look for the same failure in related code
   and documentation. Keep fixes within the confirmed issue's scope.
4. **Fix and verify.** Address the root cause, not merely the suggested
   patch. Run relevant checks and `uv run pre-commit run --all-files` before
   pushing. Follow repository commit conventions, with one commit per concern.
5. **Reply.** Use `gh-voice`: state the verdict and supporting evidence.
   Link the pushed fix when applicable; do not describe an unpushed edit as
   available to the reviewer.
6. **Resolve.** Resolve after a verified fix is pushed, a finding is
   disproved with evidence, or a preference-only suggestion is declined
   with a reason. Leave inconclusive findings, incomplete fixes, and active
   discussion open.
7. **Report state.** Refresh the paginated inventory and check CI on the
   latest pushed commit. Report remaining threads, pending CI, or failing
   checks rather than claiming completion. Leave approval and merge to a
   human; never enable auto-merge.

## Commands

Replace `{owner}`, `{repo}`, `{pr}`, and comment/thread IDs with the target
PR's values. Read full discussions from the REST inventory; the GraphQL query
fetches only each thread's root comment ID needed to reply.

```bash
# Review comments and replies across all pages
gh api --paginate repos/{owner}/{repo}/pulls/{pr}/comments \
  --jq '.[] | {id, in_reply_to_id, path, line, body, user: .user.login}'

# Unresolved threads across all pages
gh api graphql --paginate -f query='
query($endCursor: String) {
  repository(owner: "{owner}", name: "{repo}") {
    pullRequest(number: {pr}) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 1) { nodes { databaseId } }
        }
      }
    }
  }
}' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)'

# Reply to a comment
gh api --method POST repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies \
  -f body='...'

# Resolve a thread
gh api graphql -f query='
mutation { resolveReviewThread(input: {threadId: "{thread_node_id}"}) { thread { isResolved } } }'
```
