---
title: Stage branch and release policy
status: current-governance
date: 2026-10-27
---

# Stage branch and release policy

`main` is the protected integration branch. Development uses short-lived `feat/*`, `fix/*`, `docs/*`, and `chore/*` branches. An upstream or dependency synchronization uses a separate `sync/*` branch and a pull request; it does not replace `main` directly. A `release/x.y` branch is created only when an older stable line must receive fixes.

Pull requests require at least one approval from a maintainer who did not author the change. A new push dismisses stale approvals, the most recent reviewable push needs approval from someone other than its pusher, all conversations must be resolved, and the branch must be up to date with `main` before merge. Squash merge is the enabled merge method; linear history is required. Force pushes and deletion of `main` are forbidden. Merged working branches are deleted automatically.

The repository maintainers for protected paths are `egoleecode`, `yry0008`, `icelee-asr`, and `even-ship-it`. The first account retains administrator permission; the other three have Write permission. GitHub uses the CODEOWNERS file on the PR's base branch: ownership enforcement becomes effective when the initial governance PR is reviewed and merged. Ordinary independent approval is already required. A bot or the pull-request author cannot substitute for an independent human approval, even when automation uses a maintainer's credentials. Coding agents may prepare commits and PRs; approval, merge and release require explicit human decisions.

The GitHub workflow's `quality`, `storage`, and `browser` jobs are required, with GitHub Actions as the expected check source and strict base freshness. They were verified on the first real [governance PR run](https://github.com/OrchestraCorp/Stage/actions/runs/40497749031). Changes to job names or trigger conditions must preserve an always-reported merge verdict; a workflow skipped by path filters must not leave a required check permanently pending. Local test results do not substitute for the current PR checks.

The first stable release is an explicit maintainer decision represented by a non-prerelease GitHub Release and an immutable semantic-version tag, normally `v1.0.0`. Alpha, beta and release-candidate tags, package versions and developer-preview milestones do not constitute that decision. Each product reaches this milestone independently. Before publishing that release, verify the actual branch rules, CODEOWNERS, current PR checks, release credentials and human release approval. Publishing a Release does not automatically configure protection. The release commit must already be on the protected branch; its migration head, dependency locks, wheel/source archives and SHA-256 manifest are frozen together as described in the [release freeze](../deployment/dev/release-freeze.md).

Release and hotfix changes follow the same pull-request rules. A maintained `release/x.y` line must receive equivalent protection before accepting fixes; each fix is backported or merged into `main` through a separate PR. Active tag rules prohibit updates and deletions of `v*` and `orchestra-upstream/*` tags, including by administrators. Tag creation is a separate release action, not proof of reviewed release readiness. Main protection applies to administrators and auto-merge is disabled. Any emergency change to protection requires an explicit human administrator decision, a recorded reason, restoration of the rules and independent follow-up review; automation must never disable protection to make its own push succeed.

This policy does not create a long-lived `develop` branch, a merge queue, or a multi-node release platform. Those are separate decisions that require operational evidence.
