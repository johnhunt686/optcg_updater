#!/usr/bin/env bash
#
# Interactive front-end for updater.py.
# On first run (or incomplete config) it runs a setup wizard; afterwards it
# shows a menu: Launch / Check / Update / Rollback / Settings / Quit.
# Lives next to updater.py and calls it for all real work.
#
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER="$DIR/updater.py"
CFG="$DIR/config.json"
PY="${PYTHON:-python3}"

command -v "$PY" >/dev/null 2>&1 || { echo "error: python3 not found (set \$PYTHON)"; exit 1; }
[ -f "$UPDATER" ] || { echo "error: updater.py not found next to this script"; exit 1; }

# read one key from config.json (empty string if missing)
cfg_get() {
    "$PY" - "$CFG" "$1" <<'PY'
import json, os, sys
cfg, key = sys.argv[1], sys.argv[2]
data = json.load(open(cfg)) if os.path.exists(cfg) else {}
print(data.get(key) or "")
PY
}

pause() { read -r -p "press enter to continue... " _; }

expand_tilde() { printf '%s' "${1/#\~/$HOME}"; }

is_ready() {
    [ -n "$(cfg_get version_url)" ] && \
    [ -n "$(cfg_get install_dir)" ] && \
    [ -n "$(cfg_get launch_cmd)" ]
}

choose_platform() {
    local cur p
    cur="$(cfg_get platform)"; cur="${cur:-auto}"
    echo "platform choices: auto/windows/mac/linux/android/ios"
    read -r -p "platform [$cur]: " p; p="${p:-$cur}"
    case "$p" in
        auto|windows|mac|linux|android|ios) ;;
        *) echo "unsupported platform: $p"; return 1 ;;
    esac
    "$PY" "$UPDATER" config --platform "$p" >/dev/null
    echo "platform saved: $p"
}

run_wizard() {
    echo
    echo "=== setup (blank = keep current) ==="
    local cur v i l e p
    cur="$(cfg_get version_url)";     read -r -p "version page URL [$cur]: " v; v="${v:-$cur}"
    cur="$(cfg_get install_dir)";     read -r -p "install dir [$cur]: " i;      i="${i:-$cur}"
    cur="$(cfg_get launch_cmd)";      read -r -p "launch command [$cur]: " l;   l="${l:-$cur}"
    cur="$(cfg_get excluded_folder)"; cur="${cur:-decks}"
    read -r -p "excluded folder [$cur]: " e; e="${e:-$cur}"
    cur="$(cfg_get platform)"; cur="${cur:-auto}"
    read -r -p "platform [$cur]: " p; p="${p:-$cur}"

    case "$p" in
        auto|windows|mac|linux|android|ios) ;;
        *) echo "unsupported platform: $p"; return 1 ;;
    esac

    "$PY" "$UPDATER" config \
        --version-url "$v" \
        --install-dir "$i" \
        --launch-cmd "$l" \
        --excluded-folder "$e" \
        --platform "$p" >/dev/null

    if [ -n "$i" ]; then
        mkdir -p "$(expand_tilde "$i")" && echo "install folder created (empty — no app yet): $i"
    fi
    echo "settings saved. Run Update or Launch to install the app."
}

confirm() {
    local ans
    read -r -p "$1 [y/N] " ans
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# --- first run ---
is_ready || { echo "config incomplete — running setup."; run_wizard; }

# --- menu ---
PS3=$'\n''choose> '
while true; do
    echo
    iv="$(cfg_get installed_version)"
    echo "== app: $(cfg_get install_dir)  (installed: ${iv:-none}) =="
    select choice in "Launch app" "Check version" "Update" "Rollback" "Set platform" "Settings" "Quit"; do
        case "${REPLY:-}" in
            1) exec "$PY" "$UPDATER" launch ;;            # becomes util, spawns app, exits
            2) "$PY" "$UPDATER" check || true;  pause; break ;;
            3) "$PY" "$UPDATER" update || true; pause; break ;;
            4) if confirm "restore previous backup?"; then
                   "$PY" "$UPDATER" rollback || true
               else
                   echo "cancelled."
               fi
               pause; break ;;
            5) choose_platform || true; pause; break ;;
            6) run_wizard; break ;;
            7) exit 0 ;;
            *) echo "pick 1-7" ;;
        esac
    done
done
