# Verification and controlled integration

## Trusted check plan

Store the plan outside the PR-controlled checkout. It is a JSON array of argument
arrays, not shell snippets. For git-fleet:

```json
[["bash", "tests/test_cli.sh"], ["python3", "-m", "unittest", "discover", "-s", "tests"], ["python3", "-W", "error", "-m", "compileall", "-q", "src", "tests"]]
```

Use a fresh, isolated environment for dependencies. Never share mutable
node_modules directories between worktrees. Honor the target repository's
AGENTS.md, device-validation requirements, and local/release policy.

```sh
git-fleet verify-pr --repo /repo --pr 12 --checks /trusted/checks.json --output /evidence/pr12.json --trust-code
```

The receipt records source head, current base, actual merge tree, plan digest,
check exit codes, timeouts, log digests, timestamps, and optional HMAC signature.
It does not embed test output, secrets, or claim production/device validation.
The trusted executor must control the evidence key and evidence destination.
Environment filtering alone does not secure arbitrary code running as the same
OS user. A linked worktree shares Git metadata; do not treat it as a sandbox.

## Jules repairs stay with the original PR

```sh
git-fleet verify-pr --repo /repo --pr 12 --include-pr 15 --checks /trusted/checks.json --output /evidence/combined.json --trust-code
git-fleet integrate-fix --repo /repo --pr 12 --repair-pr 15 --receipt /evidence/combined.json
git-fleet verify-pr --repo /repo --pr 12 --checks /trusted/checks.json --output /evidence/final.json --trust-code
```

A trusted `GIT_FLEET_EVIDENCE_KEY` is required for repair integration. Both PRs must
remain open, same-repository, and at the tested revisions. Only the original
`chatgpt/` branch may receive the ordinary fast-forward push. The final new head
invalidates old Jules certification even when the resulting tree is unchanged.
Multiple repair PRs may be specified in the same order in both commands.

Failed/uncertain integrations retain their worktrees. Explicitly reconcile stale
lease directories after confirming the recorded process is no longer running;
never silently steal a lease or delete an active worktree.

## Merge and promotion are distinct

Give the final signed receipt to `github-agent-router merge-evaluate`. Fleet never
merges to main, rewrites GitHub refs, changes rulesets, posts a fabricated passing
gate, or treats a completed Jules session as authorization. A verified tree is not
a production deployment: target-machine health checks, promotion, and rollback
remain the target's deployment adapter responsibility. A merge whose final tree
changes must be reverified before promotion.

## Optional disposable CI runners

The CI workflow accepts an administrator-set `CI_RUNNER_LABELS_JSON` repository
variable. Leave it unset for GitHub-hosted runners. Only point it at an isolated,
ephemeral runner group after real credentialed smoke tests; never use a normal
workstation with personal/cloud credentials. Fork PRs keep hosted runners. Setting
this variable does not install, provision, or secure a runner.

Do not replace the target's required GitHub Actions gate with a locally posted
success. Running the same workflow/check on an appropriately isolated self-hosted
runner preserves its native check identity. Registration and environment approval
are operator tasks, not inferred from passing unit tests.
