#!/bin/sh
# micronalgo: von null bis laufender Bot, in einem Durchgang.
#
#   sh deploy/start_mac.sh
#
# Idempotent: beliebig oft ausfuehrbar. Was schon erledigt ist, wird
# uebersprungen. Reihenfolge ist Absicht -- es wird nichts gehandelt, bevor
# nicht gegen Dein echtes Paper-Konto bewiesen ist, dass die Auktionsorders
# akzeptiert werden.

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO/.venv"
BIN="$VENV/bin/micronalgo"
cd "$REPO"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
die()  { printf '\n\033[31mABBRUCH: %s\033[0m\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------- 1. Python
say "1/6  Python und Abhaengigkeiten"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 fehlt. Behebe das mit: xcode-select --install"
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
    3.1[0-9]|3.[2-9][0-9]) ;;
    *) die "Python $PYVER gefunden, benoetigt wird 3.10 oder neuer." ;;
esac
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e ".[all]"
printf '    Python %s, micronalgo installiert.\n' "$PYVER"

# --------------------------------------------------------------- 2. Zugang
say "2/6  Alpaca-Paper-Zugang"
[ -f .env ] || cp .env.example .env
mkdir -p logs state reports data/cache

# Schluessel aus .env lesen, ohne die Datei auszufuehren.
read_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- ; }
KEY="$(read_env ALPACA_API_KEY_ID)"
SEC="$(read_env ALPACA_API_SECRET_KEY)"

# Eingaben kommen von /dev/tty, nicht von stdin. Sonst funktioniert die
# Abfrage nicht, wenn das Skript selbst ueber eine Pipe kommt
# (`curl ... | sh`) -- dann ist stdin naemlich das Skript.
if [ -r /dev/tty ]; then TTY=/dev/tty; else TTY=""; fi
ask() { printf '%s' "$1"; [ -n "$TTY" ] && read -r REPLY_VALUE < "$TTY" || read -r REPLY_VALUE; }

if [ -z "$KEY" ] || [ -z "$SEC" ]; then
    if [ -z "$TTY" ]; then
        die ".env enthaelt keine Alpaca-Schluessel und es gibt kein Terminal fuer die Abfrage.
       Trage sie von Hand ein: $REPO/.env"
    fi
    cat <<'EOT'

    Es fehlen die Paper-Trading-Schluessel. Du bekommst sie hier:
      app.alpaca.markets  ->  Paper Trading  ->  API Keys  ->  Generate

    Paper heisst: Spielgeld, echte Kurse, echte Auktionsmechanik.
    Die Eingaben landen ausschliesslich in .env (steht in .gitignore).

EOT
    ask '    Key ID:     '; KEY="$REPLY_VALUE"
    stty -echo < "$TTY" 2>/dev/null || true
    ask '    Secret Key: '; SEC="$REPLY_VALUE"
    stty echo < "$TTY" 2>/dev/null || true
    printf '\n'
    [ -n "$KEY" ] && [ -n "$SEC" ] || die "Beide Schluessel werden gebraucht."

    "$VENV/bin/python" - "$KEY" "$SEC" <<'PYEOF'
import pathlib, re, sys
key, sec = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
text = p.read_text()
for name, value in (("ALPACA_API_KEY_ID", key), ("ALPACA_API_SECRET_KEY", sec)):
    line = f"{name}={value}"
    if re.search(rf"^{name}=.*$", text, re.M):
        text = re.sub(rf"^{name}=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
p.write_text(text)
PYEOF
    chmod 600 .env
    printf '    In .env gespeichert (Dateirechte auf 600 gesetzt).\n'
else
    printf '    Schluessel aus .env uebernommen.\n'
fi

# Broker auf Alpaca stellen, falls die Vorlage noch auf sim steht.
"$VENV/bin/python" - <<'PYEOF'
import pathlib, re
p = pathlib.Path(".env"); text = p.read_text()
if not re.search(r"^MICRONALGO_BROKER=alpaca", text, re.M):
    text = re.sub(r"^MICRONALGO_BROKER=.*$", "MICRONALGO_BROKER=alpaca", text, count=1, flags=re.M) \
        if re.search(r"^MICRONALGO_BROKER=", text, re.M) else text + "\nMICRONALGO_BROKER=alpaca\n"
    p.write_text(text)
PYEOF

# --------------------------------------------------------------- 3. Preflight
say "3/6  Preflight gegen Dein Paper-Konto"
warn "Prueft, ob die Auktionsorders (MOC/OPG) wirklich akzeptiert werden."
warn "Das ist die Annahme, an der die gesamte Strategie haengt."
if ! "$BIN" preflight --probe-orders; then
    die "Preflight nicht bestanden. Es wird nichts gehandelt, bevor das sauber ist.
       Haeufigste Ursachen: falsche Schluessel, oder Live- statt Paper-Schluessel."
fi

# --------------------------------------------------------------- 4. Studie
say "4/6  Die Studie: bleibt von den +140.000 % etwas uebrig?"
warn "Laedt die echte MU-Historie und rechnet alles durch. Dauert eine Weile."
STUDY_OK=1
"$BIN" study || STUDY_OK=$?
case "$STUDY_OK" in
    0) printf '\n    Urteil: bestanden oder mit Vorbehalt. Details oben und im Bericht.\n' ;;
    2) warn "Urteil: FAIL -- mindestens eine Pruefung ist durchgefallen."
       warn "Lies die FAIL-Zeile. Sie sagt, welche Annahme nicht haelt." ;;
    *) warn "Studie nicht durchgelaufen (meist: kein Datenprovider erreichbar)."
       warn "Der Bot laeuft trotzdem; die Preis-Plausibilitaetspruefung ist dann inaktiv." ;;
esac
REPORT="$(ls -t reports/*.html 2>/dev/null | head -1 || true)"
[ -n "$REPORT" ] && { open "$REPORT" 2>/dev/null || true; printf '    Bericht: %s\n' "$REPORT"; }

# --------------------------------------------------------------- 5. Freigabe
say "5/6  Scharfschalten"
MODE="--dry-run"
if [ -n "$TTY" ]; then
    cat <<'EOT'

    Jetzt oder Trockenlauf?
      j  = echte Orders an Dein PAPER-Konto (Spielgeld, echte Mechanik)
      n  = Trockenlauf: er protokolliert nur, was er tun wuerde

EOT
    ask '    Echte Paper-Orders senden? [j/N] '; ANSWER="$REPLY_VALUE"
    case "$ANSWER" in
        j|J|y|Y) MODE="--live"; printf '    Scharf. Es gehen echte Paper-Orders raus.\n' ;;
        *)       printf '    Trockenlauf.\n' ;;
    esac
else
    warn "Kein Terminal fuer die Rueckfrage -- bleibe im Trockenlauf."
fi

# --------------------------------------------------------------- 6. Start
say "6/6  Bot laeuft"
cat <<EOT

    Beenden:      Ctrl-C
    Not-Aus:      $BIN kill
    Live-Ansicht: $BIN status --watch     (zweites Terminalfenster)
    Dauerbetrieb: sh deploy/install_mac.sh --launchd

    Er handelt zweimal taeglich: Kauf in die Schlussauktion (15:45 New Yorker
    Zeit), Verkauf in die Eroeffnungsauktion (09:30). Dazwischen schlaeft er
    bis zum naechsten Entscheidungszeitpunkt -- Stille ist der Normalzustand.

EOT
exec "$BIN" paper $MODE
