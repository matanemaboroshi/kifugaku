"""Build the static site from data/ into ./public."""

import html
import json
import re
import shutil
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "public"
CHART_DAYS = 90
TEIBAN_WINDOW = 30
TEIBAN_MIN_DAYS = 14
TEIBAN_RATIO = 0.8

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CATS = {c["slug"]: c for c in CFG["categories"]}
SITE_URL = CFG["site_url"].rstrip("/") + "/"
E = html.escape


# ---------- data ----------

def load():
    catalog_path = DATA / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
    for info in catalog.values():
        info["name"] = display_name(info["name"])
    snaps = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((DATA / "daily").glob("*.json"))]
    return catalog, snaps


def display_name(name):
    # Every item starts with the same "【ふるさと納税】" tag; drop it so lists show the useful part.
    return re.sub(r"^\s*【ふるさと納税】\s*", "", name).strip() or name


QTY = re.compile(r"(計|合計|総量|総重量)?\s*(\d+(?:\.\d+)?)\s*(kg|キロ|g|グラム)(?:\s*[×x]\s*(\d+))?", re.I)


def parse_kg(name):
    """Best-effort total weight in kg from an item name, or None if unclear."""
    s = unicodedata.normalize("NFKC", name).replace("✕", "×").replace("ｘ", "×")
    # "選べる" / ranges mean the listed price may not match the listed weight.
    if re.search(r"選べる|選択|[~〜～]\s*\d", s):
        return None
    totals, explicit = set(), None
    for label, num, unit, mult in QTY.findall(s):
        kg = float(num) / (1 if unit.lower() in ("kg", "キロ") else 1000)
        kg = round(kg * (int(mult) if mult else 1), 3)
        totals.add(kg)
        if label and explicit is None:
            explicit = kg
    # Several different weights (e.g. "900g/1.8kg/2.8kg") are size options:
    # the price shown belongs to only one of them, so give up.
    kg = explicit if explicit is not None else (totals.pop() if len(totals) == 1 else None)
    return kg if kg and 0.1 <= kg <= 60 else None


def slug_of(code):
    return re.sub(r"[^0-9A-Za-z_-]+", "-", code).strip("-") or "item"


def analyze(catalog, snaps):
    """Return (today, items) where items are dicts for everything listed today."""
    if not snaps:
        return None, []
    latest = snaps[-1]
    # Per-code series of (date, price, reviews, category_rank).
    series = {}
    for s in snaps:
        for slug, rows in s["categories"].items():
            for rank, (code, price, reviews) in enumerate(rows, 1):
                series.setdefault(code, []).append((s["date"], price, reviews, rank))

    recent = snaps[-TEIBAN_WINDOW:]
    items = []
    for slug, rows in latest["categories"].items():
        for rank, (code, price, reviews) in enumerate(rows, 1):
            info = catalog.get(code)
            if not info:
                continue
            hist = series[code]
            prices = [p for _, p, _, _ in hist if p]
            prev = hist[-2] if len(hist) > 1 else None
            last_change = None
            for a, b in zip(hist, hist[1:]):
                if a[1] and b[1] and a[1] != b[1]:
                    last_change = {"date": b[0], "from": a[1], "to": b[1]}

            # Review growth over ~7 snapshots as a proxy for recent sales.
            base = hist[-8] if len(hist) >= 8 else hist[0]
            span = max((date.fromisoformat(hist[-1][0]) - date.fromisoformat(base[0])).days, 1)
            growth = reviews - base[2] if base is not hist[-1] else 0

            present = sum(1 for s in recent if any(r[0] == code for r in s["categories"].get(slug, [])))
            kg = parse_kg(info["name"]) if CATS.get(slug, {}).get("per_kg") else None
            items.append(dict(
                info,
                code=code, slug=slug_of(code), cat=slug, rank=rank, price=price, reviews=reviews,
                prev_price=prev[1] if prev else None,
                change=price - prev[1] if prev and prev[1] else 0,
                last_change=last_change,
                min_price=min(prices) if prices else price,
                max_price=max(prices) if prices else price,
                tracked=len(hist),
                growth=growth,
                growth_per_day=growth / span,
                teiban=len(recent) >= TEIBAN_MIN_DAYS and present / len(recent) >= TEIBAN_RATIO,
                presence=present / len(recent),
                kg=kg,
                per_kg=round(price / kg) if kg else None,
                history=hist[-CHART_DAYS:],
            ))
    return latest["date"], items


def price_events(snaps, catalog, days):
    """Price changes between consecutive snapshots within the last `days` days, newest first."""
    if len(snaps) < 2:
        return []
    cutoff = (date.fromisoformat(snaps[-1]["date"]) - timedelta(days=days)).isoformat()
    events = []
    for a, b in zip(snaps, snaps[1:]):
        if b["date"] <= cutoff:
            continue
        pa = {code: p for rows in a["categories"].values() for code, p, _ in rows}
        for slug, rows in b["categories"].items():
            for code, p, _ in rows:
                old = pa.get(code)
                if old and p and old != p and code in catalog:
                    events.append({"date": b["date"], "code": code, "cat": slug, "old": old, "new": p})
    events.sort(key=lambda e: (e["date"], abs(e["new"] - e["old"])), reverse=True)
    return events


