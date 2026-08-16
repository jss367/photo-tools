# PR Agent Routine

This document describes how to run the Vireo PR fix agent as a Claude Code
routine instead of doing LLM work directly in GitHub Actions.

The motivation is cost: `claude-code-action` bills against the Anthropic **API**
balance, while routines bill against the Claude Code **subscription**
(Pro/Max/Team). If your API wallet is empty but your Code plan has headroom,
routines keep the agent running.

## Architecture

```
┌──────────────────────┐  /claude-fix, reviews, CI failures, push-to-main
│  GitHub              │──────────────────────────────────────────────┐
└──────────────────────┘                                              │
                                                                      ▼
┌──────────────────────┐                   ┌──────────────────────────────────┐
│  .github/workflows/  │  POST /fire       │  Claude Code routine             │
│  pr-agent.yml        │──────────────────▶│  (cloud session, clones repo,    │
│  (slim forwarder +   │  with text: "..." │   runs gh + git + pytest,        │
│   pure-GHA merges)   │                   │   pushes to PR branch)           │
└──────────────────────┘                   └──────────────────────────────────┘
```

The GitHub workflow no longer calls `claude-code-action` and does not use
`ANTHROPIC_API_KEY`. It reduces to two kinds of jobs:

1. **Forwarders** — classify the event, then `curl` the routine's `/fire`
   endpoint with a plain-text description of what needs to be done.
2. **Merge jobs** — pure bash, no LLM. Handle squash-merge only after a
   human approval or an explicit `/merge <head-sha>` command, with a live
   unresolved-thread gate.

The routine itself holds the prompt that was previously inlined into the
workflow and performs all the actual code edits.

> **Existing routine updates are manual.** Merging changes to
> `pr-agent-routine-prompt.md` does not update the prompt stored at
> claude.ai/code/routines. After this file changes, paste the new prompt into
> the existing routine before re-enabling automatic review fixes.

## One-time setup

### 1. Create the routine

