#!/usr/bin/env bash
#
# merge-chain.sh — Squash-merge a chain of PRs from leaf to root.
#
# Usage: merge-chain.sh <leaf-pr-number>
#
# Traces "Parent PR: #N" links from the leaf PR back to the root
# (which targets main). Squash-merges each PR in order, running
# tests between merges.
#
set -euo pipefail

LEAF_PR="$1"
REPO="${GITHUB_REPOSITORY:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

echo "=== Merge Chain: starting from PR #${LEAF_PR} ==="

# -- Step 1: Build the chain (leaf -> root) --------------------
chain=()
current="$LEAF_PR"

while true; do
  chain+=("$current")
  echo "  Chain link: PR #${current}"

  body=$(gh pr view "$current" --repo "$REPO" --json body -q .body)
  parent=$(echo "$body" | grep -oP 'Parent PR: #\K[0-9]+' || true)

  if [[ -z "$parent" ]]; then
    echo "  Root PR: #${current} (no parent link)"
    break
  fi

  current="$parent"
done

echo ""
echo "=== Chain (leaf -> root): ${chain[*]} ==="
echo ""

# -- Step 2: Squash-merge each PR (leaf first) ----------------
for pr in "${chain[@]}"; do
  echo "-- Merging PR #${pr} --"

  # Get PR details
  pr_json=$(gh pr view "$pr" --repo "$REPO" --json headRefName,baseRefName,title,state)
  state=$(echo "$pr_json" | jq -r .state)
  head_ref=$(echo "$pr_json" | jq -r .headRefName)
  base_ref=$(echo "$pr_json" | jq -r .baseRefName)
  title=$(echo "$pr_json" | jq -r .title)

  if [[ "$state" == "MERGED" ]]; then
    echo "  Already merged, skipping."
    continue
  fi

  if [[ "$state" == "CLOSED" ]]; then
    echo "  ERROR: PR #${pr} is closed (not merged). Aborting."
    exit 1
  fi

  echo "  ${head_ref} -> ${base_ref}: ${title}"

  # Check for merge conflicts
  mergeable=$(gh pr view "$pr" --repo "$REPO" --json mergeable -q .mergeable)
  if [[ "$mergeable" == "CONFLICTING" ]]; then
    echo "  ERROR: PR #${pr} has merge conflicts. Resolve before merging chain."
    exit 1
  fi

  # If this PR targets main, use --auto to wait for required CI
  if [[ "$base_ref" == "main" ]]; then
    echo "  Targets main -- using auto-merge (waits for CI)..."
    gh pr merge "$pr" --repo "$REPO" --squash --auto \
      --subject "feat: ${title} (#${pr})"
    echo "  Auto-merge enabled. GitHub will merge when CI passes."
  else
    # Intermediate merge -- run tests first, then merge
    echo "  Intermediate merge -- running tests..."

    # Fetch the latest and checkout the head branch
    git fetch origin "$head_ref" "$base_ref"
    git checkout "$head_ref"

    # Run tests to verify this branch is healthy
    python -m pytest tests/test_workspaces.py \
      vireo/tests/test_db.py vireo/tests/test_app.py \
      vireo/tests/test_photos_api.py vireo/tests/test_edits_api.py \
      vireo/tests/test_jobs_api.py vireo/tests/test_darktable_api.py \
      vireo/tests/test_config.py -v

    echo "  Tests passed. Squash-merging..."
    gh pr merge "$pr" --repo "$REPO" --squash \
      --subject "fix: ${title} (#${pr})"

    echo "  Merged PR #${pr}."
  fi

  echo ""
done

# -- Step 3: Cleanup ------------------------------------------
echo "=== Cleanup ==="

for pr in "${chain[@]}"; do
  head_ref=$(gh pr view "$pr" --repo "$REPO" --json headRefName -q .headRefName 2>/dev/null || true)
  if [[ -n "$head_ref" && "$head_ref" != "main" ]]; then
    echo "  Deleting branch: ${head_ref}"
    gh api -X DELETE "repos/${REPO}/git/refs/heads/${head_ref}" 2>/dev/null || true
  fi
done

echo ""
echo "=== Merge chain complete ==="
