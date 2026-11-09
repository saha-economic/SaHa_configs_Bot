#!/usr/bin/env python3
"""Send the scheduled Telegram update for the DE/US bot.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / GITHUB_REPOSITORY
from the environment (GitHub Secrets in the workflow).
If the secrets are missing, exits 0 silently — the workflow
still succeeds and the subscription files are still committed.
Stdlib only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "USER/REPO")


def main():
    if not TOKEN or not CHAT_ID:
        print("Telegram secrets not set; skipping notification (files still committed).")
        return 0
    try:
        stats = json.load(open("stats.json", encoding="utf-8"))
        de_n = stats["kept"]["DE"]
        us_n = stats["kept"]["US"]
    except Exception:
        de_n = us_n = "?"
    lines = [
        "به‌روزرسانی کانفیگ‌های رایگان",
        "",
        f"🇩🇪 آلمان: {de_n} نود",
        f"🇺🇸 آمریکا: {us_n} نود",
        "فقط VLESS+Reality و Hysteria2 — کشور ثابت، IP موقع مردن نود عوض می‌شود.",
        "",
        "آلمان:",
        f"https://raw.githubusercontent.com/{REPO}/main/de.txt",
        "",
        "آمریکا:",
        f"https://raw.githubusercontent.com/{REPO}/main/us.txt",
        "",
        "توی Hiddify/Throne حالت Manual را انتخاب کن، نه Auto کلی.",
    ]
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": "\n".join(lines),
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=data, headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("Telegram response:", r.status)
    except urllib.error.HTTPError as e:
        # Telegram rejected the message (e.g. 403: bot blocked / not
        # started / wrong chat id). Log the real reason and do NOT fail
        # the workflow — the subscription files are already committed.
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        print(f"Telegram send failed: HTTP {e.code} {body}")
    except Exception as e:
        print(f"Telegram send failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