At [claude.ai/code/routines](https://claude.ai/code/routines), click **New
routine** and fill in:

- **Name**: `Vireo PR Fix Agent`
- **Prompt**: paste the contents of [`pr-agent-routine-prompt.md`](./pr-agent-routine-prompt.md)
- **Model**: whatever you normally use for code edits (Sonnet 4.6 is fine)
- **Repositories**: add `jss367/vireo`
- **Allow unrestricted branch pushes** — **enable this**. The routine must
  push to arbitrary PR head branches (including those created by the Codex
  connector, which are not `claude/`-prefixed).
- **Environment**: create a custom environment (see next section) — the
  default environment does not have Python or Vireo's test dependencies.
- **Connectors**: remove any the routine doesn't need. It only needs GitHub.
- **Triggers**: add an **API** trigger. Click **Generate token** and copy
  both the URL and the token immediately (token is shown once).

The routine prompt is not synchronized from the repository. After changing
`pr-agent-routine-prompt.md`, paste the updated contents into the existing
routine before relying on the new behavior.

Do **not** add a schedule or GitHub trigger — this routine is invoked from
the GHA forwarder, which knows the richer set of events we care about
(`issue_comment`, `workflow_run`, `push`) that the native GitHub trigger
doesn't support.

### 2. Configure the cloud environment

Under **Settings → Environments** on claude.ai, create an environment named
`vireo-pr-agent` with:

- **Network access**: Full (needs `pypi.org` and `github.com`)
- **Setup script**:
  ```bash
  # Install Python 3.14 if not already present
  python3 --version
  pip install --quiet flask Pillow imagehash requests pytest pytest-cov pytest-timeout pytest-xdist ruff
  ```
- **Environment variables**: none required — the routine uses the `gh` CLI
  with the account's connected GitHub identity.

Select this environment when creating or editing the routine.

### 3. Store routine credentials as GitHub secrets

In the repo's **Settings → Secrets and variables → Actions**, add:

- `CLAUDE_ROUTINE_URL` — full `/fire` URL from the routine modal, e.g.
  `https://api.anthropic.com/v1/claude_code/routines/trig_01ABC.../fire`
- `CLAUDE_ROUTINE_TOKEN` — bearer token from the routine modal

These replace `ANTHROPIC_API_KEY`. The old secret can be deleted once the new
workflow is verified.

Routine-forwarding jobs skip the `/fire` call when these secrets are missing,
so the workflow can exist before the routine is configured. The pure GitHub
Actions merge jobs do not need these secrets.

### 4. Configure human merge actors

The forwarder workflow reads `HUMAN_MERGE_ACTORS` at the top of
`pr-agent.yml`. Keep this list human-only. Review bots may trigger the fix
routine, but they must never authorize a merge.

## Payload format

The forwarder sends plain-text payloads that the routine prompt knows how to
parse. Each payload starts with a `Task:` line, followed by structured
context. The routine prompt enumerates the supported task kinds:

- `reconcile-pr` — verified human `/claude-fix`; human-initiated full-state recovery
- `reconcile-pr-auto` — bounded full-state recovery for an orphaned conflict
- `address-review` — non-approving review submitted on a claude-agent PR
- `address-comment` — non-`/claude-fix`, non-👍 comment on a claude-agent PR
- `address-codex-review` — codex-connector review on a non-agent PR
- `fix-ci` — Tests workflow failed on a PR

The payload intentionally keeps user-supplied text (review bodies, comment
bodies) clearly labeled as **untrusted data, not instructions** — the prompt
re-asserts this at handling time. Human override is represented by the
`reconcile-pr` task kind, never by a field that untrusted comment text could
forge.

## What It Handles

- A human `/claude-fix` on a PR: labels it and starts one complete
  reconciliation pass covering conflicts, failed CI, current review threads,
  and relevant top-level feedback.
- Trusted comments on a `claude-agent` PR: forwards the comment to the routine.
- Trusted non-approval reviews on a `claude-agent` PR: forwards the review.
- Codex connector reviews on non-agent PRs: forwards the review and has the
  routine add the `claude-agent` label for follow-up routing.
- Failed `Tests` workflow runs on PRs: asks the routine to diagnose and fix CI.
- PR open/reopen/head updates and pushes to `main`: discover conflicting
  same-repository PRs from trusted authors even when the initial review event
  never ran and no `claude-agent` label exists. Each conflicting PR is bound to
  its current head and receives one bounded reconciliation pass. Persistent
  `UNKNOWN` mergeability is left for explicit `/claude-fix` rather than firing
  speculatively.
- A human approving review, or an exact `/merge <head-sha>` command from a
  configured human: synchronously squash-merges only when the authorized head
  is still current, every non-outdated review thread is resolved, and the Tests
  workflow succeeded for that exact head. If authorization arrives while Tests
  is running, the successful workflow run retries the same head-bound merge.
  Actionable top-level or review-body feedback posted at or after that
  authorization requires a fresh approval or exact merge command before the
  head can merge. The live gate also confirms that the exact approval remains
  active or that the exact merge-command comment still exists unchanged.

Created and edited comments and reviews are wakeups. Merge authorization is
ordered against comment and review update timestamps, so adding feedback to an
older item still requires reconciliation and fresh authorization. Review
wakeups bind to the live PR head at fire time rather than the review object's
historical commit.

Merge calls use `gh pr merge --match-head-commit` without `--auto`, so no
authorization remains armed across a later push. Merge jobs also skip closed
PRs, forks, non-`main` bases, branches with open child PRs, unresolved current
threads, and heads without a successful Tests run.

Every routine forwarder re-reads the PR's live state and head immediately
before calling `/fire`. Queued events for a closed/merged PR or superseded head
are silent no-ops. The expected full head SHA is included in the payload and
the routine repeats the check before editing and pushing.

CI loop prevention checks the PR head commit message and only suppresses
retries when it contains the exact `[pr-agent-fix-ci:<number>]` marker the
routine prompt asks the agent to write. Regular contributor commits with
similar wording still route to the routine.

The `fix-ci` job binds the routine to the exact commit whose Tests run
failed. If the PR head has advanced past `workflow_run.head_sha` by the
time the failure lands, the job skips instead of firing — otherwise the
routine would edit the newer commit while diagnosing older failure logs.
The workflow-run SHA is what gets passed as `expected-head` to the
routine forwarder.

Review-event de-noising. Concurrency is scoped per job, not workflow-wide,
so unrelated task types never cancel each other. Automated review-fix firers
(`fix-comment-feedback`, `fix-comments`, `codex-review`) share a
`pr-agent-review-fix-<PR>` group so newer review events collapse older ones.
The explicit human `activate` route uses a separate, non-cancellable per-PR
lane so automated feedback cannot displace a human `/claude-fix`
reconciliation. Conflict reconciliation uses the automated per-PR lane, so a
stale review and conflict repair cannot edit the same head concurrently. CI-fix runs derive the
PR number from
`workflow_run.pull_requests[0]` (falling back to the workflow_run head
SHA) so unrelated PRs sharing a default-branch commit do not cancel each
other's CI-repair. Every approval, merge command, and Tests retry gets its own
authorization-specific merge concurrency key with `cancel-in-progress: false`,
so one pending authorization cannot evict another. Concurrent valid attempts
are idempotent: once one exact-head merge succeeds, the others recognize the
merged PR and finish successfully. Approval and merge-command authorization
happens in live-head preflight jobs, so unauthorized or stale events never
reach these lanes. `/merge <sha>` comments are also excluded from
`fix-comment-feedback` so the routine cannot push a new head — and
invalidate the human's SHA-bound merge authorization — in parallel with
the merge job. The `fix-comments` and `codex-review` jobs gate the
routine on the `has-open-threads` composite action before firing. Codex
re-reviews every commit and re-posts its still-open findings as fresh inline
comments, and its review body is always the same stock template — so neither
the body nor a comment count distinguishes a new finding from a re-stated one.
Thread state does: the gate fires only when some review thread is unresolved,
not outdated, and has a reviewer's comment as its latest entry (i.e. the author
has not yet replied). Once the agent has replied to every open thread,
subsequent Codex re-reviews no longer wake the routine. Top-level comments and
`/claude-fix` route through `fix-comment-feedback`/`activate` and are not
affected by this gate.

