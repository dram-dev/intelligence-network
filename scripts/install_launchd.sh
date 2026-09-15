#!/usr/bin/env bash
# Install launchd jobs for intelligence-network on the Mac mini.
# Run from the project root: bash scripts/install_launchd.sh
#
# Jobs: bot (KeepAlive Telegram listener) · watch (every 5 min) ·
#       daily (01:10, queued behind macro 01:00 + PC 01:05 on the run lock) ·
#       notify (08:00 digest ping).

set -euo pipefail

PROJECT_PATH="$(cd "$(dirname "$0")/.." && pwd)"
UV_PATH="$(command -v uv || true)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

if [[ -z "$UV_PATH" ]]; then
    echo "✗ 'uv' not found in PATH. Install uv first: brew install uv"
    exit 1
fi

echo "Project: $PROJECT_PATH"
echo "uv:      $UV_PATH"
echo "Target:  $LAUNCH_AGENTS"

mkdir -p "$LAUNCH_AGENTS"
mkdir -p "$PROJECT_PATH/logs"

for label in bot watch daily notify; do
    src="$PROJECT_PATH/launchd/com.dr.intelnet.${label}.plist"
    dst="$LAUNCH_AGENTS/com.dr.intelnet.${label}.plist"

    if [[ ! -f "$src" ]]; then
        echo "✗ Missing template: $src"
        exit 1
    fi

    sed -e "s|__PROJECT_PATH__|$PROJECT_PATH|g" \
        -e "s|__UV_PATH__|$UV_PATH|g" \
        "$src" > "$dst"

    # Modern bootstrap/bootout API — the legacy `launchctl load` silently no-ops
    # on macOS 11+ and leaves the job un-registered.
    launchctl bootout "gui/$(id -u)/com.dr.intelnet.${label}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
    echo "✓ Loaded $label job"
done

echo ""
echo "Verify with: launchctl list | grep com.dr.intelnet   (or: uv run intelnet health)"
echo "Logs will appear in: $PROJECT_PATH/logs/"
