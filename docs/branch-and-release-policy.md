---
title: Stage branch and release policy
status: current-governance
date: 2026-10-27
---

# Stage branch and release policy

`main` is the protected integration branch. Development uses short-lived `feat/*`, `fix/*`, `docs/*`, and `chore/*` branches. An upstream or dependency synchronization uses a separate `sync/*` branch and a pull request; it does not replace `main` directly. A `release/x.y` branch is created only when an older stable line must receive fixes.

Pull requests require at least one approval from a maintainer who did not author the change. A new push dismisses stale approvals, the latest commit must be reviewed, all conversations must be resolved, and the branch must be up to date with `main` before merge. Squash merge is the default; merge commits are reserved for a documented history-preservation need. Force pushes and deletion of `main` are forbidden.

The repository maintainers for protected paths are `yry0008`, `icelee-asr`, and `even-ship-it`. Their write permission and the `.github/CODEOWNERS` file are prerequisites for GitHub to enforce ownership. A bot or the pull-request author cannot substitute for an independent human approval.

The current GitHub workflow has three useful check jobs: `quality`, `storage`, and `browser`. Before the first stable release, these jobs must run successfully on a real pull request at least once. Only then should their exact reported check names be added as required status checks. A local test result or a workflow file that has never produced a check run is not sufficient evidence for a required rule.

The first stable release is an explicit maintainer decision represented by a GitHub Release and an immutable `v1.0.0`-style tag. The current package version and developer-preview milestones do not constitute a stable release. Before publishing that release, branch protection must require the verified checks, independent approval, stale-review dismissal, conversation resolution, and the latest-push review. The release commit, migration head, dependency locks, wheel/source archives and SHA-256 manifest are frozen together as described in the [release freeze](../deployment/dev/release-freeze.md).

Release and hotfix changes follow the same pull-request rules. A hotfix for a maintained `release/x.y` line is merged back to `main`; tags never move or get deleted. Administrators do not routinely bypass these rules. An emergency bypass requires a recorded reason, a second maintainer review afterwards, and a follow-up issue before the next release.

This policy does not create a long-lived `develop` branch, a merge queue, or a multi-node release platform. Those are separate decisions that require operational evidence.
