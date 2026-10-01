"""Download the latest published bear-sighting data from each prefecture.

Both publishers change the file name every time they update (Miyagi embeds the
snapshot date, Tokyo embeds the export date), so the download URL is resolved
from the listing page each run rather than hard-coded.

Downloads are verified by content, not just HTTP status: a 404 page served with
status 200, or an HTML error page where a spreadsheet was expected, has bitten
this project before.
"""
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
    url = _resolve(TOKYO_PAGE, TOKYO_LINK, TOKYO_ORIGIN)
    return _download(url, RAW / "tokyo_kuma.csv", expect="csv")


def fetch_all() -> list[Fetched]:
    return [fetch_tokyo(), fetch_miyagi()]


if __name__ == "__main__":
    for f in fetch_all():
        state = "updated" if f.changed else "unchanged"
        print(f"{f.name}: {state}, {f.size:,} bytes")
        print(f"  {f.url}")