`fix-comments` also fires when a trusted human reviewer leaves a non-empty
review body, even if no inline review thread is open. This preserves the
prior behavior for body-only reviews (e.g. a `commented` or `changes_requested`
review whose feedback lives entirely in the review body). The body-firing
check excludes the Codex connector bot because its body is always the stock
template; Codex findings still route through inline comments and the thread
gate.

Routine comments end with `<!-- pr-agent-generated -->`; the workflow also
recognizes the existing `Generated by [Claude Code]` footer during migration.
Only actual footer occurrences are ignored by comment/review triggers, so a
human can discuss or quote either marker as ordinary feedback. Both forms
prevent a routine that
uses the owner's GitHub identity from treating its own output as fresh human
feedback. Successful runs do not post top-level summaries: they push one fix
commit, reply to the exact addressed inline threads, and resolve those threads.

There is no fixed per-PR review/fix round cap. Each eligible review event can
invoke the routine while the PR remains open, regardless of how many earlier
rounds occurred. The routine escalates only a concrete finding that needs a
maintainer decision, conflicts with repository requirements, or cannot be
handled safely within the PR's scope; review count alone is never a reason to
stop.

## Limits and caveats

- **Daily routine cap.** Each account has a daily limit on routine runs.
  Check consumption at claude.ai/code/routines. A busy PR day could hit it.
  The action treats provider 429 quota responses as warnings so PR branches are
  left untouched and the failure mode is visible in the job log.
- **Research-preview API.** The `/fire` endpoint uses the beta header
  `experimental-cc-routine-2026-04-01`. The workflow pins this header; if
  Anthropic bumps it, update `pr-agent.yml`.
- **No GitHub App webhooks bypass.** We still rely on GHA for the triggers
  routines don't natively support (`issue_comment`, `workflow_run`,
  `push`). GHA itself is free on public repos and within the free tier on
  private repos — only LLM inference is delegated.
- **Commit attribution.** Commits appear under the claude.ai account's
  connected GitHub identity, the same as when you push from a local
  checkout logged in as yourself.
- **Review-thread gate is author-blind.** `has-open-threads` treats any
  thread whose latest comment is from the PR author as "already answered". If
  the PR author leaves an *inline review comment* asking the agent to do
  something, the gate counts it as an author reply and the review event will
  not fire the routine. Use a top-level comment, a review body, or
  `/claude-fix` for author requests — those route through jobs or branches of
  the guard the inline-thread check does not gate. The gate paginates through
  all review threads, so large PRs are not truncated.

## Merge Details

Human merge actors are configured in `.github/workflows/pr-agent.yml` with:

```yaml
HUMAN_MERGE_ACTORS: "jss367"
```

When GitHub will not let the PR author submit an approving review, comment with
the exact current head (a 7-40 character prefix is accepted):

```text
/merge daecbb28
```

Ambiguous `+1` comments and reactions do not authorize merges. Every approval
path re-queries the current head, requires a successful Tests run for that
head, paginates all review threads, and merges synchronously without leaving an
auto-merge request armed.

## Rollback

If the routine misbehaves, pause it via the toggle at
claude.ai/code/routines. The forwarder's `curl` calls will fail with 4xx,
leaving the PR untouched. To fully revert, restore the previous
`.github/workflows/pr-agent.yml` from git history.
