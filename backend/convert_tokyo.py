"""Convert Tokyo bear-sighting CSV (already has lat/lon) into GeoJSON."""
import csv
import json
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"
RAW_PATH = DATA / "raw" / "tokyo_kuma.csv"
OUT_PATH = DATA / "processed" / "tokyo_kuma.geojson"


def run() -> dict:
    """Convert the CSV. Returns a summary plus any data-quality findings."""
    features = []
    issues: list[str] = []
    skipped = 0
    now = datetime.now()

    with RAW_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for line_no, row in enumerate(reader, start=2):
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, TypeError, ValueError):
                skipped += 1
                issues.append(f"東京: 座標が読めない行 (CSV {line_no}行目, number={row.get('number')})")
                continue

            date = row.get("date")
            if date:
                try:
                    if datetime.strptime(date, "%Y/%m/%d") > now:
                        issues.append(f"東京: 未来日付 number={row.get('number')} {date}")
                except ValueError:
                    issues.append(f"東京: 日付の形式が不正 number={row.get('number')} 「{date}」")

            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "number": row.get("number"),
                        "date": date,
                        "accuracy": row.get("accuracy"),
                        "kind": row.get("sightings, traces, etc."),
                        "source": "tokyo_metro_kankyo",
                    },
                }
            )

    geojson = {"type": "FeatureCollection", "features": features}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # compact (no indent): roughly halves the file the browser has to download
    OUT_PATH.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    return {"features": len(features), "skipped": skipped, "issues": issues}


def main() -> None:
    r = run()
    print(f"wrote {r['features']} features to {OUT_PATH} (skipped {r['skipped']} rows)")
    for issue in r["issues"]:
        print(f"  [DATA QUALITY] {issue}")


if __name__ == "__main__":
    main()
