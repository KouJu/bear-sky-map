"""Geocode Miyagi bear-sighting text addresses (municipality + free-text district)
using the GSI (国土地理院) address search API, and report match accuracy.

This is the core technical risk called out in the requirements: turning loose
text like "栗原市高清水宿の沢　地内" into usable coordinates.

Results are cached by query string so a scheduled re-run only pays the API cost
for records that are actually new (a full cold run is ~15 minutes, a warm
incremental run is seconds).
"""
import argparse
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl
import requests

from config import RETENTION_YEARS, clamp_future_date

DATA = Path(__file__).parent.parent / "data"
RAW_PATH = DATA / "raw" / "miyagi_kuma.xlsx"
OUT_GEOJSON = DATA / "processed" / "miyagi_kuma.geojson"
OUT_REPORT = DATA / "processed" / "miyagi_geocode_report.json"
CACHE_PATH = DATA / "processed" / "geocode_cache.json"

GSI_ENDPOINT = "https://msearch.gsi.go.jp/address-search/AddressSearch"
SHEET_NAME = "公表用シート（報告順）"
DATA_START_ROW = 5  # 1-indexed openpyxl row where actual records begin
REQUEST_INTERVAL_SEC = 0.4
REIWA_EPOCH = 2018  # 令和1年 = 2019

# Trailing noise words that are not part of a formal address and tend to
# confuse the geocoder if left in ("地内", "付近", "地区", parenthetical notes).
NOISE_SUFFIXES = re.compile(r"(地内|付近|周辺|地区)$")
PAREN_NOTE = re.compile(r"[（(].*?[）)]")
REIWA_DATE = re.compile(r"令和(\d+)年\s*(\d+)月")


def fiscal_year_from_today() -> int:
    """Japanese fiscal year (April-March) containing today."""
    today = datetime.now()
    return today.year if today.month >= 4 else today.year - 1


def detect_fiscal_year(ws) -> int:
    """Read the snapshot date out of the sheet header to pin the fiscal year.

    The sheet only stores month/day per record, so the year has to come from
    somewhere; the header carries e.g. "（令和8年8月13日9時時点）". A snapshot
    taken in Jan-Mar still belongs to the previous fiscal year.
    """
    for row in ws.iter_rows(min_row=1, max_row=4, values_only=True):
        for cell in row:
            if not isinstance(cell, str):
                continue
            m = REIWA_DATE.search(cell)
            if m:
                western = REIWA_EPOCH + int(m.group(1))
                month = int(m.group(2))
                return western if month >= 4 else western - 1
    return fiscal_year_from_today()


def pick_sheet(wb):
    """Tolerate small renames of the published sheet."""
    if SHEET_NAME in wb.sheetnames:
        return wb[SHEET_NAME]
    for name in wb.sheetnames:
        if "報告順" in name:
            return wb[name]
    return wb[wb.sheetnames[0]]


def load_rows():
    wb = openpyxl.load_workbook(RAW_PATH, data_only=True)
    ws = pick_sheet(wb)
    fiscal_year = detect_fiscal_year(ws)

    rows = []
    for row in ws.iter_rows(min_row=DATA_START_ROW, values_only=True):
        number, month, day, time_, office, municipality, district, count, kind = row[:9]
        if number is None or municipality is None:
            continue
        date = None
        try:
            m, d = int(str(month).strip()), int(str(day).strip())
            year = fiscal_year if m >= 4 else fiscal_year + 1
            date = f"{year}/{m}/{d}"
        except (TypeError, ValueError):
            pass
        rows.append(
            {
                "number": number,
                "date": date,
                "month": str(month).strip() if month else None,
                "day": day,
                "time": time_,
                "office": office,
                "municipality": municipality.strip() if municipality else None,
                "district": district.strip() if district else None,
                "count": count,
                "kind": kind,
            }
        )
    return rows, fiscal_year


def clean_district(text: str) -> str:
    text = PAREN_NOTE.sub("", text)
    text = NOISE_SUFFIXES.sub("", text)
    return text.strip("　 ")


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def geocode(address: str, cache: dict, stats: dict) -> dict | None:
    """Look up an address, using the on-disk cache before hitting the API."""
    if address in cache:
        stats["cache_hits"] += 1
        return cache[address]

    resp = requests.get(GSI_ENDPOINT, params={"q": address}, timeout=10)
    resp.raise_for_status()
    results = resp.json()
    stats["api_calls"] += 1

    result = None
    if results:
        top = results[0]
        lon, lat = top["geometry"]["coordinates"]
        result = {
            "matched_title": top["properties"]["title"],
            "lat": lat,
            "lon": lon,
            "candidate_count": len(results),
        }
    cache[address] = result
    time.sleep(REQUEST_INTERVAL_SEC)  # only throttle real API calls
    return result