def kg_per_10k(it):
    return round(10000 / it["per_kg"], 2) if it.get("per_kg") else None


# ---------- html helpers ----------

def yen(n):
    return f"{n:,}円"


def mmdd(d):
    _, m, dd = d.split("-")
    return f"{int(m)}/{int(dd)}"


def is_lowest(it):
    return it["tracked"] >= 7 and it["price"] == it["min_price"] and it["max_price"] > it["min_price"]


def badges(it):
    out = []
    if it["change"] > 0:
        out.append(f'<span class="badge up">▲{it["change"]:,}円 値上げ</span>')
    elif it["change"] < 0:
        out.append(f'<span class="badge down">▼{-it["change"]:,}円 値下げ</span>')
    if is_lowest(it):
        out.append('<span class="badge low">記録上の最安</span>')
    if it["teiban"]:
        out.append('<span class="badge teiban">定番</span>')
    if it["growth_per_day"] >= 20:
        out.append('<span class="badge hot">勢いあり</span>')
    return "".join(out)


def stars(avg, count):
    if not count:
        return ""
    return f'<span class="review">★{avg:.2f}（{count:,}件）</span>'


def chart_svg(points, label, fmt, invert=False, flat_note=""):
    """Line chart of (date, value) with gaps for missing values."""
    vals = [v for _, v in points if v is not None]
    if len(vals) < 2:
        return '<p class="note">推移グラフは記録が2日分たまると表示されます。</p>'
    lo, hi = min(vals), max(vals)
    if lo == hi:
        return f'<p class="note">{E(flat_note or f"記録期間中は {fmt(lo)} のまま変わっていません。")}</p>'
    w, h, pad = 640, 200, 40
    n = len(points) - 1

    def xy(i, v):
        t = (v - lo) / (hi - lo)
        y = pad + (h - 2 * pad) * (t if invert else 1 - t)
        return pad + (w - 2 * pad) * i / n, y

    segs, cur = [], []
    for i, (_, v) in enumerate(points):
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append("%.1f,%.1f" % xy(i, v))
    if cur:
        segs.append(cur)
    lines = "".join(f'<polyline points="{" ".join(s)}"/>' for s in segs)
    dots = "".join(
        '<circle cx="%.1f" cy="%.1f" r="2.5"><title>%s %s</title></circle>' % (*xy(i, v), mmdd(d), fmt(v))
        for i, (d, v) in enumerate(points) if v is not None
    )
    top, bottom = (lo, hi) if invert else (hi, lo)
    return (
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{E(label)}">'
        f'<text x="{pad}" y="{pad - 10}">{E(fmt(top))}</text><text x="{pad}" y="{h - pad + 24}">{E(fmt(bottom))}</text>'
        f'<line class="axis" x1="{pad}" y1="{pad}" x2="{w - pad}" y2="{pad}"/>'
        f'<line class="axis" x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}"/>'
        f'<text x="{w - pad}" y="{h - 6}" text-anchor="end">{mmdd(points[0][0])} 〜 {mmdd(points[-1][0])}</text>'
        f'<g class="line">{lines}</g><g class="dots">{dots}</g></svg>'
    )


def page(title, description, path, body, depth, jsonld=None, ogp="ogp/default.png", noindex=False, scripts=""):
    up = "../" * depth
    canonical = SITE_URL + path
    ld = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>' if jsonld else ""
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    cats = "".join(f'<a href="{up}cat/{c["slug"]}.html">{E(c["name"])}</a>' for c in CFG["categories"])
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)}</title>
<meta name="description" content="{E(description)}">
{robots}
<link rel="canonical" href="{E(canonical)}">
<meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{E(canonical)}">
<meta property="og:image" content="{E(SITE_URL + ogp)}">
<meta property="og:site_name" content="{E(CFG["site_name"])}">
<meta name="twitter:card" content="summary_large_image">
<meta name="theme-color" content="#14161c">
<link rel="stylesheet" href="{up}assets/style.css">
{ld}
</head>
<body>
<p class="pr">【PR】当サイトは楽天アフィリエイトを利用した広告を含みます。</p>
<header class="site">
  <div class="bar">
    <a class="logo" href="{up}index.html"><span class="flash">速報</span>{E(CFG["site_name"].replace("値上げ速報", "").strip())}<b>値上げ速報</b></a>
  </div>
  <nav class="main-nav">
    <a href="{up}changes.html">値上げ速報</a>
    <a href="{up}shindan.html">残り枠ぴったり診断</a>
    <a href="{up}ryou.html">1万円でもらえる量</a>
  </nav>
  <nav class="genres">{cats}</nav>
