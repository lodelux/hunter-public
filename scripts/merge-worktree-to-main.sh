#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "$1" >&2
  exit 1
}

SOURCE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
  || fail "Run this action from a Git worktree."
SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd -P)"
SOURCE_BRANCH="$(git -C "$SOURCE_ROOT" symbolic-ref --quiet --short HEAD || true)"

[[ "$SOURCE_BRANCH" != "main" ]] \
  || fail "This is already the main worktree; there is nothing to merge into main."
[[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=normal -- . \
  ':(exclude).codex/environments/environment.toml')" ]] \
  || fail "The current worktree has uncommitted changes; commit them before merging."

MAIN_ROOT=""
candidate=""
while IFS= read -r line; do
  case "$line" in
    "worktree "*) candidate="${line#worktree }" ;;
    "branch refs/heads/main") MAIN_ROOT="$candidate"; break ;;
  esac
done < <(git -C "$SOURCE_ROOT" worktree list --porcelain)

[[ -n "$MAIN_ROOT" ]] \
  || fail "No worktree currently has the main branch checked out."
MAIN_ROOT="$(cd "$MAIN_ROOT" && pwd -P)"
[[ -z "$(git -C "$MAIN_ROOT" status --porcelain --untracked-files=normal)" ]] \
  || fail "The main worktree is dirty; clean or commit it before merging."

SOURCE_SHA="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
SOURCE_LABEL="${SOURCE_BRANCH:-worktree commit $(git -C "$SOURCE_ROOT" rev-parse --short HEAD)}"
SOURCE_REF="${SOURCE_BRANCH:-$SOURCE_SHA}"
echo "Merging $SOURCE_LABEL into main..."
if ! git -C "$MAIN_ROOT" merge --no-edit "$SOURCE_REF"; then
  git -C "$MAIN_ROOT" merge --abort >/dev/null 2>&1 || true
  fail "The merge conflicted and was aborted; main was left unchanged."
fi

git -C "$MAIN_ROOT" merge-base --is-ancestor "$SOURCE_SHA" main \
  || fail "Merge finished, but main does not contain the source commit."
echo "Merged $SOURCE_LABEL into main at $(git -C "$MAIN_ROOT" rev-parse --short HEAD)."
echo "Nothing was pushed or deployed."
