#!/usr/bin/env bash
#
# nanogen — start a debug (CDP) Chrome/Chromium so nanogen_cli.py can attach
# to it with --attach.
#
# Works on Linux, macOS and Windows (Git Bash / WSL / MSYS).
#
#   ./start_nanogen_debug.sh                 # port 9222, nanogen profile
#   NANOGEN_PORT=9333 ./start_nanogen_debug.sh
#   NANOGEN_PROFILE=/data/chrome ./start_nanogen_debug.sh
#   NANOGEN_BROWSER=/usr/bin/chromium ./start_nanogen_debug.sh
#   NANOGEN_URL=https://gemini.google.com ./start_nanogen_debug.sh
#   NANOGEN_HEADLESS=1 ./start_nanogen_debug.sh
#
set -u

PORT="${NANOGEN_PORT:-9222}"
URL="${NANOGEN_URL:-https://gemini.google.com/}"
WAIT_TIMEOUT="${NANOGEN_WAIT_TIMEOUT:-30}"

# Persistent profile, so the Google session survives between runs.
if [ -n "${NANOGEN_PROFILE:-}" ]; then
    PROFILE="$NANOGEN_PROFILE"
elif [ -n "${HOME:-}" ]; then
    PROFILE="${HOME}/.nanogen/chrome_profile"
else
    PROFILE="$(pwd)/.nanogen/chrome_profile"
fi

START_URL="${URL}"

# --- locate a browser -------------------------------------------------------
find_browser() {
    if [ -n "${NANOGEN_BROWSER:-}" ] && [ -x "${NANOGEN_BROWSER}" ]; then
        printf '%s' "${NANOGEN_BROWSER}"
        return 0
    fi

    for candidate in \
        "google-chrome" "google-chrome-stable" "chromium" "chromium-browser" \
        "chrome" "microsoft-edge" "msedge"
    do
        if command -v "${candidate}" >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done

    # Snap/flatpak bundles are not always on PATH.
    for candidate in \
        "/snap/bin/chromium" \
        "/var/lib/flatpak/exports/bin/com.google.Chrome" \
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        "/Applications/Chromium.app/Contents/MacOS/Chromium" \
        "/c/Program Files/Google/Chrome/Application/chrome.exe" \
        "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe" \
        "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    do
        if [ -x "${candidate}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done

    return 1
}

BROWSER="$(find_browser)" || {
    echo "ERROR: no Chrome/Chromium found."
    echo "Install one (Debian/Ubuntu: sudo apt install chromium-browser) or set:"
    echo "  NANOGEN_BROWSER=/path/to/chrome $0"
    exit 1
}

# --- already running? -------------------------------------------------------
cdp_alive() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O /dev/null --timeout=2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1
    else
        python3 - "$PORT" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(2)
sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0)
PY
    fi
}

if cdp_alive; then
    echo "CDP is already listening on 127.0.0.1:${PORT} — reusing the running browser."
    echo "Run nanogen_cli.py with:  --attach --host 127.0.0.1 --port ${PORT}"
    exit 0
fi

mkdir -p "${PROFILE}" || {
    echo "ERROR: cannot create profile directory '${PROFILE}'."
    exit 1
}

# --- launch -----------------------------------------------------------------
case "$(uname -s)" in
    Darwin)
        open -na "${BROWSER}" --args \
            --remote-debugging-port="${PORT}" \
            --user-data-dir="${PROFILE}" \
            "${START_URL}"
        ;;
    MINGW* | MSYS* | CYGWIN*)
        # Git Bash on Windows.
        "$BROWSER" \
            --remote-debugging-port="${PORT}" \
            --user-data-dir="$(cygpath -w "${PROFILE}" 2>/dev/null || printf '%s' "${PROFILE}")" \
            "${START_URL}" >/dev/null 2>&1 &
        ;;
    *)
        "$BROWSER" \
            --remote-debugging-port="${PORT}" \
            --user-data-dir="${PROFILE}" \
            "${START_URL}" >/dev/null 2>&1 &
        ;;
esac

# --- wait until the endpoint answers ---------------------------------------
waited=0
while [ "${waited}" -lt "${WAIT_TIMEOUT}" ]; do
    if cdp_alive; then
        break
    fi
    sleep 1
    waited=$((waited + 1))
done

if ! cdp_alive; then
    echo "ERROR: CDP did not come up on port ${PORT} within ${WAIT_TIMEOUT}s."
    echo "Try running '${BROWSER}' manually to see the error message."
    exit 1
fi

echo "Browser : ${BROWSER}"
echo "Profile : ${PROFILE}"
echo "CDP     : http://127.0.0.1:${PORT}  (ready after ${waited}s)"
echo
echo "Sign in to Google once in this window, then run:"
echo "  python nanogen_cli.py --in prompts.json --attach --host 127.0.0.1 --port ${PORT}"