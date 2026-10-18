"""Download the latest published bear-sighting data from each prefecture.

Both publishers change the file name every time they update (Miyagi embeds the
snapshot date, Tokyo embeds the export date), so the download URL is resolved
from the listing page each run rather than hard-coded.

Downloads are verified by content, not just HTTP status: a 404 page served with
status 200, or an HTML error page where a spreadsheet was expected, has bitten
this project before.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

import requests

DATA = Path(__file__).parent.parent / "data"
RAW = DATA / "raw"

# NOTE: the Miyagi page slug encodes the fiscal year ("r8" = 令和8年度) and will
# change next April. If this 404s, look for the new page under 自然保護課.
MIYAGI_PAGE = "https://www.pref.miyagi.jp/soshiki/sizenhogo/r8kumamokugeki.html"
MIYAGI_ORIGIN = "https://www.pref.miyagi.jp"
MIYAGI_LINK = re.compile(r'href="(/documents/[^"]*\.xlsx)"')

TOKYO_PAGE = "https://www.kankyo.metro.tokyo.lg.jp/nature/animals_plants/bear/data"
TOKYO_ORIGIN = "https://www.kankyo.metro.tokyo.lg.jp"
TOKYO_LINK = re.compile(r'href="(/documents/d/kankyo/tukinowaguma_source[^"]*)"')

# Prefecture outlines, used to clip the dark-sky polygons to land. This is the
# 国土交通省 N03 administrative boundary data simplified to 1% by SmartNews and
# republished as GeoJSON (the official release is a 40MB+ shapefile per
# prefecture, which would drag geopandas back into the dependency list).
# Free for any use; the 国土交通省 credit is required and is shown in the app.
PREFECTURE_URL = (
    "https://raw.githubusercontent.com/smartnews-smri/japan-topography"
    "/main/data/municipality/geojson/s0010/prefectures.json"
)
PREFECTURE_NAME_KEY = "N03_001"

# 埼玉県's map is an ArcGIS app, and ArcGIS apps are backed by a public REST
# service. Querying it directly gives GeoJSON with real coordinates, so unlike
# Tokyo there is nothing to scrape and nothing to geocode.
SAITAMA_SERVICE = (
    "https://services9.arcgis.com/n65w8AXGaYPTqFYI/arcgis/rest/services"
    "/survey123_3123e5ed452d4e89845e4ba6129c1e2d_results/FeatureServer/0"
)
SAITAMA_FIELDS = "field_1,field_2,field_4,field_6,field_9,field_10,field_11,field_17"
SAITAMA_PAGE = 1000  # the service's maxRecordCount

# 神奈川県 publishes one PDF per fiscal year. The filename carries the
# publication date, NOT the year the data covers — kuma_r7_0330.pdf is 令和7年度
# while kuma_r7_0331.pdf is 令和6年度 — so the filename must never be used to
# date the contents. The link label is only used to find which PDFs to take;
# convert_kanagawa reads the authoritative year out of each PDF's own text.
KANAGAWA_PAGE = "https://www.pref.kanagawa.jp/docs/t4i/cnt/f3813/index.html"
KANAGAWA_ORIGIN = "https://www.pref.kanagawa.jp"
KANAGAWA_ANCHOR = re.compile(
    r'<a[^>]+href="(/documents/15077/[^"]+\.pdf)"[^>]*>(.*?)</a>', re.S
)
KANAGAWA_FISCAL = re.compile(r"【令和(\d+)年度】")
TAGS = re.compile(r"<[^>]+>")
REIWA_EPOCH = 2018  # 令和1年 = 2019

UA = {"User-Agent": "bear-sky-map/1.0 (personal stargazing-safety map)"}


@dataclass
class Fetched:
    name: str
    path: Path
    url: str
    size: int
    changed: bool


def _resolve(page_url: str, pattern: re.Pattern, origin: str) -> str:
    resp = requests.get(page_url, headers=UA, timeout=30)
    resp.raise_for_status()
    m = pattern.search(resp.text)
    if not m:
        raise RuntimeError(
            f"could not find a data link on {page_url} — the page layout probably changed"
        )
    return origin + m.group(1)


def _download(url: str, dest: Path, expect: str) -> Fetched:
    resp = requests.get(url, headers=UA, timeout=60)
    resp.raise_for_status()
    body = resp.content

    # verify the payload really is what we asked for
    if expect == "xlsx" and not body.startswith(b"PK"):
        raise RuntimeError(f"{url} did not return a real .xlsx (got {body[:40]!r})")
    if expect == "csv":
        head = body[:200].lstrip(b"\xef\xbb\xbf").lower()
        if head.startswith(b"<!doctype") or head.startswith(b"<html"):
            raise RuntimeError(f"{url} returned HTML, not CSV")

    previous = dest.read_bytes() if dest.exists() else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return Fetched(dest.stem, dest, url, len(body), previous != body)


def fetch_miyagi() -> Fetched:
    url = _resolve(MIYAGI_PAGE, MIYAGI_LINK, MIYAGI_ORIGIN)
    return _download(url, RAW / "miyagi_kuma.xlsx", expect="xlsx")


def fetch_tokyo() -> Fetched:
    """Download the Tokyo CSV export.

    The pipeline no longer builds the map from this file (the export has been
    frozen at 2026-06-10 while the dashboard kept updating), but it is still
    fetched as a watch signal: the day it changes, "tokyo_kuma: 更新あり" shows
    up in the run report and we can go back to its exact coordinates instead of
    geocoding the dashboard's place names.
    """
    url = _resolve(TOKYO_PAGE, TOKYO_LINK, TOKYO_ORIGIN)
    return _download(url, RAW / "tokyo_kuma.csv", expect="csv")


def fetch_prefectures() -> Fetched:
    """Download the prefecture outlines once; they rarely change."""
    dest = RAW / "prefectures.geojson"
    if dest.exists():
        return Fetched(dest.stem, dest, PREFECTURE_URL, dest.stat().st_size, changed=False)

    resp = requests.get(PREFECTURE_URL, headers=UA, timeout=120)
    resp.raise_for_status()
    body = resp.content
    if not body.lstrip().startswith(b"{"):
        raise RuntimeError(f"{PREFECTURE_URL} did not return JSON")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return Fetched(dest.stem, dest, PREFECTURE_URL, len(body), changed=True)


def fetch_saitama() -> Fetched:
    """Page through the ArcGIS feature service and save the result as GeoJSON."""
    features: list[dict] = []
    offset = 0
    while True:
        resp = requests.get(
            f"{SAITAMA_SERVICE}/query",
            params={
                "where": "1=1",
                "outFields": SAITAMA_FIELDS,
                "outSR": "4326",
                "f": "geojson",
                "resultOffset": offset,
                "resultRecordCount": SAITAMA_PAGE,
                "orderByFields": "field_1 ASC",
            },
            headers=UA,
            timeout=60,
        )
        resp.raise_for_status()
        page = resp.json()
        if "error" in page:
            raise RuntimeError(f"ArcGIS error: {page['error']}")
        batch = page.get("features", [])
        features += batch
        if len(batch) < SAITAMA_PAGE:
            break
        offset += SAITAMA_PAGE

    body = json.dumps(
        {"type": "FeatureCollection", "features": features}, ensure_ascii=False
    ).encode("utf-8")
    dest = RAW / "saitama_arcgis.geojson"
    previous = dest.read_bytes() if dest.exists() else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return Fetched(dest.stem, dest, SAITAMA_SERVICE, len(body), previous != body)


def fetch_kanagawa() -> list[Fetched]:
    """Download every fiscal-year PDF listed on the page."""
    resp = requests.get(KANAGAWA_PAGE, headers=UA, timeout=30)
    resp.raise_for_status()
    # the server sends no charset, so requests guesses ISO-8859-1 and the
    # Japanese link labels come back as mojibake unless we say otherwise
    resp.encoding = resp.apparent_encoding or "utf-8"
    # the 【令和N年度】 label sits inside nested markup, so strip tags per anchor
    found = []
    for path, inner in KANAGAWA_ANCHOR.findall(resp.text):
        label = TAGS.sub("", inner)
        m = KANAGAWA_FISCAL.search(label)
        if m:
            found.append((path, m.group(1)))
    if not found:
        raise RuntimeError(
            f"{KANAGAWA_PAGE} に【令和N年度】形式のPDFリンクが見つかりません"
            " — ページ構成が変わった可能性があります"
        )

    fetched: list[Fetched] = []
    for path, _label_year in found:
        # keep the publisher's own filename; the year comes from the contents
        dest = RAW / f"kanagawa_{Path(path).stem}.pdf"
        body = requests.get(KANAGAWA_ORIGIN + path, headers=UA, timeout=60).content
        if not body.startswith(b"%PDF"):
            raise RuntimeError(f"{path} は PDF ではありません")
        previous = dest.read_bytes() if dest.exists() else None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        fetched.append(
            Fetched(dest.stem, dest, KANAGAWA_ORIGIN + path, len(body), previous != body)
        )
    return fetched


def fetch_all() -> list[Fetched]:
    return [
        fetch_tokyo(),
        fetch_miyagi(),
        fetch_saitama(),
        *fetch_kanagawa(),
        fetch_prefectures(),
    ]


if __name__ == "__main__":
    for f in fetch_all():
        state = "updated" if f.changed else "unchanged"
        print(f"{f.name}: {state}, {f.size:,} bytes")
        print(f"  {f.url}")