</header>
<main>
{body}
</main>
<footer class="site">
  <p><a href="{up}about.html">このサイトについて・運営者情報</a></p>
  <p>寄付額・レビュー数は取得時点のものです。最新情報は各返礼品ページでご確認ください。</p>
  <p><a href="https://developers.rakuten.com/" rel="noopener" target="_blank">Supported by Rakuten Developers</a></p>
</footer>
{scripts}
</body>
</html>
"""


def thumb(it, size=150):
    if it.get("image"):
        return f'<img src="{E(it["image"])}" alt="" loading="lazy" width="{size}" height="{size}">'
    return '<div class="noimg"></div>'


def card(it, up, lead=""):
    kg = f'<span class="perkg">1万円で約{kg_per_10k(it):g}kg</span>' if it["per_kg"] else ""
    lead = f'<div class="lead-val">{lead}</div>' if lead else ""
    return f"""<li class="card">
  <a class="thumb" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">{thumb(it)}</a>
  <div class="info">
    {lead}<div class="meta">{badges(it)}<span class="genre-tag">{E(CATS[it["cat"]]["name"])}</span></div>
    <a class="name" href="{up}item/{it["slug"]}.html">{E(it["name"][:80])}</a>
    <div class="price"><small>寄付額</small>{yen(it["price"])} {kg}</div>
    <div class="sub">{stars(it["review_avg"], it["reviews"])}<span>{E(it["shop"])}</span></div>
    <a class="buy" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天で見る</a>
  </div>
</li>"""


def cards(items, up, lead=None):
    return '<ul class="cards">' + "".join(card(i, up, lead(i) if lead else "") for i in items) + "</ul>"


def section(title, items, up, empty, lead=None, intro="", more=""):
    intro = f'<p class="intro">{intro}</p>' if intro else ""
    more = f'<p class="more">{more}</p>' if more else ""
    if not items:
        return f'<section><h2>{title}</h2>{intro}<p class="note">{empty}</p></section>'
    return f"<section><h2>{title}</h2>{intro}{cards(items, up, lead)}{more}</section>"


def change_rows(events, catalog, up, limit):
    rows = []
    for e in events[:limit]:
        info = catalog[e["code"]]
        diff = e["new"] - e["old"]
        pct = diff / e["old"] * 100
        cls = "up" if diff > 0 else "down"
        rows.append(f"""<li class="chg {cls}">
  <a class="chg-thumb" href="{E(info["url"])}" rel="sponsored nofollow noopener" target="_blank">{thumb(info, 64)}</a>
  <div class="chg-body">
    <a class="chg-name" href="{up}item/{slug_of(e["code"])}.html">{E(info["name"][:60])}</a>
    <div class="chg-meta">{mmdd(e["date"])}・{E(info["shop"])}・{E(CATS.get(e["cat"], {}).get("name", ""))}</div>
    <div class="chg-price">{yen(e["old"])} → <strong>{yen(e["new"])}</strong></div>
  </div>
  <div class="chg-diff"><span>{"▲" if diff > 0 else "▼"}{abs(diff):,}円</span><small>{pct:+.0f}%</small></div>
</li>""")
    return '<ul class="chg-list">' + "".join(rows) + "</ul>"


def by_momentum(items):
    return sorted(items, key=lambda i: (-i["growth_per_day"], -i["reviews"]))


def by_ryou(items):
    return sorted([i for i in items if i["per_kg"]], key=lambda i: i["per_kg"])


def growth_lead(i):
    return f'直近 +{i["growth"]:,}件のレビュー' if i["growth"] > 0 else ""


def ryou_lead(i):
    return f'1万円で約{kg_per_10k(i):g}kg（寄付額{yen(i["price"])}で約{i["kg"]:g}kg）'


WAIT = "記録を始めたばかりです。毎日のデータがたまると表示されます。"
TEIBAN_WAIT = f"定番の判定には{TEIBAN_MIN_DAYS}日分の記録が必要です。データがたまり次第、自動で表示されます。"


def shindan_form(action, value=""):
    return f"""<form class="shindan-form" action="{action}" method="get">
  <label for="r">控除上限の残り</label>
  <div class="field"><input id="r" name="r" type="number" inputmode="numeric" min="1000" step="1000" placeholder="例：23000" value="{E(value)}" required><span>円</span></div>
  <button type="submit">ぴったりの組み合わせを探す</button>
