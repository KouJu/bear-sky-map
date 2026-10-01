"""Core feature: compute "safe stargazing areas" = dark sky (SQM >= 20)
minus 2km buffers around recent bear sightings.

Dark sky comes from the Lorenz Light Pollution Atlas binary tiles
(1/120 deg ~ 900m grid, decoding scheme ported from the atlas viewer JS).
Buffers are computed in UTM zone 54N (EPSG:32654) so "2km" is real meters.

Outputs (WGS84 GeoJSON, also copied into frontend/public/data by the caller):
  data/processed/safe_area.geojson    dark-sky polygons with bear buffers cut out
  data/processed/bear_buffer.geojson  the unioned 2km risk buffers
"""
import argparse
import gzip
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import requests
import shapely
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform
import pyproj

from config import (
    BUFFER_METERS,
    LP_YEAR,
    REGION_PREFECTURE,
    RISK_WINDOW_DAYS,
    SQM_THRESHOLD,
)

DATA = Path(__file__).parent.parent / "data"
TILE_CACHE = DATA / "raw" / "lp_tiles"
PREFECTURE_PATH = DATA / "raw" / "prefectures.geojson"
GRID = 600  # points per 5-degree binary tile side (1/120 deg)

def js_round(x):
    """JS Math.round (half-up); Python/numpy round() is half-to-even."""
    return np.floor(np.asarray(x) + 0.5).astype(int)


WGS84 = pyproj.CRS("EPSG:4326")
UTM54 = pyproj.CRS("EPSG:32654")
TO_UTM = pyproj.Transformer.from_crs(WGS84, UTM54, always_xy=True).transform
TO_WGS = pyproj.Transformer.from_crs(UTM54, WGS84, always_xy=True).transform


def fetch_binary_tile(tilex: int, tiley: int) -> np.ndarray | None:
    """Return the 600x600 SQM grid for one atlas tile (row 0 = south)."""
    TILE_CACHE.mkdir(parents=True, exist_ok=True)
    cache = TILE_CACHE / f"binary_tile_{tilex}_{tiley}.dat.gz"
    if not cache.exists():
        url = (
            f"https://djlorenz.github.io/astronomy/binary_tiles/{LP_YEAR}/"
            f"binary_tile_{tilex}_{tiley}.dat.gz"
        )
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            return None  # pure-ocean tile: atlas omits it (treat as fully dark)
        resp.raise_for_status()
        cache.write_bytes(resp.content)

    raw = np.frombuffer(gzip.decompress(cache.read_bytes()), dtype=np.int8).astype(np.int64)
    first = 128 * raw[0] + raw[1]

    # value(ix, iy) = first + sum of per-row deltas raw[600*i+1] (i=1..iy-1)
    #                       + sum of in-row deltas raw[600*(iy-1)+1+i] (i=1..ix-1)
    row_deltas = raw[GRID * np.arange(1, GRID) + 1]
    row_start = first + np.concatenate([[0], np.cumsum(row_deltas)])
    grid = np.empty((GRID, GRID), dtype=np.int64)
    for iy in range(GRID):
        base = GRID * iy + 2
        in_row = np.concatenate([[0], np.cumsum(raw[base : base + GRID - 1])])
        grid[iy] = row_start[iy] + in_row

    ratio = (5.0 / 195.0) * (np.exp(0.0195 * grid) - 1.0)
    return 22.0 - 5.0 * np.log(1.0 + ratio) / np.log(100.0)


def sqm_grid_for_bbox(lon_min, lat_min, lon_max, lat_max):
    """Merge atlas tiles covering the bbox; return (sqm, lons, lats) cell centers."""
    # snap to the atlas 1/120-degree grid so our cell centers coincide
    # exactly with atlas grid points
    lon_min = math.floor((lon_min + 180) * 120) / 120 - 180
    lat_min = math.floor((lat_min + 65) * 120) / 120 - 65
    n_lon = math.ceil((lon_max - lon_min) * 120)
    n_lat = math.ceil((lat_max - lat_min) * 120)
    lons = lon_min + (np.arange(n_lon) + 0.5) / 120.0
    lats = lat_min + (np.arange(n_lat) + 0.5) / 120.0
    sqm = np.full((n_lat, n_lon), 22.0)  # missing tiles = fully dark (ocean)

    tiles = {}
    for lon in lons:
        for lat in (lats[0], lats[-1]):
            pass  # tile set computed below from corners instead
    tx_range = range(
        int(math.floor((lon_min + 180) % 360 / 5)) + 1,
        int(math.floor(((lon_max - 1e-9) + 180) % 360 / 5)) + 2,
    )
    ty_range = range(
        int(math.floor((lat_min + 65) / 5)) + 1,
        int(math.floor((lat_max - 1e-9 + 65) / 5)) + 2,
    )
    for tx in tx_range:
        for ty in ty_range:
            tiles[(tx, ty)] = fetch_binary_tile(tx, ty)

    for j, lat in enumerate(lats):
        ty = int(math.floor((lat + 65) / 5)) + 1
        iy = int(js_round(120 * (lat + 65 - 5 * (ty - 1) + 1 / 240)))
        for (tx, ty2), tile in tiles.items():
            if ty2 != ty or tile is None:
                continue
            lon_lo = 5 * (tx - 1) - 180
            sel = (lons >= lon_lo) & (lons < lon_lo + 5)
            if not sel.any():
                continue
            ix = js_round(120 * (lons[sel] - lon_lo + 1 / 240))
            sqm[j, sel] = tile[iy - 1, ix - 1]
    return sqm, lons, lats


