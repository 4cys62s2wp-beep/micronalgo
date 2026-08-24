#!/bin/sh
# micronalgo Mac-Installation.
#
#   sh deploy/install_mac.sh            # venv + Abhaengigkeiten + .env
#   sh deploy/install_mac.sh --launchd  # zusaetzlich als LaunchAgent einrichten
#
# Bewusst POSIX-sh und ohne sudo: alles landet im Repo-Verzeichnis und in
# ~/Library/LaunchAgents, nirgendwo sonst.

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO/.venv"
PLIST_SRC="$REPO/deploy/com.micronalgo.paper.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/com.micronalgo.paper.plist"

echo "==> micronalgo Installation in $REPO"

# ---------------------------------------------------------------- Python
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "FEHLER: python3 nicht gefunden."
    echo "  Entweder: xcode-select --install   (Apple Command Line Tools)"
    echo "  oder:     brew install python@3.12  (Homebrew)"
    exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
    3.1[0-9]|3.[2-9][0-9]) ;;
    *) echo "FEHLER: Python $PYVER gefunden, benoetigt wird >= 3.10."; exit 1 ;;
esac
echo "==> Python $PYVER: $PY"

# ---------------------------------------------------------------- venv
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> erstelle virtuelles Environment $VENV"
    "$PY" -m venv "$VENV"
fi
echo "==> installiere micronalgo + Abhaengigkeiten"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$REPO[all]"

# ---------------------------------------------------------------- config
if [ ! -f "$REPO/.env" ]; then
    cp "$REPO/.env.example" "$REPO/.env"
    echo "==> .env aus Vorlage angelegt -- BITTE AUSFUELLEN (Alpaca-Paper-Schluessel)."
else
    echo "==> .env existiert bereits, bleibt unangetastet."
fi
mkdir -p "$REPO/logs" "$REPO/state" "$REPO/reports" "$REPO/data/cache"

# ---------------------------------------------------------------- checks
echo "==> Selbsttest (offline)"
"$VENV/bin/python" -m pytest -q -x --no-header "$REPO/tests/test_returns.py" >/dev/null \
    && echo "    Kern-Mathematik ok" \
    || { echo "FEHLER: Selbsttest fehlgeschlagen -- Installation unvollstaendig."; exit 1; }

# ---------------------------------------------------------------- launchd
if [ "${1:-}" = "--launchd" ]; then
    echo "==> richte LaunchAgent ein: $PLIST_DST"
    mkdir -p "$HOME/Library/LaunchAgents"
    # Python statt sed: ein '&' oder '|' im Repo-Pfad ("Trading & Bots/...")
    # waere fuer sed ein Metazeichen und wuerde den Agenten still zerstoeren.
    "$VENV/bin/python" - "$PLIST_SRC" "$PLIST_DST" "$REPO" "$VENV" <<'PYEOF'
import pathlib, sys
from xml.sax.saxutils import escape

# XML-escape the paths: a folder named "Trading & Bots" is legal on macOS and a
# raw "&" is illegal in XML, so an unescaped path would render an unloadable
# plist. launchd reads the escaped form back as the literal path.
src, dst, repo, venv = sys.argv[1:5]
text = (pathlib.Path(src).read_text()
        .replace("__REPO__", escape(repo))
        .replace("__VENV__", escape(venv)))
pathlib.Path(dst).write_text(text)
PYEOF
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    echo "    geladen. Logs: tail -f $REPO/logs/launchd.err.log"
    echo "    entfernen:     launchctl unload $PLIST_DST && rm $PLIST_DST"
fi

cat <<EOT

Fertig. Naechste Schritte:
  1. $REPO/.env ausfuellen (Alpaca-Paper-Schluessel von app.alpaca.markets).
  2. "$VENV/bin/micronalgo" preflight --probe-orders     # prueft alles gegen das Paper-Konto
  3. "$VENV/bin/micronalgo" study                        # die echten MU-Zahlen
  4. "$VENV/bin/micronalgo" paper                        # dry-run; --live sendet Orders
  5. "$VENV/bin/micronalgo" status --watch               # Live-Ansicht

WICHTIG: Ein schlafender Mac verpasst die Auktionsfenster (der Bot ueberspringt
dann sicher, aber er handelt nicht). Siehe docs/MAC_SETUP.md, Abschnitt Schlaf.
EOT
