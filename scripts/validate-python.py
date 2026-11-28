#!/usr/bin/env python3
"""Compile and lint every repository Python source, including shebang entrypoints.

Run locally and in CI with ``python3 scripts/validate-python.py``. Ruff and mypy
are development tools only; neither is part of the privileged runtime.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
RUFF_RULES = "F821"
MYPY_CHECK_UNTYPED_SCOPE = ("lib/vm/profile-control-core.py", "lib/vm/profile-control.py")
MYPY_STRICT_SCOPE = ("lib/vm/profile-control-core.py",)
PYTHON_SHEBANG = re.compile(rb"^#!.*\b(?:python|pypy)(?:[0-9]+(?:\.[0-9]+)*)?(?:\b|$)",
                            re.IGNORECASE)


def discover_python_sources(root: pathlib.Path) -> list[pathlib.Path]:
    """Find tracked, staged, and non-ignored untracked Python sources."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sources = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = pathlib.Path(os.fsdecode(raw_path))
        path = root / relative
        if not path.is_file():
            continue
        if relative.suffix in {".py", ".pyi"}:
            sources.append(relative)
            continue
        with path.open("rb") as source:
            first_line = source.readline(256)
        if PYTHON_SHEBANG.search(first_line):
            sources.append(relative)
    return sorted(sources, key=os.fspath)


def validate_sources(root: pathlib.Path, sources: list[pathlib.Path],
                     ruff: str) -> list[str]:
    """Compile sources and run Ruff's stable undefined-name rule on each one."""
    diagnostics = []
    regular_sources = []
    shebang_sources = []
    for relative in sources:
        path = root / relative
        try:
            contents = path.read_bytes()
            compile(contents, os.fspath(relative), "exec", dont_inherit=True)
        except (OSError, SyntaxError, UnicodeError) as error:
            diagnostics.append(f"Python compile failed for {relative}: {error}")
            continue
        if relative.suffix in {".py", ".pyi"}:
            regular_sources.append(relative)
        else:
            shebang_sources.append((relative, contents))

    commands = []
    if regular_sources:
        commands.append((
            [ruff, "check", "--select", RUFF_RULES, *(os.fspath(path) for path in regular_sources)],
            None,
        ))
    for relative, contents in shebang_sources:
        commands.append((
            [ruff, "check", "--select", RUFF_RULES,
             "--stdin-filename", os.fspath(relative), "-"],
            contents,
        ))

    for command, contents in commands:
        result = subprocess.run(command, cwd=root, input=contents,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                check=False)
        if result.returncode:
            diagnostics.append(result.stdout.decode("utf-8", errors="replace").rstrip())
    return diagnostics


def check_extensionless_f821_regression(ruff: str) -> str | None:
    """Prove the shared discovery and Ruff path rejects an undefined shebang source."""
    try:
        with tempfile.TemporaryDirectory(prefix="lm-python-validation-") as temporary:
            root = pathlib.Path(temporary)
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(root)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            source = root / "python-entrypoint"
            source.write_text(
                "#!/usr/bin/env python3\nvalidation_regression_undefined_name()\n",
                encoding="utf-8")
            source.chmod(0o755)

            sources = discover_python_sources(root)
            if sources != [pathlib.Path("python-entrypoint")]:
                return f"Extensionless F821 regression did not discover its entrypoint: {sources}"
            diagnostics = validate_sources(root, sources, ruff)
    except (OSError, subprocess.CalledProcessError) as error:
        return f"Extensionless F821 regression could not run: {error}"

    if not any("F821" in diagnostic and "validation_regression_undefined_name" in diagnostic
               for diagnostic in diagnostics):
        return f"Extensionless F821 regression was not rejected: {diagnostics}"
    return None


def main() -> int:
    try:
        sources = discover_python_sources(ROOT)
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        return 2

    ruff = shutil.which("ruff")
    mypy = shutil.which("mypy")
    missing = [name for name, value in (("ruff", ruff), ("mypy", mypy)) if value is None]
    if missing:
        print(f"Python validation tools missing from PATH: {', '.join(missing)}", file=sys.stderr)
        return 2

    diagnostics = validate_sources(ROOT, sources, ruff)
    regression_failure = check_extensionless_f821_regression(ruff)
    if regression_failure:
        diagnostics.append(regression_failure)
    checks = (
        ([mypy, "--check-untyped-defs", "--no-incremental", *MYPY_CHECK_UNTYPED_SCOPE],
         "mypy check_untyped_defs"),
        ([mypy, "--check-untyped-defs", "--disallow-untyped-defs", "--no-incremental",
          *MYPY_STRICT_SCOPE], "mypy strict definitions"),
    )
    for command, label in checks:
        result = subprocess.run(
            command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            diagnostics.append(f"{label} failed:\n{result.stdout.rstrip()}")
    if diagnostics:
        for diagnostic in diagnostics:
            print(diagnostic, file=sys.stderr)
        return 1

    print(f"Python validation passed: {len(sources)} sources compiled and Ruff {RUFF_RULES} checked.")
    print("Extensionless Python F821 regression passed.")
    print(f"mypy --check-untyped-defs passed for: {', '.join(MYPY_CHECK_UNTYPED_SCOPE)}")
    print(f"mypy --disallow-untyped-defs passed for: {', '.join(MYPY_STRICT_SCOPE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
