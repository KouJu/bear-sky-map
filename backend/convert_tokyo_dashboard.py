"""Turn the scraped Tokyo dashboard table into GeoJSON.

The dashboard table has no coordinates, only 市町村 + 場所 as free text, so each
row goes through the same GSI geocoder (and the same shared cache) as Miyagi.

The sighting date comes from 年月日. 記載日 is the data-entry date and is kept
only as a property; using it as the sighting date would shift every point's age.

Note the municipalities are not all in Tokyo: the map covers the mountains
around the prefecture border, so 上野原市 / 小菅村 / 丹波山村 are in 山梨県.
Geocoding with the wrong prefecture would place them hundreds of km away.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import geocode_miyagi as geo  # reuse its cache + GSI client
from config import RETENTION_YEARS, clamp_future_date

DATA = Path(__file__).parent.parent / "data"
RAW_PATH = DATA / "raw" / "tokyo_dashboard.json"
OUT_PATH = DATA / "processed" / "tokyo_kuma.geojson"

# Municipalities appearing in the Tokyo dashboard that are actually in 山梨県.
YAMANASHI = {"上野原市", "小菅村", "丹波山村"}

# Some rows name two municipalities ("あきる野市、青梅市") because the sighting
# was near the border. We emit a point for each rather than picking one: the
# bear was somewhere around there, so covering both with a risk buffer errs on
# the safe side, which is the direction this map should err in.
SPLIT_MUNICIPALITY = re.compile(r"[、,／/]")

NOISE_SUFFIXES = re.compile(r"(地内|付近|周辺|地区)$")
PAREN_NOTE = re.compile(r"[（(].*?[）)]")


def parse_sighting_date(text: str) -> str | None:
    """Return 'YYYY/M/D' from the 年月日 cell.

    Power BI renders dates in the browser's locale. We pin the scraper to ja-JP
    so this is normally 「2026年9月30日」, but accept the English rendering too
    rather than silently dropping every row if that ever slips.
    """
    text = (text or "").strip()
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}/{int(m.group(2))}/{int(m.group(3))}"
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y/%-m/%-d")
        except ValueError:
            try:  # Windows uses %#m instead of %-m
                return datetime.strptime(text, fmt).strftime("%Y/%#m/%#d")
            except ValueError:
                continue
    return None


def clean_place(text: str) -> str:
    text = PAREN_NOTE.sub("", text or "")
    text = NOISE_SUFFIXES.sub("", text)
    return text.strip("　 ")


def prefecture_for(municipality: str) -> str:
    return "山梨県" if municipality in YAMANASHI else "東京都"


def run() -> dict:
    payload = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    rows = list(payload["records"].values())

    cache = geo.load_cache()
    stats = {"cache_hits": 0, "api_calls": 0}
    now = datetime.now()
    retention_cutoff = now - timedelta(days=365 * RETENTION_YEARS)

    features: list[dict] = []
    issues: list[str] = []
    dropped_old = failed = clamped_count = 0

    for row in rows:
        number = row.get("番号")
        raw_date = row.get("年月日", "")
        date = parse_sighting_date(raw_date)
        if not date:
            issues.append(f"東京: 年月日が読めない No.{number} 「{raw_date}」")
            continue

        raw_date = date
        date, clamped = clamp_future_date(date, now)
        if clamped:
            clamped_count += 1
            issues.append(f"東京: 未来日付 No.{number} {raw_date} → {date} として扱います")

        dt = datetime.strptime(date, "%Y/%m/%d")
        if dt < retention_cutoff:
            dropped_old += 1
            continue

        raw_municipality = row.get("市町村", "")
        municipalities = [m.strip() for m in SPLIT_MUNICIPALITY.split(raw_municipality) if m.strip()]
        place = clean_place(row.get("場所", ""))

        for municipality in municipalities:
            district = place[len(municipality):] if place.startswith(municipality) else place
            prefecture = prefecture_for(municipality)
            query = f"{prefecture}{municipality}{district}"
            result = geo.geocode(query, cache, stats)
            status = "district_match"
            if result is None:
                result = geo.geocode(f"{prefecture}{municipality}", cache, stats)
                status = "municipality_fallback" if result else "failed"

            if not result:
                failed += 1
                issues.append(f"東京: ジオコーディング失敗 No.{number} 「{query}」")
                continue

            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [result["lon"], result["lat"]]},
                    "properties": {
                        "number": number,
                        "date": date,
                        "date_raw": raw_date if clamped else None,
                        "recorded": row.get("記載日"),
                        "municipality": municipality,
                        "municipality_raw": raw_municipality,
                        # one sighting reported across a municipal border shows
                        # up as several points, all covering the same event
                        "split_of": len(municipalities) if len(municipalities) > 1 else None,
                        "district_raw": row.get("場所"),
                        "kind": row.get("目撃痕跡等"),
                        "accuracy": row.get("確度"),
                        "count": row.get("合計"),
                        "geocode_status": status,
                        "matched_title": result["matched_title"],
                        "source": "tokyo_metro_dashboard",
                    },
                }
            )

    geo.save_cache(cache)

    # Never replace good data with an empty file. A parsing or locale change
    # upstream would otherwise wipe the map silently, which is exactly the
    # failure this project cannot afford.
    if rows and not features:
        raise RuntimeError(
            f"{len(rows)}行を読み込んだが1件も変換できなかったため書き込みを中止しました。"
            f" 先頭の問題: {issues[0] if issues else '不明'}"
        )

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
        "cache_hits": stats["cache_hits"],
        "api_calls": stats["api_calls"],
        "scraped_at": payload.get("scraped_at"),
        "issues": issues,
    }


def main() -> None:
    r = run()
    print(f"wrote {r['features']} features to {OUT_PATH}")
    print(f"  dropped {r['dropped_old']} older than {RETENTION_YEARS}y, {r['skipped']} failed")
    print(f"  geocoding: {r['api_calls']} API calls, {r['cache_hits']} from cache")
    for issue in r["issues"][:10]:
        print(f"  [DATA QUALITY] {issue}")


if __name__ == "__main__":
    main()