</form>"""


# ---------- pages ----------

def build_index(today, items, events7, events30, catalog):
    days = max(i["tracked"] for i in items)
    ups = [e for e in events7 if e["new"] > e["old"]]
    downs = [e for e in events7 if e["new"] < e["old"]]
    teiban = sorted([i for i in items if i["teiban"]], key=lambda i: -i["reviews"])[:6]
    momentum = by_momentum([i for i in items if i["growth"] > 0])[:6]
    best_ryou = []
    for c in CFG["categories"]:
        if c["per_kg"]:
            top = by_ryou([i for i in items if i["cat"] == c["slug"]])[:1]
            best_ryou += top

    hikes = (change_rows(ups, catalog, "", 8) + '<p class="more"><a href="changes.html">値上げ・値下げの一覧を見る →</a></p>') if ups else \
        f'<p class="note">{"直近7日間、値上げされた返礼品はありません。" if days > 1 else WAIT}</p>'
    hike_counts = {}
    for e in events30:
        if e["new"] > e["old"]:
            hike_counts[e["cat"]] = hike_counts.get(e["cat"], 0) + 1
    if hike_counts:
        mx = max(hike_counts.values())
        bars = "".join(
            f'<li><span class="bar-label">{E(CATS[s]["name"])}</span><span class="bar-track"><span class="bar-fill" style="width:{n / mx * 100:.0f}%"></span></span><span class="bar-num">{n}件</span></li>'
            for s, n in sorted(hike_counts.items(), key=lambda x: -x[1])
        )
        hot_cats = f'<ul class="bars">{bars}</ul>'
    else:
        hot_cats = f'<p class="note">{"直近30日間の値上げはありません。" if days > 1 else WAIT}</p>'
    cat_links = "".join(
        f'<li><a href="cat/{c["slug"]}.html"><strong>{E(c["name"])}</strong><span>{sum(1 for i in items if i["cat"] == c["slug"])}品を記録中</span></a></li>'
        for c in CFG["categories"]
    )
    body = f"""<section class="hero">
  <p class="kicker">楽天ふるさと納税の寄付額を毎日記録</p>
  <h1>値上げされる前に、<br>ぴったり使い切る。</h1>
  <p class="hero-sub">人気返礼品{len(items)}品の寄付額を毎朝チェック。値上げ・値下げをいち早くお知らせします。</p>
  {shindan_form("shindan.html")}
</section>
<section class="stats">
  <div class="stat up"><small>直近7日の値上げ</small><b>{len(ups)}<i>件</i></b></div>
  <div class="stat down"><small>直近7日の値下げ</small><b>{len(downs)}<i>件</i></b></div>
  <div class="stat"><small>記録中の返礼品</small><b>{len(items)}<i>品</i></b></div>
  <div class="stat"><small>記録日数</small><b>{days}<i>日</i></b></div>
  <p class="stats-date">{today} 6:00 更新</p>
</section>
<section><h2>今週、値上げされた返礼品</h2>{hikes}</section>
{section("1万円でいちばん多くもらえる", best_ryou, "", "対象の返礼品がありません。", lambda i: f'{CATS[i["cat"]]["name"]}部門：1万円で約{kg_per_10k(i):g}kg',
         "カテゴリごとに、寄付額1万円あたりの量がいちばん多い返礼品です。", '<a href="ryou.html">カテゴリ別のランキングを見る →</a>')}
{section("いま申し込みが集まっている", momentum, "", WAIT, growth_lead, "直近のレビュー増加数が多い返礼品です。")}
<section><h2>値上げが多いカテゴリ（直近30日）</h2>{hot_cats}</section>
{section("売れ続けている定番", teiban, "", TEIBAN_WAIT, None, f"直近{TEIBAN_WINDOW}日のうち{int(TEIBAN_RATIO * 100)}%以上、人気上位に入り続けている返礼品です。")}
<section><h2>カテゴリから探す</h2><ul class="genre-list">{cat_links}</ul></section>"""
    title = f"{CFG['site_name']}｜値上げ前に選ぶ楽天ふるさと納税・残り枠ぴったり診断"
    return page(title, CFG["site_description"], "", body, 0, ogp="ogp/index.png")


def build_category(c, today, items):
    mine = [i for i in items if i["cat"] == c["slug"]]
    popular = sorted(mine, key=lambda i: i["rank"])
    momentum = by_momentum([i for i in mine if i["growth"] > 0])[:10]
    teiban = [i for i in popular if i["teiban"]][:10]
    changed = sorted([i for i in mine if i["change"]], key=lambda i: -i["change"])[:10]
    ryou = ""
    if c["per_kg"]:
        ryou = f'<div id="ryou">{section(f"1万円でもらえる{c["name"]}の量ランキング", by_ryou(mine)[:20], "../", "対象の返礼品がありません。", ryou_lead, "重さは返礼品名から自動で算出した目安です。内容量は必ず返礼品ページでご確認ください。")}</div>'
    body = f"""<h1>ふるさと納税「{E(c["name"])}」値上げ速報と人気返礼品</h1>
