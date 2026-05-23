#!/bin/bash
# apply-baseline-protection.sh — Apply a baseline ruleset to one or more repos.
#
# The ruleset targets the default branch (~DEFAULT_BRANCH, so no per-repo
# branch name needed) and enforces:
#   - PR required before merging (blocks direct pushes to the default branch)
#   - Force pushes blocked (non_fast_forward)
#   - Branch deletion blocked (belt-and-suspenders; GitHub already blocks
#     default-branch deletion platform-wide)
#   - Optionally: at least 1 approving review (--require-approval). This
#     prevents the gh-app-agent App from merging its own PRs, since GitHub
#     forbids an App from approving a PR it authored.
#
# Note: rulesets on PRIVATE repos under a personal account require GitHub Pro.
# Public repos work for free. The script reports a clean SKIP for repos it
# can't touch and exits non-zero at the end if any failed.
#
# Requires: gh CLI authenticated with admin on the target repos; jq.
# Note: the gh-app-agent App does NOT have admin permission — use your
# personal gh auth (gh auth login) for this.
#
# Idempotent: if a ruleset named "baseline-default-protection" already exists
# on a repo, it is updated in place. Otherwise a new one is created.
#
# Usage:
#   apply-baseline-protection.sh [--dry-run] [--require-approval] [--owner OWNER] <repo> [<repo>...]
#   cat repos.txt | apply-baseline-protection.sh [flags]
#
# Examples:
#   apply-baseline-protection.sh --dry-run gh-app-agent dotfiles
#   apply-baseline-protection.sh --require-approval gh-app-agent
#   gh repo list --limit 200 --json name,visibility \
#       --jq '.[] | select(.visibility=="PUBLIC") | .name' \
#     | apply-baseline-protection.sh --dry-run
#
# Defaults: OWNER = currently authenticated gh user.

set -euo pipefail

OWNER=""
RULESET_NAME="baseline-default-protection"
DRY_RUN=0
REQUIRE_APPROVAL=0
REPOS=()

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^set -euo/d'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)          DRY_RUN=1; shift ;;
    --require-approval) REQUIRE_APPROVAL=1; shift ;;
    --owner)            OWNER="$2"; shift 2 ;;
    -h|--help)          usage 0 ;;
    --)                 shift; REPOS+=("$@"); break ;;
    -*)                 echo "ERROR: unknown flag: $1" >&2; usage 1 ;;
    *)                  REPOS+=("$1"); shift ;;
  esac
done

if [ -z "$OWNER" ]; then
  OWNER="$(gh api user --jq .login 2>/dev/null || true)"
  if [ -z "$OWNER" ]; then
    echo "ERROR: --owner not given and could not determine current gh user. Run 'gh auth login' or pass --owner." >&2
    exit 1
  fi
fi

# Append any repos piped on stdin (one per line; '#' comments and blanks ignored).
if [ ! -t 0 ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    REPOS+=("$line")
  done
fi

if [ ${#REPOS[@]} -eq 0 ]; then
  echo "ERROR: no repos provided" >&2
  usage 1
fi

payload() {
  cat <<JSON
{
  "name": "${RULESET_NAME}",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["~DEFAULT_BRANCH"],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": ${REQUIRE_APPROVAL},
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": ["merge", "squash", "rebase"]
      }
    },
    { "type": "deletion" },
    { "type": "non_fast_forward" }
  ]
}
JSON
}

apply_to_repo() {
  local repo="$1"
  local list_response list_status=0
  list_response="$(gh api "/repos/${OWNER}/${repo}/rulesets" 2>&1)" || list_status=$?

  if [ "$list_status" -ne 0 ]; then
    local msg
    msg="$(printf '%s' "$list_response" | jq -r '.message // empty' 2>/dev/null)"
    [ -z "$msg" ] && msg="$list_response"
    echo "SKIP ${OWNER}/${repo}: cannot list rulesets (${msg})" >&2
    return 1
  fi

  local existing_id
  existing_id="$(printf '%s' "$list_response" \
    | jq -r --arg n "$RULESET_NAME" '.[] | select(.name == $n) | .id' \
    | head -n1)"

  if [ "$DRY_RUN" -eq 1 ]; then
    if [ -n "$existing_id" ]; then
      echo "[dry-run] would UPDATE ruleset ${existing_id} on ${OWNER}/${repo} (require_approval=${REQUIRE_APPROVAL})"
    else
      echo "[dry-run] would CREATE ruleset on ${OWNER}/${repo} (require_approval=${REQUIRE_APPROVAL})"
    fi
    return 0
  fi

  if [ -n "$existing_id" ]; then
    payload | gh api --method PUT "/repos/${OWNER}/${repo}/rulesets/${existing_id}" --input - >/dev/null
    echo "updated ${OWNER}/${repo} (ruleset ${existing_id}, require_approval=${REQUIRE_APPROVAL})"
  else
    local new_id
    new_id="$(payload | gh api --method POST "/repos/${OWNER}/${repo}/rulesets" --input - --jq '.id')"
    echo "created ${OWNER}/${repo} (ruleset ${new_id}, require_approval=${REQUIRE_APPROVAL})"
  fi
}

EXIT=0
for repo in "${REPOS[@]}"; do
  if ! apply_to_repo "$repo"; then
    EXIT=1
  fi
done
exit "$EXIT"
