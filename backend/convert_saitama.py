"""Convert the Saitama ArcGIS feature service output into our GeoJSON shape.

This is the easiest of the three sources: the service already returns point
geometry in WGS84, so no geocoding is involved and the coordinates are the
ones the prefecture itself published rather than ones we inferred from text.

Dates arrive as epoch milliseconds. The service stores them at midday JST, so
the calendar date is unambiguous, but we still convert through JST explicitly
rather than relying on that — a record stored at local midnight would
otherwise land on the previous day.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import RETENTION_YEARS, clamp_future_date

DATA = Path(__file__).parent.parent / "data"
RAW_PATH = DATA / "raw" / "saitama_arcgis.geojson"
OUT_PATH = DATA / "processed" / "saitama_kuma.geojson"

JST = timezone(timedelta(hours=9))

# field_N names come from the Survey123 form behind the service
F_DATE = "field_1"        # 出没日
F_TIME = "field_2"        # 出没時間
F_MUNICIPALITY = "field_4"  # 市町村名
F_PLACE = "field_6"       # 地名
F_SUMMARY = "field_9"     # 出没状況の概要
F_COUNT = "field_10"      # 出没頭数
F_DAMAGE = "field_11"     # 被害状況
F_LEVEL = "field_17"      # 出没レベル


def epoch_ms_to_date(ms) -> str | None:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, JST)
    except (TypeError, ValueError, OSError):
        return None
    return f"{dt.year}/{dt.month}/{dt.day}"


def run() -> dict:
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    rows = raw.get("features", [])

    now = datetime.now()
    retention_cutoff = now - timedelta(days=365 * RETENTION_YEARS)

    features: list[dict] = []
    issues: list[str] = []
    dropped_old = no_date = no_geom = clamped_count = 0

    for row in rows:
        props = row.get("properties", {}) or {}
        geom = row.get("geometry") or {}
        coords = geom.get("coordinates")
        if geom.get("type") != "Point" or not coords or coords[0] is None:
            no_geom += 1
            issues.append(f"埼玉: 座標のない記録 (objectid付近 {props.get(F_PLACE)})")
            continue

        date = epoch_ms_to_date(props.get(F_DATE))
        if not date:
            no_date += 1
            continue

        raw_date = date
        date, clamped = clamp_future_date(date, now)
        if clamped:
            clamped_count += 1
            issues.append(f"埼玉: 未来日付 {raw_date} ({props.get(F_PLACE)}) → {date} として扱います")

        if datetime.strptime(date, "%Y/%m/%d") < retention_cutoff:
            dropped_old += 1
            continue

        municipality = (props.get(F_MUNICIPALITY) or "").strip()
        place = (props.get(F_PLACE) or "").strip()
        # 地名 often repeats the municipality ("秩父市" + "秩父市荒川…")
        if municipality and place.startswith(municipality):
            place = place[len(municipality):]

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [coords[0], coords[1]]},
                "properties": {
                    "date": date,
                    "date_raw": raw_date if clamped else None,
                    "municipality": municipality,
                    "district_raw": place,
                    "kind": props.get(F_LEVEL),
                    "time": props.get(F_TIME),
                    "count": props.get(F_COUNT),
                    "damage": props.get(F_DAMAGE),
                    "summary": props.get(F_SUMMARY),
                    # published coordinates, not geocoded from text
                    "geocode_status": "published_coordinates",
                    "source": "saitama_pref_arcgis",
                },
            }
        )

    if rows and not features:
        raise RuntimeError(
            f"{len(rows)}件読み込んだが1件も変換できなかったため書き込みを中止しました"
        )

    if no_date:
        issues.append(f"埼玉: 出没日のない記録 {no_date}件")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "features": len(features),
        "skipped": no_geom,
        "dropped_old": dropped_old,
        "clamped_future": clamped_count,
        "issues": issues,
    }


def main() -> None:
    r = run()
    print(f"wrote {r['features']} features to {OUT_PATH}")
    print(f"  dropped {r['dropped_old']} older than {RETENTION_YEARS}y, {r['skipped']} without geometry")
    for issue in r["issues"][:10]:
        print(f"  [DATA QUALITY] {issue}")


if __name__ == "__main__":
    main()
