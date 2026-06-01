#!/usr/bin/env bash
# watch-mine.sh — spawn watch pollers for every id a "list mine" query returns.
#
# Glue between a platform list op (gl-mrs / gh-prs) and the generic watch
# preset. The query op owns "what's mine"; the watch preset owns "watch one
# id". This script just connects them. Idempotent — the watch op skips ids
# already being watched, so it is safe to re-run on a loop:
#
#   /loop 5m bash presets/watch/watch-mine.sh
#
# Args (all optional):
#   $1 FEED    supertool op emitting bare ids   (default: my failing MRs)
#   $2 SOURCE  watch source for those ids       (default: gitlab-mr)
#   $3 ONLY    events to emit notifications for  (default: pipeline_failed,merged)
#
# Example — watch my failing GitHub PRs instead:
#   bash watch-mine.sh 'gh-prs:author=@me,failed,iids' github-pr
set -euo pipefail

ST="${SUPERTOOL:-./supertool}"
FEED="${1:-gl-mrs:author=@me,failed,iids}"
SOURCE="${2:-gitlab-mr}"
ONLY="${3:-pipeline_failed,merged}"

# The supertool wrapper frames output with a header + PASS line; the bare ids
# are the only all-digit lines.
ids="$("$ST" "$FEED" | grep -E '^[0-9]+$' || true)"

if [ -z "$ids" ]; then
  echo "watch-mine: no ids from '$FEED' — nothing to watch."
  exit 0
fi

for id in $ids; do
  "$ST" "watch:${SOURCE}:${id}:only=${ONLY}" >/dev/null 2>&1 \
    && echo "watch-mine: watching ${SOURCE}:${id}"
done

"$ST" 'watches'