<p class="updated">{today} 更新 ／ {len(mine)}品を記録中</p>
{section("今日、寄付額が変わった返礼品", changed, "../", "今日、寄付額が変わった返礼品はありません。" if any(i["tracked"] > 1 for i in mine) else WAIT)}
{ryou}
{section("いま申し込みが集まっている", momentum, "../", WAIT, growth_lead)}
{section("定番の返礼品", teiban, "../", TEIBAN_WAIT)}
{section(f"{c['name']}の人気返礼品（レビュー数順）", popular, "../", "データがありません。")}"""
    title = f"【{mmdd(today)}更新】ふるさと納税 {c['name']}の値上げ速報・1万円でもらえる量ランキング"
    desc = f"楽天ふるさと納税「{c['name']}」の人気返礼品{len(mine)}品の寄付額を毎日記録。値上げ・値下げ、1万円でもらえる量、売れ続けている定番品がわかります。"
    ld = {"@context": "https://schema.org", "@type": "ItemList", "name": title,
          "itemListElement": [{"@type": "ListItem", "position": n, "name": i["name"][:100]} for n, i in enumerate(popular[:30], 1)]}
    return page(title, desc, f"cat/{c['slug']}.html", body, 1, ld, ogp=f"ogp/cat-{c['slug']}.png")


def build_ryou(today, items):
    blocks = []
    for c in CFG["categories"]:
        if not c["per_kg"]:
            continue
        top = by_ryou([i for i in items if i["cat"] == c["slug"]])[:5]
        blocks.append(section(f"{c['name']}：1万円でもらえる量 TOP5", top, "", "対象の返礼品がありません。", ryou_lead,
                              more=f'<a href="cat/{c["slug"]}.html#ryou">{E(c["name"])}のランキングをもっと見る →</a>'))
    body = f"""<h1>1万円でもらえる量ランキング</h1>
<p class="updated">{today} 更新</p>
<p class="intro">寄付額1万円あたりにもらえる量（kg）で並べています。重さは返礼品名から自動で読み取った目安で、サイズを選べる返礼品などは対象外です。内容量は必ず返礼品ページでご確認ください。</p>
{"".join(blocks)}"""
    return page(f"ふるさと納税 1万円でもらえる量ランキング【{mmdd(today)}更新】", "楽天ふるさと納税の肉・魚介・米・果物・野菜を、寄付額1万円あたりにもらえる量で比べたランキングです。", "ryou.html", body, 0, ogp="ogp/ryou.png")


def build_item(it, today):
    hist = it["history"]
    price_chart = chart_svg([(d, p) for d, p, _, _ in hist], "寄付額の推移", yen,
                            flat_note=f"記録期間中（{it['tracked']}日）、寄付額は {yen(it['price'])} のまま変わっていません。")
    review_chart = chart_svg([(d, r) for d, _, r, _ in hist], "レビュー数の推移", lambda v: f"{v:,.0f}件")
    facts = [
        ("現在の寄付額", yen(it["price"])),
        ("記録上の最安 / 最高", f'{yen(it["min_price"])} / {yen(it["max_price"])}'),
        ("最後の寄付額変更", f'{it["last_change"]["date"]}：{yen(it["last_change"]["from"])} → {yen(it["last_change"]["to"])}' if it["last_change"] else "記録期間中の変更なし"),
        ("カテゴリ内の人気順位", f'{CATS[it["cat"]]["name"]} {it["rank"]}位'),
        ("直近のレビュー増加", f'+{it["growth"]:,}件' if it["tracked"] > 1 else "記録中"),
        ("定番度（直近30日で上位にいた割合）", f'{it["presence"] * 100:.0f}%' + ("（定番）" if it["teiban"] else "")),
    ]
    if it["per_kg"]:
        facts.append(("1万円でもらえる量（目安）", f'約{kg_per_10k(it):g}kg（約{it["kg"]:g}kg として算出）'))
    facts.append(("記録日数", f'{it["tracked"]}日'))
    rows = "".join(f"<tr><th>{E(k)}</th><td>{E(v)}</td></tr>" for k, v in facts)
    img = f'<img src="{E(it["image"])}" alt="{E(it["name"][:60])}" width="300" height="300">' if it["image"] else ""
    catch = f'<p class="catch">{E(it["catch"])}</p>' if it["catch"] else ""
    body = f"""<article class="item">
<h1>{E(it["name"])}</h1>
<div class="item-head">
  <a href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">{img}</a>
  <div>
    <div class="meta">{badges(it)}</div>
    {catch}
    <p class="price big"><small>寄付額</small>{yen(it["price"])}</p>
    <p>{stars(it["review_avg"], it["reviews"])}</p>
    <p>{E(it["shop"])}</p>
    <a class="buy big" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天ふるさと納税で見る</a>
    <p class="updated">{today} 時点</p>
  </div>
</div>
<section><h2>買い時チェック</h2><table class="facts">{rows}</table></section>
<section class="hist"><h2>寄付額の推移</h2>{price_chart}</section>
<section class="hist"><h2>レビュー数の推移（申し込みの勢い）</h2>{review_chart}</section>
<p class="more"><a href="../cat/{it["cat"]}.html">{E(CATS[it["cat"]]["name"])}の返礼品をもっと見る →</a></p>
</article>"""
    title = f"{it['name'][:45]}｜寄付額の推移・値上げ情報"
    desc = f"「{it['name'][:60]}」の寄付額の推移、値上げ・値下げ履歴、レビュー数の伸びを毎日記録。現在{yen(it['price'])}。"
    return page(title, desc, f"item/{it['slug']}.html", body, 1)


def build_changes(today, events30, catalog, n_snaps):
    ups = [e for e in events30 if e["new"] > e["old"]]
    downs = [e for e in events30 if e["new"] < e["old"]]
    empty = WAIT if n_snaps < 2 else "直近30日の変更はありません。"
    body = f"""<h1>ふるさと納税 値上げ速報</h1>