def dark_polygon(lon_min, lat_min, lon_max, lat_max):
    """Union of all grid cells with SQM >= threshold inside the bbox."""
    sqm, lons, lats = sqm_grid_for_bbox(lon_min, lat_min, lon_max, lat_max)
    mask = sqm >= SQM_THRESHOLD
    jj, ii = np.nonzero(mask)
    if len(ii) == 0:
        return None, 0, mask.size
    half = 1 / 240.0
    boxes = shapely.box(lons[ii] - half, lats[jj] - half, lons[ii] + half, lats[jj] + half)
    union = shapely.union_all(boxes)
    union = shapely.set_precision(union.simplify(0.0005), 1e-5)
    return union, int(mask.sum()), mask.size


def prefecture_polygon(name: str):
    """Mainland outline of one prefecture, used to clip dark-sky areas to land.

    Only the largest polygon is kept. Tokyo legally extends to 南鳥島 at 153°E,
    so the full multipolygon spans a bounding box of several million grid cells
    that is almost entirely open ocean. The outlying islands are also outside
    the area the prefectures report bear sightings for, so presenting them as
    "safe" would mean inferring safety from data we simply do not have.
    """
    fc = json.loads(PREFECTURE_PATH.read_text(encoding="utf-8"))
    for feature in fc["features"]:
        if feature["properties"].get("N03_001") == name:
            geom = shape(feature["geometry"]).buffer(0)  # buffer(0) fixes self-intersections
            if geom.geom_type == "MultiPolygon":
                geom = max(geom.geoms, key=lambda g: g.area)
            return geom
    raise RuntimeError(f"prefecture not found in boundary data: {name}")


def load_points(path: Path, cutoff: datetime | None):
    fc = json.loads(path.read_text(encoding="utf-8"))
    pts, future_dates, undated = [], [], 0
    newest: datetime | None = None
    now = datetime.now()
    for f in fc["features"]:
        date_str = f["properties"].get("date")
        dt = None
        if isinstance(date_str, str):
            try:
                dt = datetime.strptime(date_str, "%Y/%m/%d")
            except ValueError:
                pass
        if dt is None:
            undated += 1
            continue
        if dt > now:
            future_dates.append((f["properties"].get("number"), date_str))
        else:
            # ignore future-dated typos when judging how fresh the feed is
            newest = dt if newest is None else max(newest, dt)
        if cutoff is None or dt >= cutoff:
            pts.append(shape(f["geometry"]))
    return pts, future_dates, undated, newest


