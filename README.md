# git-fleet

Policy-driven synchronization and reconciliation for a fleet of local Git repositories.

`git-fleet` grew out of a workstation automation tool for keeping many Git checkouts aligned with their remotes without throwing away local work. The standalone project is being extracted in stages so the reusable engine is independent of any particular dotfiles manager, scheduler, GitHub account, or machine layout.

## Goals

- inspect many local repositories without mutating them;
- fetch and report ahead/behind, dirty, branch, and upstream state;
- reconcile eligible repositories to their remote default branches;
- preserve dirty work and local commits before integration;
- keep recovery refs before history-changing reconciliation;
- apply global and per-repository automation policy;
- publish safely-ahead default-branch commits with ordinary non-force pushes;
- work as a normal CLI, from schedulers, or from event-driven automation.

## Status

Early extraction. The first milestone moves the tested fleet inspection, policy, and reconciliation core out of its original dotfiles repository. GitHub PR integration and platform-specific scheduling remain separate until the core boundary is stable.

## CLI

```sh
git-fleet status
git-fleet fetch
git-fleet sync --dry-run
git-fleet sync --only owner/repository
git-fleet policy show
```

Configuration defaults to `${XDG_CONFIG_HOME:-~/.config}/git-fleet` and runtime state defaults to `${XDG_STATE_HOME:-~/.local/state}/git-fleet`.

## Safety model

The reconciliation engine does not use force-push or hard-reset as normal recovery mechanisms. Before rebasing divergent local history it creates a recovery ref, and dirty work is snapshotted before worktree mutation when policy allows it.

Start with `git-fleet sync --dry-run` and a small registry before enabling unattended reconciliation.

## License

MIT
