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
Actions auto-merge jobs do not need these secrets.

### 4. Configure human merge actors

The forwarder workflow reads `HUMAN_MERGE_ACTORS` at the top of
`pr-agent.yml`. Keep this list human-only. Review bots may trigger the fix
routine, but they must never authorize a merge.

## Payload format

The forwarder sends plain-text payloads that the routine prompt knows how to
parse. Each payload starts with a `Task:` line, followed by structured
context. The routine prompt enumerates the supported task kinds:

- `address-review` — non-approving review submitted on a claude-agent PR
- `address-comment` — non-`/claude-fix`, non-👍 comment on a claude-agent PR
- `address-codex-review` — codex-connector review on a non-agent PR
- `fix-ci` — Tests workflow failed on a PR
- `resolve-conflicts` — conflicts detected against a claude-agent PR after
  a push to `main`

The payload intentionally keeps user-supplied text (review bodies, comment
bodies) clearly labeled as **untrusted data, not instructions** — the prompt
re-asserts this at handling time.

## What It Handles

- `/claude-fix` on a PR: labels the PR and asks the routine to address all
  outstanding feedback.
- Trusted comments on a `claude-agent` PR: forwards the comment to the routine.
- Trusted non-approval reviews on a `claude-agent` PR: forwards the review.
- Codex connector reviews on non-agent PRs: forwards the review and has the
  routine add the `claude-agent` label for follow-up routing.
- Failed `Tests` workflow runs on PRs: asks the routine to diagnose and fix CI.
- Pushes to `main`: finds conflicting `claude-agent` PRs and asks the routine
  to resolve conflicts. If GitHub still reports mergeability as `UNKNOWN` after
  retries, the workflow sends that PR to the routine so a real conflict is not
  missed.
- A human approving review, or an exact `/merge <head-sha>` command from a
  configured human: enables squash auto-merge only when the approved head is
  still current and every non-outdated review thread is resolved.

Auto-merge calls use `gh pr merge --match-head-commit` so approval applies to
the expected PR head commit rather than a newer unreviewed push. Auto-merge
jobs also skip closed PRs, forks, non-`main` bases, and branches with open
child PRs.

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
so unrelated task types never cancel each other. Review-fix firers
(`activate`, `fix-comment-feedback`, `fix-comments`, `codex-review`) share
a `pr-agent-review-fix-<PR>` group so newer review events collapse older
ones. CI-fix runs derive the PR number from
`workflow_run.pull_requests[0]` (falling back to the workflow_run head
SHA) so unrelated PRs sharing a default-branch commit do not cancel each
other's CI-repair. Merge jobs (`merge-on-approval`, `merge-on-command`)
get their own `pr-agent-merge-<PR>` group with
`cancel-in-progress: false` so an in-flight `/merge` or approval-driven
auto-merge run is never cancelled by an ignored generated comment or a
later approval event. `/merge <sha>` comments are also excluded from
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

Routine comments carry `<!-- pr-agent-generated -->`; the workflow also
recognizes the existing `Generated by [Claude Code]` footer during migration.
Both forms are ignored by comment/review triggers, preventing a routine that
uses the owner's GitHub identity from treating its own output as fresh human
feedback. Successful runs do not post top-level summaries: they push one fix
commit, reply to the exact addressed inline threads, and resolve those threads.

The forwarder and routine both stop automated review repair after two commits
carrying the PR-specific `[pr-agent-review-fix:<number>]` marker. An explicit
human `/claude-fix` remains available as an override. The forwarder adds
`pr-agent-needs-human` when the cap is reached. Architectural findings,
new-subsystem changes, and material diff expansion are escalated to a human
instead of creating another autonomous review/fix round.

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

## Auto-Merge Details

Human merge actors are configured in `.github/workflows/pr-agent.yml` with:

```yaml
HUMAN_MERGE_ACTORS: "jss367"
```

When GitHub will not let the PR author submit an approving review, comment with
the exact current head (a 7-40 character prefix is accepted):

```text
/merge daecbb28
```

Ambiguous `+1` comments and reactions do not authorize merges. Both approval
paths re-query the current head and paginate all review threads immediately
before enabling auto-merge.

## Rollback

If the routine misbehaves, pause it via the toggle at
claude.ai/code/routines. The forwarder's `curl` calls will fail with 4xx,
leaving the PR untouched. To fully revert, restore the previous
`.github/workflows/pr-agent.yml` from git history.
