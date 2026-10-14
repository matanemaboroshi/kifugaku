"""Build the static site from data/snapshots into ./public."""

import html
import json
import re
import shutil
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public"
CHART_DAYS = 30

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SITE_URL = CFG["site_url"].rstrip("/") + "/"
E = html.escape


# ---------- data ----------

def load_genre(genre):
    snaps = []
    for f in sorted((ROOT / "data" / "snapshots" / genre["slug"]).glob("*.json")):
        snaps.append(json.loads(f.read_text(encoding="utf-8")))
    return snaps


def item_slug(code):
    return re.sub(r"[^0-9A-Za-z_-]+", "-", code).strip("-") or "item"


def analyze(genre, snaps):
    """Annotate the latest snapshot's items with movement metrics."""
    if not snaps:
        return None
    latest = snaps[-1]
    prev = {i["code"]: i for i in snaps[-2]["items"]} if len(snaps) > 1 else None
    seen_before = {i["code"] for s in snaps[:-1] for i in s["items"]}
    present = [{i["code"] for i in s["items"]} for s in snaps]

    rows = []
    for it in latest["items"]:
        row = dict(it, genre=genre, has_prev=prev is not None)
        p = prev.get(it["code"]) if prev is not None else None
        row["prev_rank"] = p["rank"] if p else None
        row["change"] = p["rank"] - it["rank"] if p else None
        row["is_new"] = prev is not None and it["code"] not in seen_before
        row["price_diff"] = it["price"] - p["price"] if p and p["price"] else 0
        streak = 0
        for codes in reversed(present):
            if it["code"] not in codes:
                break
            streak += 1
        row["streak"] = streak
        row["best_rank"] = min(i["rank"] for s in snaps for i in s["items"] if i["code"] == it["code"])
        rows.append(row)
    return {"genre": genre, "date": latest["date"], "rows": rows, "has_prev": prev is not None, "days": len(snaps)}


def rank_history(snaps):
    hist = defaultdict(list)
    recent = snaps[-CHART_DAYS:]
    codes = {i["code"] for s in recent for i in s["items"]}
    for s in recent:
        ranks = {i["code"]: i["rank"] for i in s["items"]}
        for code in codes:
            hist[code].append((s["date"], ranks.get(code)))
    return hist


# ---------- html helpers ----------

def yen(n):
    return f"{n:,}円"


def mmdd(d):
    y, m, dd = d.split("-")
    return f"{int(m)}/{int(dd)}"


def change_badge(row):
    if row["is_new"]:
        return '<span class="badge new">初登場</span>'
    if row["change"] is None:
        return '<span class="badge in">再ランクイン</span>' if row["has_prev"] else ""
    if row["change"] > 0:
        return f'<span class="badge up">▲{row["change"]}</span>'
    if row["change"] < 0:
        return f'<span class="badge down">▼{-row["change"]}</span>'
    return '<span class="badge flat">→</span>'


def stars(avg, count):
    if not count:
        return ""
    return f'<span class="review">★{avg:.2f}（{count:,}件）</span>'


def chart_svg(points):
    """Rank-over-time line; lower rank number is drawn higher."""
    vals = [r for _, r in points if r is not None]
    if not vals:
        return ""
    w, h, pad = 640, 180, 28
    lo, hi = 1, max(max(vals), 10)
    n = max(len(points) - 1, 1)

    def xy(i, r):
        x = pad + (w - 2 * pad) * i / n
        y = pad + (h - 2 * pad) * (r - lo) / (hi - lo)
        return f"{x:.1f},{y:.1f}"

    segs, cur = [], []
    for i, (_, r) in enumerate(points):
        if r is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(xy(i, r))
    if cur:
        segs.append(cur)
    lines = "".join(f'<polyline points="{" ".join(s)}"/>' for s in segs)
    dots = "".join(
        f'<circle cx="{xy(i, r).split(",")[0]}" cy="{xy(i, r).split(",")[1]}" r="3"><title>{mmdd(d)} {r}位</title></circle>'
        for i, (d, r) in enumerate(points) if r is not None
    )
    return (
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="順位の推移">'
        f'<text x="4" y="{pad + 4}">1位</text><text x="4" y="{h - pad + 4}">{hi}位</text>'
        f'<line class="axis" x1="{pad}" y1="{pad}" x2="{w - pad}" y2="{pad}"/>'
        f'<line class="axis" x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}"/>'
        f'<text x="{pad}" y="{h - 6}">{mmdd(points[0][0])}</text>'
        f'<text x="{w - pad}" y="{h - 6}" text-anchor="end">{mmdd(points[-1][0])}</text>'
        f'<g class="line">{lines}</g><g class="dots">{dots}</g></svg>'
    )


