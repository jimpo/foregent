#!/usr/bin/env bash
# Install (or refresh) foregent's CAO agent profiles into the local agent
# store. Run from anywhere; idempotent. Phase 2+ will automate this as part
# of profile/skills sync.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for profile in "$repo_root"/profiles/*.md; do
  echo "Installing $(basename "$profile")"
  cao install "$profile"
done
