#!/usr/bin/env python3
"""check-tui-jsx.py -- static guard for OpenTUI/Opentui JSX mistakes in TUI plugins.

The OpenCode v2 TUI renders plugin JSX through @opentui/solid. Two rules of that
renderer are not obvious from the JSX and fail at *render* time (i.e. inside the
TUI, not at load time), which is why they keep slipping through review:

  R1  A <text> element is a TextRenderable. Its children may only be strings,
      TextNodeRenderables or StyledText. Nesting a <text> inside a <text> makes
      the renderer call TextNodeRenderable.add() with a TextRenderable, which
      throws:

        TextNodeRenderable only accepts strings, TextNodeRenderable instances,
        or StyledText instances

      and kills the whole slot (the plugin renders nothing at all).
      Inline fragments inside a <text> must use <span>, which IS a
      TextNodeRenderable.

  R2  A <span> (and the other inline spans) ignores a bare fg=/bg= prop; the
      colour has to go through style={{ fg: "..." }}.

  R3  style={{ fontWeight: "bold" }} is silently ignored -- the style object
      understands bold/italic/underline/dim, not fontWeight. Use <strong>.

Usage:
    check-tui-jsx.py <file.tsx> [more.tsx ...]
    check-tui-jsx.py agent/opencode/plugins/*/tui.tsx

Exit code 0 = clean, 1 = problems found.
"""

import re
import sys

# Intrinsic elements of the Opentui Solid renderer (@opentui/solid catalogue).
# Only these names participate in the nesting analysis, so TypeScript generics
# like Promise<string> are never mistaken for JSX.
INTRINSIC = {
    "box", "text", "input", "select", "textarea", "ascii_font", "tab_select",
    "scrollbox", "code", "diff", "line_number", "markdown", "image",
    "span", "strong", "b", "em", "i", "u", "br", "a",
}

# Inline spans: colour must come through style={{ fg: ... }}.
SPANS = {"span", "strong", "b", "em", "i", "u", "a", "br"}

TAG_OPEN = re.compile(r"^<\s*([A-Za-z_][\w.-]*)")


class Problem:
    def __init__(self, path, line, rule, message):
        self.path = path
        self.line = line
        self.rule = rule
        self.message = message

    def __str__(self):
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def skip_string(src, i):
    """Return the index just past the string literal starting at src[i]."""
    quote = src[i]
    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return i


def read_tag(src, i):
    """Read the JSX tag starting at src[i] ('<'). Returns (tag_text, end_index)."""
    n = len(src)
    j = i + 1
    depth = 0
    while j < n:
        c = src[j]
        if c in ('"', "'", "`"):
            j = skip_string(src, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth <= 0:
            return src[i:j + 1], j + 1
        j += 1
    return src[i:], n


def attrs_of(tag_text):
    """Attribute names of an opening tag, lowercased."""
    body = tag_text[1:-1]  # drop < and >
    body = re.sub(r"\{\{.*?\}\}", " ", body, flags=re.DOTALL)  # drop {...} expressions
    names = []
    for m in re.finditer(r"([A-Za-z_][\w:-]*)\s*(=|\s|$)", body):
        name = m.group(1)
        if name in ("true", "false", "null", "undefined"):
            continue
        names.append(name)
    return names


def style_body(tag_text):
    """The object literal of a style={{ ... }} attribute, if present."""
    m = re.search(r"style\s*=\s*\{\s*\{(.*?)\}\s*\}", tag_text, flags=re.DOTALL)
    return m.group(1) if m else None


def check_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()

    problems = []
    stack = []  # [(name, line)]
    i = 0
    n = len(src)
    line = 1

    while i < n:
        c = src[i]

        if c == "\n":
            line += 1
            i += 1
            continue

        # comments
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            end = n if j == -1 else j + 2
            line += src.count("\n", i, end)
            i = end
            continue

        # string / template literals
        if c in ('"', "'", "`"):
            end = skip_string(src, i)
            line += src.count("\n", i, end)
            i = end
            continue

        # JSX tag
        if c == "<" and i + 1 < n and (src[i + 1].isalpha() or src[i + 1] == "/"):
            tag_text, end = read_tag(src, i)

            if src[i + 1] == "/":  # closing tag
                m = re.match(r"^</\s*([A-Za-z_][\w.-]*)", tag_text)
                name = m.group(1) if m else ""
                if name in INTRINSIC:
                    for k in range(len(stack) - 1, -1, -1):
                        if stack[k][0] == name:
                            del stack[k:]
                            break
                line += tag_text.count("\n")
                i = end
                continue

            m = TAG_OPEN.match(tag_text)
            name = m.group(1) if m else ""
            if name not in INTRINSIC:
                line += tag_text.count("\n")
                i = end
                continue

            tag_line = line
            self_closing = tag_text.rstrip().endswith("/>")

            # R1 -- <text> inside <text>
            if name == "text" and any(entry[0] == "text" for entry in stack):
                parent = next(e for e in stack if e[0] == "text")
                problems.append(Problem(
                    path, tag_line, "R1",
                    f"<text> nested inside <text> (opened at line {parent[1]}). "
                    "Use <span> for inline fragments -- a <text> inside a <text> "
                    "throws \"TextNodeRenderable only accepts strings, "
                    "TextNodeRenderable instances, or StyledText instances\"."))

            # R2 -- bare fg/bg on an inline span
            if name in SPANS:
                for attr in attrs_of(tag_text):
                    if attr in ("fg", "bg"):
                        problems.append(Problem(
                            path, tag_line, "R2",
                            f"<{name}> ignores a bare {attr}= prop. "
                            f"Use style={{{{ {attr}: \"#rrggbb\" }}}} instead."))

            # R3 -- fontWeight is not a style attribute
            body = style_body(tag_text)
            if body and re.search(r"\bfontWeight\b|\bfontStyle\b", body):
                problems.append(Problem(
                    path, tag_line, "R3",
                    "style={{ fontWeight: ... }} is ignored. The style object "
                    "understands bold/italic/underline/dim -- use <strong> for bold."))

            if not self_closing:
                stack.append((name, tag_line))

            line += tag_text.count("\n")
            i = end
            continue

        i += 1

    return problems


def main(argv):
    paths = [a for a in argv[1:] if not a.startswith("-")]
    if not paths:
        print("usage: check-tui-jsx.py <file.tsx> [...]", file=sys.stderr)
        return 2

    problems = []
    for path in paths:
        try:
            problems.extend(check_file(path))
        except OSError as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return 2

    for p in problems:
        print(p)

    if problems:
        print(f"FAIL: {len(problems)} TUI JSX problem(s) found", file=sys.stderr)
        return 1

    print(f"OK: {len(paths)} TUI plugin file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