<p class="updated">{today} 6:00 更新 ／ 直近30日：値上げ {len(ups)}件・値下げ {len(downs)}件</p>
<p class="intro">人気返礼品の寄付額を毎朝記録し、変わったものを新しい順に並べています。気になる返礼品が値上げされる前の参考にどうぞ。</p>
<section><h2>値上げ（直近30日）</h2>{change_rows(ups, catalog, "", 150) if ups else f'<p class="note">{empty}</p>'}</section>
<section><h2>値下げ（直近30日）</h2>{change_rows(downs, catalog, "", 150) if downs else f'<p class="note">{empty}</p>'}</section>"""
    return page(f"ふるさと納税 値上げ速報【{mmdd(today)}更新】寄付額の値上げ・値下げ一覧",
                "楽天ふるさと納税の人気返礼品で、寄付額が値上げ・値下げされたものを毎朝記録した一覧です。", "changes.html", body, 0, ogp="ogp/changes.png")


def build_shindan(today):
    chips = "".join(f'<label class="chip"><input type="checkbox" name="c" value="{c["slug"]}"><span>{E(c["name"])}</span></label>' for c in CFG["categories"])
    body = f"""<section class="hero small">
  <h1>残り枠ぴったり診断</h1>
  <p class="hero-sub">控除上限の残りを入れると、ぴったり使い切れる返礼品の組み合わせを探します。</p>
  {shindan_form("shindan.html")}
  <div class="chips" id="chips"><span class="chips-label">カテゴリで絞る：</span>{chips}</div>
</section>
<div id="result" aria-live="polite"><p class="note">残りの金額を入れて「ぴったりの組み合わせを探す」を押してください。</p></div>
<section class="guide">
  <h2>この診断について</h2>
  <ul>
    <li>当サイトが記録している人気返礼品（{today}時点の寄付額）から、合計が残りの金額以内で、なるべくぴったりになる組み合わせを探します。</li>
    <li>控除上限額そのものは、年収や家族構成で変わります。<a href="https://www.soumu.go.jp/main_sosiki/jichi_zeisei/czaisei/czaisei_seido/furusato/" rel="noopener" target="_blank">総務省のふるさと納税ポータル</a>などでご確認ください。</li>
    <li>寄付額は変わることがあります。申し込む前に各返礼品ページで最新の寄付額をご確認ください。</li>
  </ul>
</section>"""
    return page("ふるさと納税 残り枠ぴったり診断｜残りの金額で使い切れる返礼品の組み合わせ",
                "控除上限の残りの金額を入れると、楽天ふるさと納税の人気返礼品からぴったり使い切れる組み合わせを探します。",
                "shindan.html", body, 0, ogp="ogp/shindan.png", scripts='<script src="assets/shindan.js" defer></script>')


def build_about():
    body = f"""<h1>このサイトについて</h1>
