#!/bin/bash
# Run tools/check.sh before every git push from this clone (skip once with: git push --no-verify).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
cat > .git/hooks/pre-push <<'HOOK'
#!/bin/bash
cd "$(git rev-parse --show-toplevel)"
echo "Running tools/check.sh before pushing..."
tools/check.sh >/tmp/lanpulse-check.log 2>&1 || { grep -E "FAIL" /tmp/lanpulse-check.log; echo "Push stopped: fix the above (full log: /tmp/lanpulse-check.log)."; exit 1; }
echo "Checks passed."
HOOK
chmod +x .git/hooks/pre-push
echo "Installed: tools/check.sh now runs before each git push."
