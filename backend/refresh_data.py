"""One command to refresh every data product the frontend serves.

  download latest sources -> convert/geocode -> recompute safe areas
  -> publish to frontend/public/data -> report to Discord

Designed to run unattended on a schedule, so every stage is wrapped: a failure
is collected and reported rather than killing the run silently. Data-quality
findings from the publishers (future dates, unreadable rows, addresses the
geocoder cannot resolve) are always surfaced, never quietly corrected.
"""
import argparse
import shutil
import traceback
from datetime import datetime
from pathlib import Path

import compute_safe_area
import convert_saitama
import convert_tokyo_dashboard
import fetch_sources
import geocode_miyagi
import notify
import scrape_tokyo_dashboard
from config import RISK_WINDOW_DAYS

ROOT = Path(__file__).parent.parent
PROCESSED = ROOT / "data" / "processed"
FRONTEND_DATA = ROOT / "frontend" / "public" / "data"
PUBLISH = [
    "tokyo_kuma.geojson",
    "miyagi_kuma.geojson",
    "saitama_kuma.geojson",
    "safe_area.geojson",
    "bear_buffer.geojson",
]


def publish_to_frontend() -> list[str]:
    FRONTEND_DATA.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in PUBLISH:
        src = PROCESSED / name
        if src.exists():
            shutil.copy2(src, FRONTEND_DATA / name)
            copied.append(name)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=RISK_WINDOW_DAYS,
                        help="bear sighting lookback window used for risk buffers")
    parser.add_argument("--no-notify", action="store_true", help="skip the Discord report")
    parser.add_argument("--full-scrape", action="store_true",
                        help="re-read every dashboard row instead of stopping at known records")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="reuse the dashboard data already on disk")
    args = parser.parse_args()

    started = datetime.now()
    print(f"=== refresh started {started:%Y-%m-%d %H:%M:%S} ===")

    sources, errors, issues = [], [], []
    tokyo_stats: dict = {}
    miyagi_report: dict = {}
    saitama_stats: dict = {}
    safe_stats: dict = {}

    try:
        sources = fetch_sources.fetch_all()
        for s in sources:
            print(f"{s.name}: {'updated' if s.changed else 'unchanged'} ({s.size:,} bytes)")
    except Exception as e:
        errors.append(f"ダウンロード失敗: {e}")
        traceback.print_exc()

    # The Tokyo CSV export has been frozen since 2026-06-10 while the published
    # dashboard keeps updating, so sightings come from scraping that dashboard.
    if not errors and not args.skip_scrape:
        try:
            r = scrape_tokyo_dashboard.scrape(incremental=not args.full_scrape)
            print(f"tokyo dashboard: {r['total']} rows ({r['new']} new)")
        except Exception as e:
            errors.append(f"東京ダッシュボードの取得に失敗: {e}")
            traceback.print_exc()

    if not errors:
        for label, fn in (("東京の変換", convert_tokyo_dashboard.run),
                          ("宮城のジオコーディング", geocode_miyagi.run),
                          ("埼玉の変換", convert_saitama.run)):
            try:
                result = fn()
                issues += result.get("issues", [])
                if label.startswith("東京"):
                    tokyo_stats = result
                    print(f"tokyo: {result['features']} features, {result['skipped']} skipped")
                elif label.startswith("宮城"):
                    miyagi_report = result
                    print(f"miyagi: {result['sample_size']} records, "
                          f"{result['api_calls']} new geocodes, "
                          f"{result['cache_hits']} from cache")
                else:
                    saitama_stats = result
                    print(f"saitama: {result['features']} features "
                          f"(座標は県の公開値、ジオコーディング不要)")
            except Exception as e:
                errors.append(f"{label}に失敗: {e}")
                traceback.print_exc()

    if not errors:
        try:
            safe_stats = compute_safe_area.run(days=args.days)
            issues += safe_stats.get("issues", [])
        except Exception as e:
            errors.append(f'直近熊"低頻出"&星観測"暗さ"良好エリアの再計算に失敗: {e}')
            traceback.print_exc()

    if not errors:
        copied = publish_to_frontend()
        print(f"published {len(copied)} files to {FRONTEND_DATA}")

    # de-duplicate: the same bad record is seen by several stages
    issues = list(dict.fromkeys(issues))
    for issue in issues:
        print(f"  [DATA QUALITY] {issue}")

    elapsed = (datetime.now() - started).total_seconds()
    print(f"=== finished in {elapsed:.0f}s, {len(errors)} errors, {len(issues)} data issues ===")

    if not args.no_notify:
        message = notify.build_summary(
            sources=sources,
            miyagi_report=miyagi_report,
            tokyo_stats=tokyo_stats,
            saitama_stats=saitama_stats,
            safe_area_stats=safe_stats,
            issues=issues,
            errors=errors,
        )
        try:
            if notify.send(message):
                print("Discordへ通知しました")
            else:
                print("Discord webhook未設定のため通知はスキップ (backend/discord_webhook.txt)")
        except Exception as e:
            print(f"Discord通知に失敗: {e}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
