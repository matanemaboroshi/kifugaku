"""Fetch popular Rakuten furusato-nozei items and record a daily snapshot.

Items are found with the Ichiba Item Search API (sorted by review count) and
kept only when sold by a municipality shop (shop code "f" + 6-digit code).

Env:
  RAKUTEN_APP_ID        applicationId (required)
  RAKUTEN_ACCESS_KEY    accessKey (required)
  RAKUTEN_AFFILIATE_ID  affiliateId (optional, but no revenue without it)

Writes:
  data/catalog.json            code -> latest item details
  data/daily/<YYYY-MM-DD>.json [[code, price, review_count], ...] per category
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JST = timezone(timedelta(hours=9))
MUNICIPALITY_SHOP = re.compile(r"^f\d{6}")


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def first_image(raw):
    imgs = raw.get("mediumImageUrls") or []
    if not imgs:
        return ""
    img = imgs[0]
    url = img.get("imageUrl", "") if isinstance(img, dict) else str(img)
    return url.replace("_ex=128x128", "_ex=300x300")


def normalize(raw):
    # formatVersion=1 wraps each entry as {"Item": {...}}.
    raw = raw.get("Item", raw)
    code = raw.get("itemCode", "")
    return {
        "code": code,
        "shop_code": raw.get("shopCode") or code.split(":", 1)[0],
        "name": raw.get("itemName", "").strip(),
        "catch": raw.get("catchcopy", "").strip(),
        "price": int(raw.get("itemPrice", 0) or 0),
        "url": raw.get("affiliateUrl") or raw.get("itemUrl", ""),
        "image": first_image(raw),
        "shop": raw.get("shopName", ""),
        "review_avg": float(raw.get("reviewAverage", 0) or 0),
        "review_count": int(raw.get("reviewCount", 0) or 0),
    }


class NotFound(RuntimeError):
    """The gateway does not know this endpoint path/version."""


def call(endpoint, cfg, params):
    url = endpoint + "?" + urllib.parse.urlencode(params)
    # The Rakuten API rejects requests whose Referer does not match the site
    # registered for the application.
    req = urllib.request.Request(url, headers={
        "Referer": cfg["site_url"],
        "Origin": "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(cfg["site_url"])),
        "User-Agent": "furusato-watch/1.0",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 404 or "API Configuration not found" in body:
                raise NotFound(f"HTTP {e.code}: {body}") from e
            if e.code in (429, 500, 503) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


_endpoint = None


def request(cfg, params, creds):
    """Call the item search API, settling on the first endpoint that exists."""
    global _endpoint
    params = dict(params, applicationId=creds["app_id"], accessKey=creds["access_key"],
                  formatVersion=2, format="json")
    if creds["affiliate_id"]:
        params["affiliateId"] = creds["affiliate_id"]
    candidates = [_endpoint] if _endpoint else cfg["search_endpoints"]
    last = None
    for endpoint in candidates:
        try:
            data = call(endpoint, cfg, params)
        except NotFound as e:
            print(f"::warning::{endpoint} は利用できません: {e}")
            last = e
            continue
        if _endpoint is None:
            _endpoint = endpoint
            print(f"使用するAPI: {endpoint}")
            items = data.get("Items") or []
            if items:
                first = items[0].get("Item", items[0])
                print("レスポンス項目:", ", ".join(sorted(first.keys())))
        return data
    raise last or RuntimeError("search_endpoints が空です")


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

    today = datetime.now(JST).date().isoformat()
    catalog_path = DATA / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}

    daily, seen, failures = {}, set(), 0
    for cat in cfg["categories"]:
        rows = []
        # Keyword search also hits unrelated items (e.g. beer for "米"), so the
        # name must contain a word typical of the category.
        match = re.compile(cat["match"]) if cat.get("match") else None
        exclude = re.compile(cat["exclude"]) if cat.get("exclude") else None
        try:
            for page in range(1, cfg["pages_per_category"] + 1):
                data = request(cfg, {
                    "keyword": cat["keyword"],
                    "sort": "-reviewCount",
                    "hits": 30,
                    "page": page,
                    "availability": 1,
                }, creds)
                for item in map(normalize, data.get("Items", [])):
                    if not MUNICIPALITY_SHOP.match(item["shop_code"]) or item["code"] in seen:
                        continue
                    if match and not match.search(item["name"]):
                        continue
                    if exclude and exclude.search(item["name"]):
                        continue
                    seen.add(item["code"])
                    rows.append([item["code"], item["price"], item["review_count"]])
                    prev = catalog.get(item["code"], {})
                    catalog[item["code"]] = dict(
                        {k: v for k, v in item.items() if k not in ("price", "review_count", "shop_code")},
                        category=prev.get("category", cat["slug"]),
                        first_seen=prev.get("first_seen", today),
                        last_seen=today,
                    )
                time.sleep(1.2)
        except Exception as e:  # keep going with the other categories
            failures += 1
            print(f"::error::{cat['name']} の取得に失敗: {e}")
        if rows:
            daily[cat["slug"]] = rows
            print(f"{cat['name']}: {len(rows)}件")

    if daily:
        (DATA / "daily").mkdir(parents=True, exist_ok=True)
        (DATA / "daily" / f"{today}.json").write_text(
            json.dumps({"date": today, "categories": daily}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=0), encoding="utf-8")

    return 1 if failures == len(cfg["categories"]) else 0


if __name__ == "__main__":
    sys.exit(main())
