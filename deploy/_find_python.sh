# Findet den neuesten brauchbaren Python-Interpreter (>= 3.10).
# Wird von start_mac.sh und install_mac.sh eingebunden, nicht direkt gestartet.
#
# Warum das noetig ist: `python3` allein reicht auf einem Mac nicht.
#   * Die Apple Command Line Tools liefern Python 3.9 -- zu alt.
#   * Ein aktives venv eines anderen Projekts kapert den Namen zusaetzlich,
#     und VS Code aktiviert solche venvs in neuen Terminals automatisch.
# Deshalb wird an allen Stellen gesucht, an denen macOS Interpreter ablegt,
# und der neueste gewonnen -- unabhaengig davon, worauf `python3` gerade zeigt.

py_version_code() {
    "$1" -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>/dev/null || echo 0
}

find_python() {
    _best=""; _best_code=0
    for _cand in \
        /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
        /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
        /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
        "$(command -v python3.13 2>/dev/null)" "$(command -v python3.12 2>/dev/null)" \
        "$(command -v python3.11 2>/dev/null)" "$(command -v python3.10 2>/dev/null)" \
        "$(command -v python3 2>/dev/null)"
    do
        [ -n "$_cand" ] && [ -x "$_cand" ] || continue
        _code="$(py_version_code "$_cand")"
        [ "$_code" -ge 310 ] || continue
        if [ "$_code" -gt "$_best_code" ]; then _best="$_cand"; _best_code="$_code"; fi
    done
    printf '%s' "$_best"
}

# Was der Nutzer tun soll, wenn nichts Brauchbares da ist.
python_install_hint() {
    if command -v brew >/dev/null 2>&1; then
        printf 'brew install python@3.12'
    else
        printf 'Homebrew von brew.sh installieren, dann: brew install python@3.12\n       (alternativ Python 3.12+ von python.org)'
    fi
}
