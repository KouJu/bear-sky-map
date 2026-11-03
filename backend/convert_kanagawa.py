"""Parse the Kanagawa bear-sighting PDFs into GeoJSON.

The PDFs are real text (not scans), laid out as a table:

    1 4月4日 5時30分頃 1 目撃 松田町寄 人里 ○

but a record can wrap across several lines when a field is long, and a
remarks paragraph can sit in the middle of one:

    9 5月19日 13時27分 1 捕殺
    相模原市緑区三ケ木
    有害鳥獣捕獲用のくくり罠に錯誤捕獲。…
    人里 ○

So records are split on their leading number and then flattened, rather than
parsed line by line.

Two things the source leaves vague, both resolved toward showing risk:
  - "4月下旬" style dates become the 25th (上旬→5, 中旬→15)
  - "5月14〜18日" ranges take the later day, so the point stays inside the
    90-day window as long as it plausibly could

The year is not in the table rows at all. It is taken from the 「令和N年度」
heading printed inside each PDF — deliberately not from the filename, where
the date is the publication day and genuinely misleading: kuma_r7_0330.pdf
holds 令和7年度 while kuma_r7_0331.pdf holds 令和6年度.
"""
import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from pypdf import PdfReader

import geocode_miyagi as geo  # reuse the shared GSI client + cache
from config import RETENTION_YEARS, clamp_future_date

DATA = Path(__file__).parent.parent / "data"
RAW_DIR = DATA / "raw"
OUT_PATH = DATA / "processed" / "kanagawa_kuma.geojson"

# "1 4月4日 …" — a record starts with its row number followed by a month/day
RECORD_START = re.compile(r"(?=(?:^|\n)\s*(\d{1,3})\s+\d{1,2}月)")
DATE = re.compile(r"(\d{1,2})月\s*(\d{1,2})?\s*(?:[〜～~]\s*(\d{1,2}))?\s*日?")
TEN_DAY = re.compile(r"(\d{1,2})月(上旬|中旬|下旬)")
KINDS = ("目撃", "痕跡", "撮影", "捕獲", "捕殺", "錯誤捕獲", "その他")
# every place starts with a municipality name
PLACE = re.compile(r"([^\s　]*?[市町村][^\s　]*)")
AREA = re.compile(r"(山中|人里)")
TEN_DAY_TO_DAY = {"上旬": 5, "中旬": 15, "下旬": 25}
FISCAL_HEADING = re.compile(r"令和(\d+)年度")
REIWA_EPOCH = 2018  # 令和1年 = 2019


def fiscal_year_of(text: str) -> int | None:
    """Read 「令和N年度」 from the PDF's own heading (digits may be full-width)."""
    m = FISCAL_HEADING.search(unicodedata.normalize("NFKC", text))
    return REIWA_EPOCH + int(m.group(1)) if m else None


def parse_record(text: str, fiscal_year: int) -> dict | None:
    """Pull one row out of its (possibly multi-line) chunk of PDF text."""
    flat = re.sub(r"\s+", " ", text).strip()

    month = day = None
    approximate = None
    ten = TEN_DAY.search(flat)
    if ten:
        month, day = int(ten.group(1)), TEN_DAY_TO_DAY[ten.group(2)]
        approximate = ten.group(0)
    else:
        m = DATE.search(flat)
        if not m:
            return None
        month = int(m.group(1))
        # a range like 14〜18 takes the later day: the sighting could have been
        # that recent, and keeping it in the window errs toward showing risk
        day = int(m.group(3) or m.group(2) or 0)
        if m.group(3):
            approximate = m.group(0).strip()
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None

    # fiscal year runs April-March
    year = fiscal_year if month >= 4 else fiscal_year + 1
    try:
        datetime(year, month, day)
    except ValueError:
        return None

    kind = next((k for k in KINDS if k in flat), None)
    # look for the place after the kind word so remarks text cannot win
    tail = flat.split(kind, 1)[1] if kind else flat
    place_match = PLACE.search(tail) or PLACE.search(flat)
    if not place_match:
        return None
    area = AREA.search(flat)

    return {
        "date": f"{year}/{month}/{day}",
        "place": place_match.group(1),
        "kind": kind,
        "area": area.group(1) if area else None,
        "approximate_date": approximate,
    }


def read_pdf(path: Path) -> tuple[list[dict], int, int | None]:
    """Parse one PDF. The fiscal year comes from its own heading."""
    text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    fiscal_year = fiscal_year_of(text)
    if fiscal_year is None:
        return [], 0, None
    chunks = RECORD_START.split(text)
    records: list[dict] = []
    # split() yields [prefix, number, body, number, body, ...]
    highest = 0
    for i in range(1, len(chunks) - 1, 2):
        number, body = chunks[i], chunks[i + 1]
        highest = max(highest, int(number))
        parsed = parse_record(body, fiscal_year)
        if parsed:
            parsed["number"] = number
            records.append(parsed)
    return records, highest, fiscal_year


