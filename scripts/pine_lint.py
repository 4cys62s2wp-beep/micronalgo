"""Offline checks for the Pine sources.

TradingView cannot be reached from here, so this enforces the mechanical rules
that are checkable without a compiler. The important one is Pine's indentation
convention: a block is indented by a multiple of four spaces, and a *line
continuation* must be indented by a number of spaces that is NOT a multiple of
four -- otherwise the parser reads it as a new statement inside a block.
"""
import re
import sys
from pathlib import Path

BUILTIN_BLOCK_STARTERS = ("if ", "else", "for ", "while ", "switch")


def strip_comment(line: str) -> str:
    out, in_str = [], False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            in_str = not in_str
        if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def check(path: Path) -> list[str]:
    problems: list[str] = []
    lines = path.read_text().splitlines()

    if not lines or lines[0].strip() != "//@version=6":
        problems.append("line 1 must be exactly '//@version=6'")

    depth = 0
    for n, raw in enumerate(lines, 1):
        code = strip_comment(raw)
        if "\t" in raw:
            problems.append(f"{n}: tab character (Pine wants spaces)")
        if any(ord(c) > 127 for c in raw):
            problems.append(f"{n}: non-ASCII character")
        if raw.rstrip() != raw:
            problems.append(f"{n}: trailing whitespace")

        if code.strip():
            indent = len(code) - len(code.lstrip(" "))
            continuation = depth > 0
            if continuation and indent % 4 == 0 and indent > 0:
                problems.append(
                    f"{n}: continuation line indented {indent} spaces (a multiple of 4); "
                    "Pine would read it as a new statement"
                )
            if not continuation and indent % 4 != 0:
                problems.append(f"{n}: statement indented {indent} spaces (not a multiple of 4)")

        in_str = False
        for ch in code:
            if ch == '"':
                in_str = not in_str
            elif not in_str and ch in "([":
                depth += 1
            elif not in_str and ch in ")]":
                depth -= 1
                if depth < 0:
                    problems.append(f"{n}: unbalanced closing bracket")
                    depth = 0
        if in_str:
            problems.append(f"{n}: unterminated string literal")

    if depth != 0:
        problems.append(f"file ends with {depth} unclosed bracket(s)")

    text = "\n".join(strip_comment(ln) for ln in lines)
    n_decl = len(re.findall(r"^(indicator|strategy)\(", text, re.M))
    if n_decl != 1:
        problems.append(f"expected exactly one indicator()/strategy() declaration, found {n_decl}")

    # A block-opening line must be followed by a more-indented line.
    for n, raw in enumerate(lines, 1):
        code = strip_comment(raw).rstrip()
        stripped = code.strip()
        if not stripped.startswith(BUILTIN_BLOCK_STARTERS):
            continue
        if not code.endswith(("=>",)) and stripped.startswith("if ") and " ? " in stripped:
            continue  # ternary, not a block
        indent = len(code) - len(code.lstrip(" "))
        nxt = next((ln for ln in lines[n:] if strip_comment(ln).strip()), None)
        if nxt is None:
            problems.append(f"{n}: block opener is the last statement")
            continue
        nxt_code = strip_comment(nxt)
        if (len(nxt_code) - len(nxt_code.lstrip(" "))) <= indent:
            problems.append(f"{n}: block opener not followed by an indented body")

    return problems


ok = True
for f in sys.argv[1:]:
    p = Path(f)
    issues = check(p)
    print(f"{p.name}: {'OK' if not issues else str(len(issues)) + ' problem(s)'}")
    for i in issues:
        print(f"   {i}")
    ok = ok and not issues
sys.exit(0 if ok else 1)