def run(limit: int | None = None) -> dict:
    """Geocode the workbook into GeoJSON. Returns a summary + quality issues."""
    rows, fiscal_year = load_rows()
    if limit:
        rows = rows[:limit]
    cache = load_cache()
    stats = {"cache_hits": 0, "api_calls": 0}

    features, log = [], []
    ok_district = ok_municipality_only = failed = dropped_old = clamped_count = 0
    issues: list[str] = []
    now = datetime.now()
    retention_cutoff = now - timedelta(days=365 * RETENTION_YEARS)

    for row in rows:
        # drop history beyond the retention window before spending any API call
        if row["date"]:
            try:
                if datetime.strptime(row["date"], "%Y/%m/%d") < retention_cutoff:
                    dropped_old += 1
                    continue
            except ValueError:
                pass

        municipality = row["municipality"]
        district_raw = row["district"] or ""
        district = clean_district(district_raw)
        # some rows repeat the municipality name inside the free-text district
        # field (e.g. municipality="登米市", district="登米市中田町浅水");
        # keeping both would send a malformed query to the geocoder
        if district.startswith(municipality):
            district = district[len(municipality):]

        full_address = f"宮城県{municipality}{district}"
        result = geocode(full_address, cache, stats)
        status = "district_match"

        if result is None:
            # fall back to municipality-level centroid so the point still lands
            # roughly in the right place instead of being dropped entirely
            result = geocode(f"宮城県{municipality}", cache, stats)
            status = "municipality_fallback" if result else "failed"

        date, clamped = clamp_future_date(row["date"], now)
        if clamped:
            clamped_count += 1
            issues.append(
                f"宮城: 未来日付 No.{row['number']} {row['date']} "
                f"({municipality}{district_raw}) → {date} として扱います"
            )

        if result:
            if status == "district_match":
                ok_district += 1
            else:
                ok_municipality_only += 1
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [result["lon"], result["lat"]]},
                    "properties": {
                        "number": row["number"],
                        "date": date,
                        "date_raw": row["date"] if clamped else None,
                        "municipality": municipality,
                        "district_raw": district_raw,
                        "kind": row["kind"],
                        "geocode_status": status,
                        "matched_title": result["matched_title"],
                        "source": "miyagi_pref",
                    },
                }
            )
        else:
            failed += 1
            issues.append(f"ジオコーディング失敗: No.{row['number']} 「{full_address}」")

        if row["date"] is None:
            issues.append(f"日付が読めない行: No.{row['number']} (月={row['month']}, 日={row['day']})")

        log.append(
            {
                "number": row["number"],
                "query": full_address,
                "status": status,
                "matched_title": result["matched_title"] if result else None,
            }
        )

    save_cache(cache)

    geojson = {"type": "FeatureCollection", "features": features}
    OUT_GEOJSON.parent.mkdir(parents=True, exist_ok=True)
    # compact (no indent): roughly halves the file the browser has to download
    OUT_GEOJSON.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")

    total = len(rows) - dropped_old  # rows actually geocoded
    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "fiscal_year": fiscal_year,
        "sample_size": total,
        "district_level_match": ok_district,
        "municipality_fallback": ok_municipality_only,
        "failed": failed,
        "district_match_rate": round(ok_district / total, 3) if total else 0,
        "any_coordinate_rate": round((ok_district + ok_municipality_only) / total, 3) if total else 0,
        "cache_hits": stats["cache_hits"],
        "api_calls": stats["api_calls"],
        "dropped_old": dropped_old,
        "clamped_future": clamped_count,
        "issues": issues,
        "log": log,
    }
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="only geocode the first N rows")
    args = parser.parse_args()

    r = run(limit=args.limit)
    total = r["sample_size"]
    print(f"fiscal year {r['fiscal_year']}, {total} records "
          f"(cache hits {r['cache_hits']}, API calls {r['api_calls']})")
    print(f"district-level match: {r['district_level_match']}/{total} ({r['district_match_rate']:.1%})")
    print(f"municipality-only fallback: {r['municipality_fallback']}/{total}")
    print(f"failed entirely: {r['failed']}/{total}")
    for issue in r["issues"]:
        print(f"  [DATA QUALITY] {issue}")
    print(f"wrote {OUT_GEOJSON}")


if __name__ == "__main__":
    main()