def page(title, description, path, body, depth, jsonld=None):
    up = "../" * depth
    canonical = SITE_URL + path
    ld = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>' if jsonld else ""
    nav = "".join(f'<a href="{up}genre/{g["slug"]}.html">{E(g["name"])}</a>' for g in CFG["genres"])
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)}</title>
<meta name="description" content="{E(description)}">
<link rel="canonical" href="{E(canonical)}">
<meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{E(canonical)}">
<meta name="twitter:card" content="summary">
<link rel="stylesheet" href="{up}assets/style.css">
{ld}
</head>
<body>
<p class="pr">当サイトは楽天アフィリエイトを利用した広告（PR）を含みます。</p>
<header class="site">
  <a class="logo" href="{up}index.html">{E(CFG["site_name"])}</a>
  <nav class="genres">{nav}</nav>
</header>
<main>
{body}
</main>
<footer class="site">
  <p><a href="{up}about.html">このサイトについて・運営者情報</a></p>
  <p>掲載価格・順位は取得時点のものです。最新情報は各販売ページでご確認ください。</p>
  <p><a href="https://developers.rakuten.com/" rel="noopener" target="_blank">Supported by Rakuten Developers</a></p>
</footer>
</body>
</html>
"""


def item_card(row, up, show_genre=False):
    g = f'<span class="genre-tag">{E(row["genre"]["name"])}</span>' if show_genre else ""
    price = yen(row["price"])
    if row["price_diff"] < 0:
        price += f' <span class="down-price">({yen(row["price_diff"])})</span>'
    img = f'<img src="{E(row["image"])}" alt="" loading="lazy" width="150" height="150">' if row["image"] else '<div class="noimg"></div>'
    return f"""<li class="card">
  <div class="rank">{row["rank"]}<small>位</small></div>
  <a class="thumb" href="{E(row["url"])}" rel="sponsored nofollow noopener" target="_blank">{img}</a>
  <div class="info">
    <div class="meta">{change_badge(row)}{g}</div>
    <a class="name" href="{up}item/{item_slug(row["code"])}.html">{E(row["name"][:90])}</a>
    <div class="price">{price}</div>
    <div class="sub">{stars(row["review_avg"], row["review_count"])}<span>{E(row["shop"])}</span></div>
    <a class="buy" href="{E(row["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天市場で見る</a>
  </div>
</li>"""


# ---------- pages ----------

def build_genre(a):
    g, rows = a["genre"], a["rows"]
    title = f"【{mmdd(a['date'])}更新】{g['name']}の楽天ランキング｜急上昇・初登場を毎日チェック"
    desc = f"楽天市場「{g['name']}」ジャンルの最新ランキング{len(rows)}位まで。前日からの順位変動・初登場・値下げ商品を毎日自動で記録しています。"
    up_rows = sorted([r for r in rows if r["change"] and r["change"] > 0], key=lambda r: -r["change"])[:5]
    hl = ""
    if up_rows:
        hl = '<section><h2>今日の急上昇</h2><ul class="cards compact">' + "".join(item_card(r, "../") for r in up_rows) + "</ul></section>"
    note = "" if a["has_prev"] else '<p class="note">記録を開始したばかりです。順位の変動は明日から表示されます。</p>'
    body = f"""<h1>{E(g["name"])}の楽天ランキング</h1>
<p class="updated">{a["date"]} 更新 ／ 記録日数 {a["days"]}日</p>
{note}{hl}
<section><h2>ランキング 1〜{len(rows)}位</h2><ul class="cards">{"".join(item_card(r, "../") for r in rows)}</ul></section>"""
    ld = {
        "@context": "https://schema.org", "@type": "ItemList", "name": f"{g['name']}の楽天ランキング",
        "itemListElement": [{"@type": "ListItem", "position": r["rank"], "name": r["name"][:100]} for r in rows[:30]],
    }
    return page(title, desc, f"genre/{g['slug']}.html", body, 1, ld)


def build_item(code, entries, histories, today):
    first = entries[0]
    name = first["name"]
    title = f"{name[:50]}｜楽天ランキング順位の推移"
    desc = f"「{name[:60]}」の楽天市場ランキング順位の推移と価格。最高{min(e['best_rank'] for e in entries)}位。毎日自動更新。"
    blocks = []
    for e in entries:
        g = e["genre"]
        pts = histories[g["slug"]].get(code, [])
        blocks.append(f"""<section class="hist">
<h2>{E(g["name"])}ランキング：現在 {e["rank"]}位 {change_badge(e)}</h2>
<p>最高順位 {e["best_rank"]}位 ／ 連続ランクイン {e["streak"]}日</p>
{chart_svg(pts)}
<p><a href="../genre/{g["slug"]}.html">{E(g["name"])}ランキングをすべて見る</a></p>
</section>""")
    img = f'<img src="{E(first["image"])}" alt="{E(name[:60])}" width="300" height="300">' if first["image"] else ""
    catch = f'<p class="catch">{E(first["catch"])}</p>' if first["catch"] else ""
    body = f"""<article class="item">
<h1>{E(name)}</h1>
<div class="item-head">
  <a href="{E(first["url"])}" rel="sponsored nofollow noopener" target="_blank">{img}</a>
  <div>
    {catch}
    <p class="price big">{yen(first["price"])}</p>
    <p>{stars(first["review_avg"], first["review_count"])}</p>
    <p>ショップ：{E(first["shop"])}</p>
    <a class="buy big" href="{E(first["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天市場で詳細を見る</a>
    <p class="updated">{today} 時点</p>
  </div>
