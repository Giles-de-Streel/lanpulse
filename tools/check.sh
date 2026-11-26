#!/usr/bin/env bash
# lanpulse pre-deploy check: run before pushing, publishing or copying files to a box.
#   - Python files compile, shell scripts parse
#   - the dashboard loads in a headless browser (main page, wall screen, devices / Wi-Fi / history
#     pages, light and dark) with the demo data, finishes drawing, and logs no script errors
# Exits non-zero on any failure. Needs python3 and Chrome/Chromium/Edge (set CHROME=... if not found).
# Installed as the git pre-push hook by tools/install-hooks.sh, and run by tools/publish-pages.sh.
set -u
cd "$(dirname "$0")/.."
fail=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fail=1; }

PY=""
for c in python3 python py; do          # Windows has a python3 placeholder that only opens the Store
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then PY=$c; break; fi
done
[ -n "$PY" ] || { echo "python 3 not found"; exit 2; }

echo "== Python"
for f in collector/*.py tools/*.py; do
  if "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1])" "$f" 2>/tmp/lp-check-py.$$; then
    ok "$f"
  else
    bad "$f: $(tail -1 /tmp/lp-check-py.$$)"
  fi
done
rm -f /tmp/lp-check-py.$$

echo "== Shell scripts"
for f in box/setup.sh box/scripts/*.sh tools/*.sh; do
  if bash -n "$f" 2>/dev/null; then ok "$f"; else bad "$f: $(bash -n "$f" 2>&1 | head -1)"; fi
done

echo "== Demo data"
if "$PY" -c "import json,sys; json.load(open('web/demo/snapshot.json', encoding='utf-8'))"; then ok "web/demo/snapshot.json is valid JSON"; else bad "web/demo/snapshot.json"; fi

echo "== Dashboard in a headless browser"
CHROME=${CHROME:-}
if [ -z "$CHROME" ]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge \
           "/c/Program Files/Google/Chrome/Application/chrome.exe" \
           "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then CHROME=$c; break; fi
  done
fi
if [ -z "$CHROME" ]; then
  bad "no Chrome/Chromium/Edge found (set CHROME=/path/to/browser): page not checked"
else
  port=$((18000 + RANDOM % 1000))
  "$PY" -m http.server "$port" --bind 127.0.0.1 --directory web >/dev/null 2>&1 &
  srv=$!
  trap 'kill $srv 2>/dev/null' EXIT
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    "$PY" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=1)" "http://127.0.0.1:$port/" 2>/dev/null && break
    sleep 0.5
  done
  prof=$(mktemp -d)
  for q in "" "&kiosk" "&theme=dark" "&view=devices" "&view=wifi" "&view=history" "&kiosk&theme=dark"; do
    url="http://127.0.0.1:$port/?source=demo$q"
    out=$("$CHROME" --headless=new --disable-gpu --no-first-run --user-data-dir="$prof" \
          --enable-logging=stderr --v=0 --virtual-time-budget=8000 --dump-dom "$url" 2>"$prof/err.txt")
    errs=$(grep -E 'Uncaught|SyntaxError|TypeError|ReferenceError' "$prof/err.txt" | grep -v 'favicon' | head -3)
    if ! printf '%s' "$out" | grep -q 'data-booted="1"'; then
      bad "?source=demo$q: page never finished drawing${errs:+ ($errs)}"
    elif [ -n "$errs" ]; then
      bad "?source=demo$q: script error: $errs"
    else
      ok "?source=demo$q"
    fi
  done
  rm -rf "$prof"
fi

echo
if [ $fail -ne 0 ]; then echo "CHECK FAILED: fix the above before deploying."; exit 1; fi
echo "All checks passed."