<p>{E(CFG["site_name"])}は、楽天ふるさと納税の人気返礼品について、寄付額とレビュー数を毎日自動で記録し、値上げ・値下げや人気の推移をわかりやすくまとめたサイトです。</p>
<h2>各指標について</h2>
<ul>
<li><strong>値上げ・値下げ</strong>：前回の記録から寄付額が変わった返礼品です。毎朝6時ごろに記録します。</li>
<li><strong>1万円でもらえる量</strong>：返礼品名に書かれた重さから自動で算出した目安です。正確な内容量は返礼品ページでご確認ください。</li>
<li><strong>勢い</strong>：直近のレビュー増加数。申し込みが集まっている目安です。</li>
<li><strong>定番</strong>：直近{TEIBAN_WINDOW}日のうち{int(TEIBAN_RATIO * 100)}%以上、カテゴリ内の人気上位に入っていた返礼品です。</li>
<li><strong>記録上の最安</strong>：当サイトが記録を始めてからの最安の寄付額です。過去すべての最安値ではありません。</li>
</ul>
<h2>広告について</h2>
<p>当サイトは楽天アフィリエイトに参加しており、リンク先で寄付が行われた場合に運営者が紹介料を受け取ることがあります。掲載順はデータに基づいて機械的に決めており、紹介料による操作は行っていません。</p>
<h2>データについて</h2>
<p>寄付額・レビュー情報は楽天ウェブサービスのAPIから取得しています。情報は取得時点のもので、最新の寄付額や受付状況は各返礼品ページでご確認ください。控除上限額などの税制については、お住まいの自治体や公式情報をご確認ください。</p>
<h2>免責事項</h2>
<p>当サイトの情報によって生じた損害について、運営者は責任を負いかねます。</p>"""
    return page(f"このサイトについて｜{CFG['site_name']}", "当サイトの指標の見方・広告表記・データの取得元について。", "about.html", body, 0)


def build_empty():
    body = '<h1>準備中</h1><p class="note">返礼品データの初回取得を待っています。取得が完了すると自動でページが作られます。</p>'
    return page(CFG["site_name"], CFG["site_description"], "", body, 0)


# ---------- data for the planner and for the short-video pipeline ----------

def item_record(it):
    return {
        "code": it["code"], "name": it["name"], "shop": it["shop"], "category": it["cat"],
        "price": it["price"], "reviews": it["reviews"], "review_avg": it["review_avg"],
        "kg": it["kg"], "kg_per_10k": kg_per_10k(it),
        "page": SITE_URL + f"item/{it['slug']}.html", "url": it["url"], "image": it["image"],
    }


def planner_data(items):
    # Compact keys keep the file small; read by assets/shindan.js.
    return [{"n": i["name"][:70], "p": i["price"], "c": i["cat"], "u": i["url"], "i": i["image"],
             "s": i["shop"], "r": i["reviews"], "h": i["slug"]}
            for i in sorted(items, key=lambda i: -i["reviews"]) if i["price"] >= 1000]


def feed(today, items, events7, catalog, n_snaps):
    by_code = {i["code"]: i for i in items}

    def event(e):
        info = catalog[e["code"]]
        return {"date": e["date"], "code": e["code"], "name": info["name"], "shop": info["shop"], "category": e["cat"],
                "before": e["old"], "after": e["new"], "diff": e["new"] - e["old"],
                "pct": round((e["new"] - e["old"]) / e["old"] * 100, 1),
                "page": SITE_URL + f"item/{slug_of(e['code'])}.html", "url": info["url"], "image": info["image"],
                "reviews": by_code[e["code"]]["reviews"] if e["code"] in by_code else None}

    return {
        "generated": today,
        "site": SITE_URL,
        "days_recorded": n_snaps,
        "categories": {c["slug"]: c["name"] for c in CFG["categories"]},
        "price_up_7d": [event(e) for e in events7 if e["new"] > e["old"]][:50],
        "price_down_7d": [event(e) for e in events7 if e["new"] < e["old"]][:50],
        "kg_per_10k": {c["slug"]: [item_record(i) for i in by_ryou([i for i in items if i["cat"] == c["slug"]])[:10]]
                       for c in CFG["categories"] if c["per_kg"]},
        "momentum": [dict(item_record(i), review_growth=i["growth"]) for i in by_momentum([i for i in items if i["growth"] > 0])[:20]],
        "teiban": [item_record(i) for i in sorted([i for i in items if i["teiban"]], key=lambda i: -i["reviews"])[:20]],
    }


# ---------- per-video link pages ----------

def load_videos():
    vids = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((DATA / "videos").glob("*.json"))]
    return sorted(vids, key=lambda v: v["published"], reverse=True)


def build_video(v, by_code, catalog, today):
    rows = []
    for n, entry in enumerate(v["items"], 1):
        it = by_code.get(entry["code"])
        info = it or catalog.get(entry["code"])
        if not info:
            continue
        label = entry.get("label") or f"{n}"
        price = f'<small>寄付額</small>{yen(it["price"])}<small>（{mmdd(today)}時点）</small>' if it else "現在は記録対象外です。最新の寄付額は返礼品ページでご確認ください。"
        detail = f'<a href="../item/{slug_of(entry["code"])}.html">寄付額の推移を見る</a>' if it else ""
        rows.append(f"""<li class="vcard">
  <div class="vlabel">{E(label)}</div>
  <a class="thumb" href="{E(info["url"])}" rel="sponsored nofollow noopener" target="_blank">{thumb(info)}</a>
  <div class="info">
    <div class="name">{E(info["name"][:80])}</div>
    <div class="sub"><span>{E(info["shop"])}</span></div>
    <div class="price">{price}</div>
    <a class="buy" href="{E(info["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天ふるさと納税で見る（PR）</a>
    <div class="sub">{detail}</div>
  </div>
</li>""")
    body = f"""<h1>{E(v["title"])}</h1>
<p class="updated">動画の公開日 {E(v["published"])}</p>
<p class="intro">動画で紹介した返礼品のリンクです。寄付額は変わることがあるため、申し込む前に返礼品ページでご確認ください。</p>
<ul class="vcards">{"".join(rows)}</ul>
<p class="more"><a href="index.html">紹介した返礼品の一覧へ</a></p>"""
    return page(f"{v['title']}｜紹介した返礼品", "動画で紹介した返礼品のリンク集です。", f"v/{v['id']}.html", body, 1, noindex=True)


def build_video_index(videos):
    rows = "".join(f'<li><a href="{E(v["id"])}.html"><strong>{E(v["title"])}</strong><span>{E(v["published"])}</span></a></li>' for v in videos)
    body = f"""<h1>動画で紹介した返礼品</h1>
