"""Fail if requirements.txt and the actual imports have drifted apart.

A package installed by hand during development but never added to
requirements.txt works fine locally and then crashes CI on the first import —
which is exactly how the first deploy failed (pypdf). This catches that before
a push rather than after.
"""
import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).parent
REQUIREMENTS = BACKEND / "requirements.txt"

# modules that ship with Python, so they never belong in requirements.txt
STDLIB = set(sys.stdlib_module_names)
# import name -> distribution name, where the two differ
DISTRIBUTION_NAME = {"PIL": "pillow", "yaml": "pyyaml", "dateutil": "python-dateutil"}


def imported_packages() -> set[str]:
    found: set[str] = set()
    for path in BACKEND.glob("*.py"):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    local = {p.stem for p in BACKEND.glob("*.py")}
    return {DISTRIBUTION_NAME.get(m, m) for m in found - STDLIB - local}


def declared_packages() -> set[str]:
    names = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(line.split("==")[0].split(">=")[0].strip().lower())
    return names


def main() -> int:
    used, declared = imported_packages(), declared_packages()
    missing = {p for p in used if p.lower() not in declared}
    unused = {p for p in declared if p not in {u.lower() for u in used}}

    for name in sorted(missing):
        print(f"[ERROR] {name} を import しているが requirements.txt にありません")
    for name in sorted(unused):
        print(f"[WARN ] {name} は requirements.txt にあるが import されていません")
    if not missing and not unused:
        print(f"OK: {len(declared)}個の依存関係が import と一致しています")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