def run() -> dict:
    now = datetime.now()
    retention_cutoff = now - timedelta(days=365 * RETENTION_YEARS)
    cache = geo.load_cache()
    stats = {"cache_hits": 0, "api_calls": 0}

    features: list[dict] = []
    issues: list[str] = []
    dropped_old = failed = clamped_count = approx_count = 0
    parsed_total = expected_total = 0

    # The prefecture republishes the current year under a new filename each
    # time it updates (kuma_r8_1013 -> kuma_r8_1102), and the superseded file
    # stays on disk. Reading both would duplicate every record of that year,
    # drawing two risk circles for one sighting, so keep only the newest file
    # per fiscal year.
    by_year: dict[int, tuple[Path, list[dict], int]] = {}
    for path in sorted(RAW_DIR.glob("kanagawa_*.pdf")):
        records, highest, fiscal_year = read_pdf(path)
        if fiscal_year is None:
            issues.append(f"神奈川: {path.name} に「令和N年度」の記載がなく年を特定できません")
            continue
        previous = by_year.get(fiscal_year)
        if previous is None or path.stat().st_mtime >= previous[0].stat().st_mtime:
            if previous is not None:
                issues.append(
                    f"神奈川: 令和{fiscal_year - 2018}年度は {path.name} を採用"
                    f"（{previous[0].name} は旧版のため除外）"
                )
            by_year[fiscal_year] = (path, records, highest)
        else:
            issues.append(
                f"神奈川: 令和{fiscal_year - 2018}年度は {previous[0].name} を採用"
                f"（{path.name} は旧版のため除外）"
            )

    for fiscal_year, (path, records, highest) in sorted(by_year.items()):
        parsed_total += len(records)
        expected_total += highest
        if highest and len(records) < highest:
            issues.append(
                f"神奈川: {fiscal_year}年度のPDFで{highest}件中{len(records)}件しか解析できませんでした"
            )

        for rec in records:
            raw_date = rec["date"]
            date, clamped = clamp_future_date(raw_date, now)
            if clamped:
                clamped_count += 1
                issues.append(f"神奈川: 未来日付 {raw_date} ({rec['place']}) → {date} として扱います")
            if datetime.strptime(date, "%Y/%m/%d") < retention_cutoff:
                dropped_old += 1
                continue
            if rec["approximate_date"]:
                approx_count += 1

            query = f"神奈川県{rec['place']}"
            result = geo.geocode(query, cache, stats)
            status = "district_match"
            if result is None:
                # strip the trailing hamlet name and retry at municipality level
                municipality = re.match(r".*?[市町村]", rec["place"])
                if municipality:
                    result = geo.geocode(f"神奈川県{municipality.group(0)}", cache, stats)
                status = "municipality_fallback" if result else "failed"
            if not result:
                failed += 1
                issues.append(f"神奈川: ジオコーディング失敗 「{query}」")
                continue

            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [result["lon"], result["lat"]]},
                    "properties": {
                        "number": f"{fiscal_year}-{rec['number']}",
                        "date": date,
                        # what the source actually printed, whenever the
                        # published date differs from it: the PDF's own
                        # wording for a vague date ("4月下旬"), or the
                        # original date when a future typo was pulled back
                        "date_raw": rec["approximate_date"] or (raw_date if clamped else None),
                        "district_raw": rec["place"],
                        "kind": rec["kind"],
                        "area": rec["area"],
                        "geocode_status": status,
                        "matched_title": result["matched_title"],
                        "source": "kanagawa_pref_pdf",
                    },
                }
            )

    geo.save_cache(cache)

    if expected_total and not features:
        raise RuntimeError(
            f"{expected_total}件を見つけたが1件も変換できなかったため書き込みを中止しました"
        )
    if approx_count:
        issues.append(f"神奈川: 日付が曖昧な記録 {approx_count}件（「下旬」「14〜18日」等を代表日に置換）")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "features": len(features),
        "skipped": failed,
        "dropped_old": dropped_old,
        "clamped_future": clamped_count,
        "approximate_dates": approx_count,
        "parsed": parsed_total,
        "expected": expected_total,
        "cache_hits": stats["cache_hits"],
        "api_calls": stats["api_calls"],
        "issues": issues,
    }


def main() -> None:
    r = run()
    print(f"wrote {r['features']} features to {OUT_PATH}")
    print(f"  解析 {r['parsed']}/{r['expected']}件, 3年超で破棄 {r['dropped_old']}件, 失敗 {r['skipped']}件")
    print(f"  ジオコーディング: API {r['api_calls']}回, キャッシュ {r['cache_hits']}回")
    for issue in r["issues"][:10]:
        print(f"  [DATA QUALITY] {issue}")


if __name__ == "__main__":
    main()
