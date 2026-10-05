# Repository lifecycle contract

git-fleet owns synchronization of canonical checkouts. It does not make every
repository deploy the same way.

## Default execution model

Repository mutations belong in a dedicated task worktree and focused task
branch. The registered primary checkout remains on the remote default branch
and is reserved for fleet reconciliation, scheduled writers, and post-sync
hooks. Linked task worktrees are not generic fleet mutation targets.

Every task revision should receive deterministic verification before it is
considered ready for integration. A repository may opt into stronger
target-specific behavior with a tracked `.git-fleet/lifecycle.toml` profile:

```toml
version = 1

[lifecycle]
verify_on_commit = true
live_preview = false
deploy_on_default_update = false
rollback = false
repair_on_failure = false
requires_live_verification = false
```

These keys are a repository-owned capability declaration. The repository's own
hooks/adapter implement live preview, deployment, rollback, and repair. git-fleet
continues to call the repository-owned `.repo-sync/post-sync` hook after a
changed default-branch revision is reconciled.

## Evidence model

Evidence is revision-bound. Keep static/unit/integration, remote-agent, and
target-machine evidence separate. ChatGPT or Jules verification can satisfy an
independent review gate only for the exact revision it observed. It cannot
claim a local daemon, desktop app, hardware, accessibility permission, or other
machine-specific runtime was healthy unless that target actually produced the
evidence.

Repositories with `requires_live_verification = true` must therefore keep a
live-verification receipt distinct from remote CI/review evidence.

## Candidate and stable promotion

A live-capable repository should use candidate/stable promotion rather than
switching the canonical checkout:

1. verify the committed task revision;
2. materialize or otherwise identify an exact revision candidate;
3. atomically point the live runtime at that candidate;
4. run target health checks;
5. retain the candidate on success or restore the previous known-good runtime
   on failure;
6. after the source PR merges, repeat promotion for the exact merged default
   branch revision and mark that revision stable.

A closed or unavailable runtime is a deferred live check, not a successful one.

## Repair lineage

Automatic repair must not bypass the source task. For ChatGPT handoff PRs under
Jules verification, the original source PR remains the integration unit. Jules
may create an incremental repair PR against that source branch. Once the repair
is incorporated, any certification of the previous source head is stale and the
new source head must be verified again before the original PR may merge to the
default branch.

Local repair agents should work in isolated recovery worktrees rooted at the
failed revision. They may produce a focused local repair commit, but they must
not silently push, merge, deploy, or move the live runtime pointer.
