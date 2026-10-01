"""Scrape the Tokyo bear-sighting table out of the published Power BI dashboard.

Why this exists: 東京都 publishes a CSV with exact coordinates, but that export
has been frozen at 2026-06-10 while the dashboard behind
「TOKYOくまっぷ」 keeps receiving new sightings. Using only the CSV leaves the
map four months stale, which for a bear-risk map is worse than a coordinate
that is a few hundred metres off.

What it produces: data/raw/tokyo_dashboard.json, a dict keyed by 番号 holding the
raw table rows. Coordinates are NOT in the table, so these rows go through the
same GSI geocoder the Miyagi data uses.

Two important column distinctions, easy to get backwards:
  年月日 = when the bear was seen  -> this is the sighting date we publish
  記載日 = when the record was typed in -> only used to decide where to stop
                                           during an incremental run

Incremental runs rely on the table's default sort being 記載日 descending, so
new records appear at the top. We stop once we have seen a run of 番号 we
already hold. Records can still be corrected after the fact, so --full should
be run periodically (monthly is plenty).
"""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

DATA = Path(__file__).parent.parent / "data"
OUT_PATH = DATA / "raw" / "tokyo_dashboard.json"

DASHBOARD_URL = (
    "https://app.powerbi.com/view?r=eyJrIjoiMTI3ZWMwMzktODJkZS00NzMxLWI0MTctOTVl"
    "ODBhMjEzMGY0IiwidCI6ImQwMzAyZmNjLTNlODEtNDljMy04MjM1LWQzMTFhMzY4NGNmYyJ9"
)
COLUMNS = [
    "番号", "年月日", "確認時間", "市町村", "場所",
    "成獣", "幼獣", "不明", "合計", "目撃痕跡等", "確度", "記載日",
]
# stop an incremental run after this many consecutive already-known records
KNOWN_RUN_TO_STOP = 30

READ_ROWS_JS = """
(columns) => {
  const out = [];
  for (const r of document.querySelectorAll('[role="row"]')) {
    const cells = [...r.querySelectorAll('[role="gridcell"]')].map(c => c.textContent.trim());
    if (cells.length < columns.length + 1) continue;   // header or partial row
    const vals = cells.slice(1);                        // drop the "行の選択" cell
    const o = {};
    columns.forEach((k, i) => o[k] = vals[i] ?? '');
    if (/^\\d+$/.test(o['番号'])) out.push(o);
  }
  return out;
}
"""

# The published default leaves some years unticked (2026 was unticked as of
# 2026-10), so every year has to be ticked explicitly or recent data is missing.
SELECT_ALL_YEARS_JS = """
() => {
  const items = [...document.querySelectorAll('[role="treeitem"]')]
    .filter(e => /^\\d{4}年$/.test(e.textContent.trim()));
  const clicked = [];
  for (const ti of items) {
    if (ti.getAttribute('aria-selected') === 'true') continue;
    const box = ti.querySelector('.slicerCheckbox') || ti;
    const r = box.getBoundingClientRect();
    const opts = { bubbles: true, cancelable: true, view: window,
                   clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      box.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(type, opts));
    }
    clicked.push(ti.textContent.trim());
  }
  return clicked;
}
"""


def load_existing() -> dict:
    if OUT_PATH.exists():
        try:
            return json.loads(OUT_PATH.read_text(encoding="utf-8")).get("records", {})
        except json.JSONDecodeError:
            return {}
    return {}


def parse_sighting_date(text: str) -> str | None:
    """「2026年9月30日」 -> 「2026/9/30」 (the date the bear was seen)."""
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text or "")
    return f"{m.group(1)}/{int(m.group(2))}/{int(m.group(3))}" if m else None


def scrape(incremental: bool, headless: bool = True, timeout_ms: int = 60000) -> dict:
    known = load_existing() if incremental else {}
    records = dict(known)
    new_count = 0
    consecutive_known = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        # Power BI formats dates per the browser locale. Without this the table
        # renders "Thursday, October 01, 2026" instead of 「2026年10月1日」 and
        # every row fails to parse downstream.
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()
        page.goto(DASHBOARD_URL, wait_until="load", timeout=timeout_ms)
        page.wait_for_selector('[role="treeitem"]', timeout=timeout_ms)
        page.wait_for_timeout(6000)  # let the visuals finish their first render

        # switch from the map to the table view
        page.get_by_text("一覧表", exact=True).first.click()
        page.wait_for_selector('[role="gridcell"]', timeout=timeout_ms)
        page.wait_for_timeout(3000)

        ticked = page.evaluate(SELECT_ALL_YEARS_JS)
        if ticked:
            print(f"  年フィルターを追加選択: {', '.join(ticked)}")
            page.wait_for_timeout(6000)

        total_text = page.inner_text("body")
        m = re.search(r"表示件数\s*([\d,]+)", total_text)
        expected = int(m.group(1).replace(",", "")) if m else None
        print(f"  表示件数: {expected}")

        viewport = page.locator(".mid-viewport")
        viewport.wait_for(timeout=timeout_ms)

        stuck = 0
        while True:
            for row in page.evaluate(READ_ROWS_JS, COLUMNS):
                number = row["番号"]
                if number in records:
                    consecutive_known += 1
                else:
                    records[number] = row
                    new_count += 1
                    consecutive_known = 0

            if incremental and consecutive_known >= KNOWN_RUN_TO_STOP:
                print(f"  既知の記録が{consecutive_known}件続いたため打ち切り")
                break

            moved = page.evaluate(
                """() => {
                  const vp = document.querySelector('.mid-viewport');
                  const before = vp.scrollTop;
                  vp.scrollTop += vp.clientHeight - 60;
                  return { before, after: vp.scrollTop,
                           atBottom: vp.scrollTop + vp.clientHeight >= vp.scrollHeight - 5 };
                }"""
            )
            page.wait_for_timeout(250)
            if moved["after"] <= moved["before"] + 1:
                stuck += 1
                if stuck > 4:
                    break
            else:
                stuck = 0

        browser.close()

    if expected and not incremental and len(records) != expected:
        print(f"  [WARN] 取得 {len(records)}件 が表示件数 {expected}件 と一致しません")

    payload = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "source": DASHBOARD_URL,
        "expected_rows": expected,
        "records": records,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"total": len(records), "new": new_count, "expected": expected}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                        help="re-read every row instead of stopping at known records")
    parser.add_argument("--show", action="store_true", help="run with a visible browser")
    args = parser.parse_args()

    r = scrape(incremental=not args.full, headless=not args.show)
    print(f"保存: {OUT_PATH}")
    print(f"  合計 {r['total']}件 (新規 {r['new']}件)")


if __name__ == "__main__":
    main()