def run(days: int = 365) -> dict:
    """Recompute safe areas. Returns a summary plus data-quality findings."""
    cutoff = datetime.now() - timedelta(days=days)
    processed = DATA / "processed"
    issues: list[str] = []
    counts: dict[str, int] = {}

    # --- bear buffers (2km, computed in meters via UTM 54N) ---
    all_pts = []
    freshness: dict[str, dict] = {}
    now = datetime.now()
    for region in REGION_PREFECTURE:
        name = f"{region}_kuma.geojson"
        pts, future, undated, newest = load_points(processed / name, cutoff)
        all_pts += pts
        counts[name] = len(pts)
        print(f"{name}: {len(pts)} points within {days} days"
              + (f", {undated} without date" if undated else ""))
        if undated:
            issues.append(f"{name}: 日付のない記録 {undated}件")
        for number, date_str in future:
            issues.append(f"{name}: 未来日付 No.{number} {date_str}")
        if future:
            print(f"  [DATA QUALITY] future-dated records in {name}: {future}")

        # A feed that has stopped updating is the most dangerous failure mode
        # here: zero recent sightings produces zero risk buffers, so the whole
        # prefecture would be presented as having few bears when in fact we
        # simply have no current information about it.
        age = (now - newest).days if newest else None
        stale = age is None or age > days
        freshness[region] = {
            "latest": newest.strftime("%Y/%m/%d") if newest else None,
            "age_days": age,
            "stale": stale,
            "recent_points": len(pts),
        }
        if stale:
            issues.append(
                f"{region}: 最新の出没記録が{age}日前で、直近{days}日の記録が{len(pts)}件。"
                f"この県のリスクエリアは実態を反映していない可能性があります"
            )
            print(f"  [DATA QUALITY] {region} feed is stale ({age} days old)")

    utm_pts = [shp_transform(TO_UTM, p) for p in all_pts]
    buffers_utm = shapely.union_all([p.buffer(BUFFER_METERS, quad_segs=16) for p in utm_pts])
    buffers = shp_transform(TO_WGS, buffers_utm)
    buffers = shapely.set_precision(buffers, 1e-5)

    # --- dark sky per region, clipped to the prefecture outline ---
    # Without the clip the result is the analysis bounding box itself, which
    # both looks like a rectangle drawn on the map and wrongly presents open
    # sea as a safe place to observe from (the atlas treats ocean as dark).
    regions = {}
    for region, prefecture in REGION_PREFECTURE.items():
        outline = prefecture_polygon(prefecture)
        lon_min, lat_min, lon_max, lat_max = outline.bounds
        m = 0.02  # small margin so the grid covers the whole outline
        dark, n_dark, n_total = dark_polygon(lon_min - m, lat_min - m, lon_max + m, lat_max + m)
        if dark is not None:
            dark = dark.intersection(outline)
        print(f"{region} ({prefecture}): dark cells {n_dark}/{n_total} "
              f"({n_dark / n_total:.1%}), clipped to prefecture")
        regions[region] = dark
        counts[f"{region}_dark_ratio"] = round(n_dark / n_total, 3) if n_total else 0

    # --- core: difference ---
    safe_features = []
    for region, dark in regions.items():
        if dark is None:
            continue
        safe = dark.difference(buffers)
        safe_features.append(
            {
                "type": "Feature",
                "geometry": mapping(safe),
                "properties": {
                    "region": region,
                    "prefecture": REGION_PREFECTURE[region],
                    "sqm_threshold": SQM_THRESHOLD,
                    "buffer_km": BUFFER_METERS / 1000,
                    "days": days,
                    "atlas_year": LP_YEAR,
                    **freshness.get(region, {}),
                },
            }
        )

    (processed / "safe_area.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": safe_features}, ensure_ascii=False),
        encoding="utf-8",
    )
    (processed / "bear_buffer.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": mapping(buffers),
                        "properties": {"buffer_km": BUFFER_METERS / 1000, "days": days},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"wrote {processed / 'safe_area.geojson'}")
    print(f"wrote {processed / 'bear_buffer.geojson'}")

    used = sum(v for k, v in counts.items() if k.endswith(".geojson"))
    return {
        "summary": f"直近{days}日の{used}地点から2kmバッファを生成",
        "counts": counts,
        "freshness": freshness,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=RISK_WINDOW_DAYS,
                        help="bear sightings lookback window")
    args = parser.parse_args()
    run(days=args.days)


def selftest() -> None:
    """Cross-check the numpy decoder against values verified in the browser."""
    cases = [
        (139.70, 35.69, 17.12, "Shinjuku"),
        (139.02, 35.85, 20.96, "Okutama"),
        (140.44, 38.14, 21.32, "Zao"),
        (140.78, 38.96, 21.77, "Kurikoma"),
    ]
    for lng, lat, expected, name in cases:
        tx = int(math.floor((lng + 180) % 360 / 5)) + 1
        ty = int(math.floor((lat + 65) / 5)) + 1
        tile = fetch_binary_tile(tx, ty)
        ix = int(js_round(120 * ((lng + 180) % 360 - 5 * (tx - 1) + 1 / 240)))
        iy = int(js_round(120 * (lat + 65 - 5 * (ty - 1) + 1 / 240)))
        got = tile[iy - 1, ix - 1]
        # points landing exactly on a cell edge may resolve to either neighbor
        # depending on float noise (JS vs Python), so accept adjacent cells too
        neighbors = [
            tile[max(iy - 1 + dy, 0), max(ix - 1 + dx, 0)]
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
        ]
        exact = abs(got - expected) < 0.005
        edge = any(abs(v - expected) < 0.005 for v in neighbors)
        status = "OK" if exact else ("OK (cell-edge neighbor)" if edge else "MISMATCH")
        print(f"  {name}: expected {expected}, got {got:.2f} -> {status}")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
