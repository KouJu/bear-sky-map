"""Fail if a prefecture has data on the map but no credit in the notice.

Adding a prefecture touches config.py, a converter and the frontend, and it is
easy to finish all three and forget the attribution — which is what happened
with 埼玉 and 神奈川. Crediting the source is a condition of using this data,
so it should not depend on remembering.
"""
import re
import sys
from pathlib import Path

from config import REGION_PREFECTURE

NOTICE = Path(__file__).parent.parent / "frontend" / "index.html"


def main() -> int:
    html = NOTICE.read_text(encoding="utf-8")
    # the sources block lives inside the notice; look there only
    m = re.search(r'<div class="sources">(.*?)</div>', html, re.S)
    if not m:
        print("[ERROR] index.html に出典ブロック (.sources) が見つかりません")
        return 1
    sources = m.group(1)

    missing = [p for p in REGION_PREFECTURE.values() if p not in sources]
    for name in missing:
        print(f"[ERROR] {name} のデータを使っているのに出典に記載がありません")
    if missing:
        return 1

    print(f"OK: {len(REGION_PREFECTURE)}県すべてが出典に記載されています")
    return 0


if __name__ == "__main__":
    sys.exit(main())