</div>
{"".join(blocks)}
</article>"""
    return page(title, desc, f"item/{item_slug(code)}.html", body, 1)


def build_index(analyses, today):
    rows = [r for a in analyses for r in a["rows"] if a["genre"]["slug"] != "all"]
    rising = sorted([r for r in rows if r["change"] and r["change"] > 0], key=lambda r: -r["change"])[:12]
    new = sorted([r for r in rows if r["is_new"]], key=lambda r: r["rank"])[:12]
    cheaper = sorted([r for r in rows if r["price_diff"] < 0], key=lambda r: r["price_diff"] / max(r["price"] - r["price_diff"], 1))[:12]

    def section(title, items, empty):
        if not items:
            return f"<section><h2>{title}</h2><p class=\"note\">{empty}</p></section>"
        return f'<section><h2>{title}</h2><ul class="cards">{"".join(item_card(r, "", True) for r in items)}</ul></section>'

    genre_cards = "".join(
        f"""<li><a href="genre/{a["genre"]["slug"]}.html"><strong>{E(a["genre"]["name"])}</strong>
<span>1位：{E(a["rows"][0]["name"][:40])}</span></a></li>""" for a in analyses if a["rows"]
    )
    wait = "明日の更新から表示されます。"
    body = f"""<h1>{E(CFG["site_name"])}</h1>
<p class="lead">{E(CFG["site_description"])}</p>
<p class="updated">{today} 更新</p>
{section("今日の急上昇ランキング", rising, wait)}
{section("今日の初登場", new, wait)}
{section("値下がりした人気商品", cheaper, "本日は値下がりした商品はありません。" if analyses and analyses[0]["has_prev"] else wait)}
<section><h2>ジャンル別ランキング</h2><ul class="genre-list">{genre_cards}</ul></section>"""
    title = f"{CFG['site_name']}｜楽天市場の急上昇・初登場ランキングを毎日記録"
    return page(title, CFG["site_description"], "", body, 0)


def build_about():
    body = f"""<h1>このサイトについて</h1>
<p>{E(CFG["site_name"])}は、楽天市場のジャンル別ランキングを毎日自動で取得・記録し、順位の変動や価格の変化をわかりやすくまとめたサイトです。</p>
<h2>広告について</h2>
<p>当サイトは楽天アフィリエイトに参加しており、商品リンクから購入された場合に運営者が紹介料を受け取ることがあります。掲載順位は楽天市場のランキングデータに基づいて機械的に決定しており、紹介料の有無による操作は行っていません。</p>
<h2>データについて</h2>
<p>ランキング・価格・レビュー情報は楽天ウェブサービスのAPIから取得しています。情報は取得時点のもので、最新の価格や在庫は各販売ページでご確認ください。</p>
<h2>免責事項</h2>
<p>当サイトの情報によって生じた損害について、運営者は責任を負いかねます。</p>"""
    return page(f"このサイトについて｜{CFG['site_name']}", "当サイトの運営方針・広告表記・データの取得元について。", "about.html", body, 0)


def build_empty():
    body = """<h1>準備中</h1><p class="note">ランキングデータの初回取得を待っています。取得が完了すると自動でページが作られます。</p>"""
    return page(CFG["site_name"], CFG["site_description"], "", body, 0)


def write(rel, content):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    shutil.copy(ROOT / "assets" / "style.css", OUT / "assets" / "style.css")

    analyses, histories = [], {}
    for g in CFG["genres"]:
        snaps = load_genre(g)
        a = analyze(g, snaps)
        if a:
            analyses.append(a)
            histories[g["slug"]] = rank_history(snaps)

    urls = ["", "about.html"]
    write("about.html", build_about())
    write(".nojekyll", "")

    if not analyses:
        write("index.html", build_empty())
    else:
        today = max(a["date"] for a in analyses)
        write("index.html", build_index(analyses, today))
        by_code = defaultdict(list)
        for a in analyses:
            write(f"genre/{a['genre']['slug']}.html", build_genre(a))
            urls.append(f"genre/{a['genre']['slug']}.html")
            for r in a["rows"]:
                by_code[r["code"]].append(r)
        for code, entries in by_code.items():
            entries.sort(key=lambda e: (e["genre"]["slug"] == "all", e["rank"]))
            write(f"item/{item_slug(code)}.html", build_item(code, entries, histories, today))
            urls.append(f"item/{item_slug(code)}.html")

    lastmod = date.today().isoformat()
    sitemap = "".join(f"<url><loc>{E(SITE_URL + u)}</loc><lastmod>{lastmod}</lastmod></url>" for u in urls)
    write("sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sitemap}</urlset>')
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n")
    write("404.html", page("ページが見つかりません", "", "404.html", '<h1>ページが見つかりません</h1><p><a href="' + urlsplit(SITE_URL).path + '">トップへ戻る</a></p>', 0))
    print(f"built {len(urls)} pages -> {OUT}")


if __name__ == "__main__":
    main()
