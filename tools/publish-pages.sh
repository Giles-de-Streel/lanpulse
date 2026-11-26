#!/bin/bash
# Publish web/ to the gh-pages branch (GitHub Pages serves that branch).
# Run from the repo root after committing changes to web/:  tools/publish-pages.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git diff --quiet -- web || { echo "Commit your web/ changes first"; exit 1; }
tools/check.sh >/dev/null || { tools/check.sh | grep -E "FAIL"; echo "Not published: tools/check.sh failed"; exit 1; }
git push origin "$(git subtree split --prefix web HEAD)":refs/heads/gh-pages --force
echo "Published. GitHub Pages updates in about a minute."