<p class="intro">ショート動画で紹介した返礼品を、動画ごとにまとめています。</p>
{f'<ul class="genre-list">{rows}</ul>' if rows else '<p class="note">まだ動画はありません。</p>'}
<p class="more"><a href="../index.html">値上げ速報のトップへ →</a></p>"""
    return page("動画で紹介した返礼品", "ショート動画で紹介した返礼品のリンク集です。", "v/index.html", body, 1, noindex=True)


# ---------- share images ----------

def make_ogp(rel, title, sub, accent="#e5322d"):
    """1200x630 share image. Skipped when Pillow or the font is unavailable."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    font_path = ROOT / "fonts" / "DelaGothicOne-Regular.ttf"
    if not font_path.exists():
        return False
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), "#14161c")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 14], fill=accent)
    flash = ImageFont.truetype(str(font_path), 40)
    d.rounded_rectangle([64, 70, 224, 136], radius=10, fill="#ffd400")
    d.text((144, 103), "速報", font=flash, fill="#14161c", anchor="mm")
    d.text((248, 103), CFG["site_name"], font=ImageFont.truetype(str(font_path), 40), fill="#ffffff", anchor="lm")
    size = 96
    lines = title.split("\n")
    while size > 48:
        f = ImageFont.truetype(str(font_path), size)
        if max(d.textlength(l, font=f) for l in lines) <= W - 128:
            break
        size -= 4
    f = ImageFont.truetype(str(font_path), size)
    y = 200
    for l in lines:
        d.text((64, y), l, font=f, fill="#ffffff")
        y += int(size * 1.25)
    d.text((64, H - 70), sub, font=ImageFont.truetype(str(font_path), 34), fill="#ffd400", anchor="lm")
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    return True


def write(rel, content):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    for f in ("style.css", "shindan.js"):
        shutil.copy(ROOT / "assets" / f, OUT / "assets" / f)
    write(".nojekyll", "")

    catalog, snaps = load()
    today, items = analyze(catalog, snaps)
    urls = ["", "about.html"]
    write("about.html", build_about())

    events30 = price_events(snaps, catalog, 30)
    events7 = price_events(snaps, catalog, 7)
    ups7 = sum(1 for e in events7 if e["new"] > e["old"])
    stamp = f"{mmdd(today)} 更新" if today else ""
    make_ogp("ogp/default.png", "値上げされる前に、\nぴったり使い切る。", "楽天ふるさと納税の寄付額を毎朝記録")
    make_ogp("ogp/index.png", "値上げされる前に、\nぴったり使い切る。", f"{stamp}　直近7日の値上げ {ups7}件" if today else "楽天ふるさと納税の寄付額を毎朝記録")

    if not items:
        write("index.html", build_empty())
    else:
        by_code = {i["code"]: i for i in items}
        write("index.html", build_index(today, items, events7, events30, catalog))
        write("changes.html", build_changes(today, events30, catalog, len(snaps)))
        write("shindan.html", build_shindan(today))
        write("ryou.html", build_ryou(today, items))
        urls += ["changes.html", "shindan.html", "ryou.html"]
        make_ogp("ogp/changes.png", "ふるさと納税\n値上げ速報", f"{stamp}　直近7日の値上げ {ups7}件")
        make_ogp("ogp/shindan.png", "残り枠\nぴったり診断", "残りの金額で使い切れる組み合わせを探す", accent="#1d5fd1")
        make_ogp("ogp/ryou.png", "1万円で\nもらえる量ランキング", f"{stamp}　肉・魚介・米・果物・野菜", accent="#1f9d55")
        for c in CFG["categories"]:
            write(f"cat/{c['slug']}.html", build_category(c, today, items))
            urls.append(f"cat/{c['slug']}.html")
            make_ogp(f"ogp/cat-{c['slug']}.png", f"ふるさと納税\n{c['name']}の値上げ速報", stamp)
        for it in items:
            write(f"item/{it['slug']}.html", build_item(it, today))
            urls.append(f"item/{it['slug']}.html")
        write("data/items.json", json.dumps(planner_data(items), ensure_ascii=False, separators=(",", ":")))
        write("api/feed.json", json.dumps(feed(today, items, events7, catalog, len(snaps)), ensure_ascii=False, indent=1))

        videos = load_videos() if (DATA / "videos").exists() else []
        for v in videos:
            write(f"v/{v['id']}.html", build_video(v, by_code, catalog, today))
        write("v/index.html", build_video_index(videos))

    lastmod = today or date.today().isoformat()
    sitemap = "".join(f"<url><loc>{E(SITE_URL + u)}</loc><lastmod>{lastmod}</lastmod></url>" for u in urls)
    write("sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sitemap}</urlset>')
    write("robots.txt", f"User-agent: *\nAllow: /\nDisallow: /v/\nSitemap: {SITE_URL}sitemap.xml\n")
    write("404.html", page("ページが見つかりません", "", "404.html",
                           f'<h1>ページが見つかりません</h1><p><a href="{urlsplit(SITE_URL).path}">トップへ戻る</a></p>', 0))
    print(f"built {len(urls)} pages -> {OUT}")


if __name__ == "__main__":
    main()
