"""Merge sightings that geocoded to the same coordinate into one point.

Place names resolve to a single coordinate, so every sighting reported as
"奥多摩町原" lands on the exact same spot. Across the four prefectures 56% of
records sit underneath another one: Tokyo's 1057 sightings occupy just 133
distinct positions, with up to 76 stacked on one.

Drawn as separate circles they cannot all be seen, and which one ends up on
top decides the colour — so the same place looked "90日より前" under one
period filter and "1年より前" under another.

Merging rather than discarding the older ones keeps what matters: a place with
76 sightings is not the same as a place with one, and that difference is the
whole point of a bear map. Each merged point carries:

  date        newest sighting, which drives colour, age tag and filtering
  count       how many sightings share this position
  dates       every sighting date, so the frontend can recount under a filter
  date_oldest how far back the history at this spot goes

Buffers are unaffected: identical circles merge into the same union either way.
"""
import json
from datetime import datetime
from pathlib import Path

PROCESSED = Path(__file__).parent.parent / "data" / "processed"
# round before grouping so float noise cannot split one place into two points
PRECISION = 6


def _parse(date: str | None) -> datetime | None:
    try:
        return datetime.strptime(date, "%Y/%m/%d") if date else None
    except ValueError:
        return None


def aggregate(path: Path) -> dict:
    fc = json.loads(path.read_text(encoding="utf-8"))
    groups: dict[tuple, list[dict]] = {}
    for feature in fc["features"]:
        coords = feature["geometry"]["coordinates"]
        key = (round(coords[0], PRECISION), round(coords[1], PRECISION))
        groups.setdefault(key, []).append(feature)

    merged: list[dict] = []
    for (lon, lat), members in groups.items():
        # newest first, so members[0] supplies the descriptive fields
        members.sort(
            key=lambda f: _parse(f["properties"].get("date")) or datetime.min,
            reverse=True,
        )
        dates = [f["properties"].get("date") for f in members if f["properties"].get("date")]
        newest = members[0]["properties"]
        merged.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    **{k: v for k, v in newest.items() if v not in (None, "")},
                    "count": len(members),
                    "dates": dates,
                    "date_oldest": dates[-1] if dates else None,
                },
            }
        )

    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": merged}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "before": len(fc["features"]),
        "after": len(merged),
        "largest": max((len(v) for v in groups.values()), default=0),
    }


def run() -> dict:
    stats, issues = {}, []
    for path in sorted(PROCESSED.glob("*_kuma.geojson")):
        region = path.stem.replace("_kuma", "")
        stats[region] = aggregate(path)
    merged_away = sum(s["before"] - s["after"] for s in stats.values())
    return {"regions": stats, "merged_away": merged_away, "issues": issues}


def main() -> None:
    r = run()
    for region, s in r["regions"].items():
        print(f"{region}: {s['before']} -> {s['after']}地点 (最大 {s['largest']}件/地点)")
    print(f"合計 {r['merged_away']}件を既存地点に集約")


if __name__ == "__main__":
    main()
