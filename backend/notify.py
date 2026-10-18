"""Send a run summary to Discord via an incoming webhook.

The webhook URL is a secret (anyone holding it can post to the channel), so it
is never stored in this file. Provide it either as the environment variable
BEAR_SKY_MAP_DISCORD_WEBHOOK or in backend/discord_webhook.txt, which .gitignore
keeps out of version control.

With no webhook configured this is a no-op, so the pipeline still runs locally
without any setup.
"""
import os
from pathlib import Path

import requests

WEBHOOK_FILE = Path(__file__).parent / "discord_webhook.txt"
ENV_VAR = "BEAR_SKY_MAP_DISCORD_WEBHOOK"
DISCORD_LIMIT = 2000  # characters per message


def webhook_url() -> str | None:
    url = os.environ.get(ENV_VAR, "").strip()
    if not url and WEBHOOK_FILE.exists():
        url = WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    return url or None


def _truncate(text: str) -> str:
    if len(text) <= DISCORD_LIMIT:
        return text
    cut = DISCORD_LIMIT - 40
    return text[:cut] + f"\n…(省略: 全{len(text)}文字)"


def send(message: str) -> bool:
    """Post a message. Returns False when no webhook is configured."""
    url = webhook_url()
    if not url:
        return False
    resp = requests.post(url, json={"content": _truncate(message)}, timeout=20)
    resp.raise_for_status()
    return True


def build_summary(
    *,
    sources: list,
    region_stats: dict,
    safe_area_stats: dict,
    issues: list[str],
    errors: list[str],
) -> str:
    """Compose the run report. Problems go first so they are visible on a phone."""
    lines = []
    if errors:
        lines.append("🔴 **bear-sky-map 更新エラー**")
        lines += [f"- {e}" for e in errors]
        lines.append("")
    elif issues:
        lines.append("🟡 **bear-sky-map 更新完了（データに要確認あり）**")
    else:
        lines.append("🟢 **bear-sky-map 更新完了**")

    for s in sources:
        lines.append(f"- {s.name}: {'更新あり' if s.changed else '変化なし'} ({s.size:,} bytes)")

    labels = {"tokyo": "東京", "miyagi": "宮城", "saitama": "埼玉", "kanagawa": "神奈川"}
    for region, stats in region_stats.items():
        count = stats.get("features", stats.get("sample_size", 0))
        extra = ""
        if stats.get("api_calls"):
            extra = f" (新規ジオコーディング {stats['api_calls']}件)"
        lines.append(f"- {labels.get(region, region)}: {count}件{extra}")
    if safe_area_stats:
        lines.append(f"- 観測良好エリア再計算: {safe_area_stats.get('summary', 'done')}")

    if issues:
        lines.append("")
        lines.append(f"⚠️ **データ品質の指摘 {len(issues)}件**（提供元のミスの可能性）")
        for issue in issues[:15]:
            lines.append(f"- {issue}")
        if len(issues) > 15:
            lines.append(f"- …他 {len(issues) - 15}件")

    return "\n".join(lines)


if __name__ == "__main__":
    ok = send("✅ bear-sky-map: Discord通知のテストです。")
    print("sent" if ok else f"webhook未設定 ({ENV_VAR} か {WEBHOOK_FILE.name} を用意してください)")
