---
name: resolve-review-threads
description: Assess-before-fix loop for addressing review comments on a pull request, from Copilot, other bots, or humans. Use when asked to address, resolve, or handle review feedback, or when driving a PR's threads to resolved.
---

# Resolve Review Threads

Every finding gets verified before any code changes. Reviewers, bots
especially, are sometimes wrong, and applying an unverified prescription
trades one defect for another.

## Loop

1. **Inventory.** List the unresolved threads (commands below). Note which
   are from bots and which from people.
2. **Classify each finding before touching code.** Rate the evidence:
   - the reviewer said so: worthless on its own;
   - they pointed at a line: read it and the code around it;
   - reasoned through why the bad case can or cannot happen;
   - ran code that demonstrates it. This is the target for anything
     nontrivial. Reproduce the failure the finding claims, or record it
     explicitly as plausible but not reproduced. Never silently accept.
3. **Verdict per finding.** Real: fix at the root cause, not the prescribed
   patch when the prescription is shallow. Wrong: rebut in the thread with the
   evidence and leave the code alone.
4. **Look for the sibling.** A confirmed finding usually has relatives: the
   same pattern elsewhere, the same assumption in a design doc or ADR.
5. **Fix, test, commit.** Run `uv run pre-commit run --all-files` before
   pushing. One commit per concern, Conventional Commits subject, no
   generated-with footers or tool co-author trailers.
6. **Reply, then resolve.** The reply states evidence, not agreement:
   "Confirmed by running X, fixed in <sha>" or "Not reproducible: <what was
   run and observed>". Tone follows the `gh-voice` skill. Resolve every thread
   you addressed; leave open only active discussion or a question awaiting the
   reviewer.
7. **End state.** CI green, zero unresolved threads. A human approves and
   merges. Never merge, never enable auto-merge.

## Repo checks a finding can miss

While verifying, also check the invariants in the `code-review` skill. A
reviewer asking for a template change under `software/charts/` has usually not
asked for the `Chart.yaml` version bump that makes the change ship; add it.

## Bot rounds

Bot reviewers inflate nits once nothing critical is left. Track the round
count. From the third round, a judgment-call nit earns a reasoned reply and a
resolve rather than a code change, unless verification shows a real defect.

## Commands

```bash
# Review comments with ids
gh api repos/{owner}/{repo}/pulls/{pr}/comments \
  --jq '.[] | {id, path, line, body, user: .user.login}'

# Unresolved threads with node ids
gh api graphql -f query='
query {
  repository(owner: "{owner}", name: "{repo}") {
    pullRequest(number: {pr}) {
      reviewThreads(first: 50) {
        nodes {
          id
          isResolved
          comments(first: 1) { nodes { databaseId body } }
        }
      }
    }
  }
}' --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)'

# Reply to a comment
gh api --method POST repos/{owner}/{repo}/pulls/{pr}/comments/{comment_id}/replies \
  -f body="..."

# Resolve a thread
gh api graphql -f query='
mutation { resolveReviewThread(input: {threadId: "{thread_node_id}"}) { thread { isResolved } } }'
```
