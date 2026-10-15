# git-fleet

Policy-driven synchronization and reconciliation for a fleet of local Git repositories.

`git-fleet` grew out of workstation automation for keeping Git checkouts aligned
without throwing away local work. The reusable engine is independent of the
scheduler, GitHub account, dotfiles manager, and machine layout.

## Commands

```sh
git-fleet status
git-fleet fetch
git-fleet sync --dry-run
git-fleet sync --only owner/repository
git-fleet policy show
git-fleet verify-pr --repo /path/to/repo --pr 123 --checks /trusted/checks.json --output /tmp/receipt.json --trust-code
```

Configuration defaults to `${XDG_CONFIG_HOME:-~/.config}/git-fleet`; runtime state
uses `${XDG_STATE_HOME:-~/.local/state}/git-fleet`.

## Publication is not bypass authorization

`publish` defaults to `false` for **every** automation profile. An explicit
`publish=true` still cannot publish directly to a github.com default branch or
any protected branch. Unknown protection/access state defers publication rather
than trying the write with elevated credentials. Local/non-GitHub transports
retain the existing opt-in, non-force publication behavior; enterprise-host
protection discovery is not inferred from github.com policy.

Default-branch work must go through a task branch and PR. Neither successful
reconciliation nor an admin token nor a passing local test is merge authority.
`github-agent-router` owns merge evaluation and the native PR merge operation.

## Isolated verification and repair lineage

`verify-pr` leases one source PR, constructs its actual base-plus-head merge in a
dedicated worktree, runs an explicit trusted argv-based check plan, and emits a
revision-bound receipt. `--include-pr` adds explicitly selected repair PR heads to
the tested candidate. Failure, tracked-file mutation, conflicts, timeouts, or
head/base movement invalidate the receipt. Existing local work is preserved.

A worktree is **not a security sandbox**. `--trust-code` is required; run untrusted
code on disposable, credential-isolated workers. Test subprocesses receive an
allowlisted environment and temporary HOME, not provider tokens. Receipts are
HMAC-signed when `GIT_FLEET_EVIDENCE_KEY` is supplied by a trusted executor.
Unsigned receipts are informational, never merge approval.

`integrate-fix` can then integrate that exact tested repair tree into the original
`chatgpt/` PR branch using an ordinary push. It rejects stale/unsigned evidence,
protected targets, and changed trees. The new source head must be reverified and
recertified; a Jules-authored repair is not inherently safe.

Read [verification, repair integration and deployment boundaries](docs/VERIFICATION.md).

## Reconciliation safety

The engine does not use force-push or hard-reset as normal recovery mechanisms.
It creates recovery refs before divergent-history rebases, snapshots dirty work
when policy permits it, and honors active leases. Automatic root discovery ignores
linked worktrees unless explicitly registered or targeted. Start with a small
registry and `git-fleet sync --dry-run` before unattended reconciliation.

## License

MIT
