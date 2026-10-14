"""Fetch Rakuten Ichiba genre rankings and store a daily snapshot per genre.

Env:
  RAKUTEN_APP_ID        applicationId (required)
  RAKUTEN_ACCESS_KEY    accessKey (required)
  RAKUTEN_AFFILIATE_ID  affiliateId (optional, but no revenue without it)

Snapshots: data/snapshots/<slug>/<YYYY-MM-DD>.json
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def first_image(raw):
    imgs = raw.get("mediumImageUrls") or []
    if not imgs:
        return ""
    img = imgs[0]
    url = img.get("imageUrl", "") if isinstance(img, dict) else str(img)
    # Request a larger thumbnail than the default 128px.
    return url.replace("_ex=128x128", "_ex=300x300")


def normalize(raw):
    # formatVersion=1 wraps each entry as {"Item": {...}}.
    raw = raw.get("Item", raw)
    return {
        "rank": int(raw.get("rank", 0)),
        "code": raw.get("itemCode", ""),
        "name": raw.get("itemName", "").strip(),
        "catch": raw.get("catchcopy", "").strip(),
        "price": int(raw.get("itemPrice", 0) or 0),
        "url": raw.get("affiliateUrl") or raw.get("itemUrl", ""),
        "image": first_image(raw),
        "shop": raw.get("shopName", ""),
        "review_avg": float(raw.get("reviewAverage", 0) or 0),
        "review_count": int(raw.get("reviewCount", 0) or 0),
    }


def fetch_page(cfg, genre_id, page, creds):
    params = {
        "applicationId": creds["app_id"],
        "accessKey": creds["access_key"],
        "genreId": genre_id,
        "page": page,
        "formatVersion": 2,
        "format": "json",
    }
    if creds["affiliate_id"]:
        params["affiliateId"] = creds["affiliate_id"]
    url = cfg["api_endpoint"] + "?" + urllib.parse.urlencode(params)
    # The new Rakuten API rejects requests without a Referer matching the
    # site URL registered for the application.
    req = urllib.request.Request(url, headers={
        "Referer": cfg["site_url"],
        "Origin": "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(cfg["site_url"])),
        "User-Agent": "ranking-watch/1.0",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def prune(dir_path, keep_days, today):
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    for f in dir_path.glob("*.json"):
        if f.stem < cutoff:
            f.unlink()


def main():
    cfg = load_config()
    creds = {
        "app_id": os.environ.get("RAKUTEN_APP_ID", "").strip(),
        "access_key": os.environ.get("RAKUTEN_ACCESS_KEY", "").strip(),
        "affiliate_id": os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip(),
    }
    if not creds["app_id"] or not creds["access_key"]:
        print("::warning::RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY が未設定のため取得をスキップしました")
        return 0
    if not creds["affiliate_id"]:
        print("::warning::RAKUTEN_AFFILIATE_ID が未設定です。リンクから報酬が発生しません")

    today = datetime.now(JST).date()
    failures = 0
    for genre in cfg["genres"]:
        items = []
        try:
            for page in range(1, cfg["pages_per_genre"] + 1):
                data = fetch_page(cfg, genre["id"], page, creds)
                items.extend(normalize(i) for i in data.get("Items", []))
                time.sleep(1.2)
        except Exception as e:  # keep going with other genres
            failures += 1
            print(f"::error::{genre['name']} の取得に失敗: {e}")
            continue
        if not items:
            continue
        out_dir = ROOT / "data" / "snapshots" / genre["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{today.isoformat()}.json").write_text(
            json.dumps({"date": today.isoformat(), "genre": genre["slug"], "items": items},
                       ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        prune(out_dir, cfg["keep_days"], today)
        print(f"{genre['name']}: {len(items)}件")

    # Fail the job only when nothing at all could be fetched.
    return 1 if failures == len(cfg["genres"]) else 0


if __name__ == "__main__":
    sys.exit(main())
