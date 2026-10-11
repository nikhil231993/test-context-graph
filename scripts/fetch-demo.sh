#!/usr/bin/env bash
# Clone the open-source demo target, spring-petclinic-rest, into .demo/ (git-ignored).
# Pinned to a commit so results are reproducible; pass another ref as $1 to try a newer one.
set -euo pipefail
REF="${1:-afc8fc1d3b8ec8de69414482b3bd70a1da78cb95}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/.demo/spring-petclinic-rest"
if [ ! -d "$DEST/.git" ]; then
  git clone --quiet https://github.com/spring-petclinic/spring-petclinic-rest.git "$DEST"
fi
git -C "$DEST" fetch --quiet --depth 1 origin "$REF" 2>/dev/null || git -C "$DEST" fetch --quiet origin
git -C "$DEST" checkout --quiet "$REF"
echo "spring-petclinic-rest at $(git -C "$DEST" rev-parse --short HEAD) in $DEST"
