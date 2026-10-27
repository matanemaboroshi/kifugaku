"""Build the static site from data/ into ./public."""

import html
import json
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta, timezone
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
JST = timezone(timedelta(hours=9))


# ---------- data ----------

def load():
    catalog_path = DATA / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
    for info in catalog.values():
        info["name"] = display_name(info["name"])
        info["options"] = has_options(info["name"])
    snaps = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((DATA / "daily").glob("*.json"))]
    return catalog, snaps


def display_name(name):
    # Every item starts with the same "【ふるさと納税】" tag; drop it so lists show the useful part.
    return re.sub(r"^\s*【ふるさと納税】\s*", "", name).strip() or name


QTY = re.compile(r"(計|合計|総量|総重量)?\s*(\d+(?:\.\d+)?)\s*(kg|キロ|g|グラム)(?:\s*[×x]\s*(\d+))?", re.I)


# Size options ("選べる", ranges, "3kg or 5kg"): the recorded price belongs to one option and
# jumps when the listing switches options, so such moves are not shown as price drops.
OPTIONS_RE = re.compile(
    r"選べる|選択|[~〜～]\s*\d|\d\s*(?:kg|g|キロ|グラム|個|本|枚|袋|パック)?\s*(?:or|OR|または|もしくは)\s*\d|容量違い|サイズ違い"
)


# "選べる発送月" only picks the delivery timing, not the size.
TIMING_RE = re.compile(r"(?:選べる|選択できる|選択可)\s*(?:発送|配送|お届け)\s*(?:月|日|時期|日時)")


def has_options(name):
    s = TIMING_RE.sub("", unicodedata.normalize("NFKC", name))
    if OPTIONS_RE.search(s):
        return True
    # Several different weights ("500g / 1kg", "5kg 10kg 15kg") are size options too.
    weights = {round(float(num) / (1 if unit.lower() in ("kg", "キロ") else 1000) * (int(mult) if mult else 1), 3)
               for _, num, unit, mult in QTY.findall(s)}
    return len(weights) > 1


def parse_kg(name):
    """Best-effort total weight in kg from an item name, or None if unclear."""
    s = unicodedata.normalize("NFKC", name).replace("✕", "×").replace("ｘ", "×")
    # "選べる" / ranges / "1.8kg or 3.6kg" mean the listed price may not match the listed weight.
    if re.search(r"選べる|選択|[~〜～]\s*\d|\d\s*(?:kg|g|キロ|グラム)?\s*(?:or|OR|または|もしくは)\s*\d", s):
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
                change=price - prev[1] if prev and prev[1] and not info["options"] else 0,
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
                if old and p and old != p and code in catalog and not catalog[code]["options"]:
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
    return not it["options"] and it["tracked"] >= 7 and it["price"] == it["min_price"] and it["max_price"] > it["min_price"]


def badges(it):
    out = []
    if it["change"] < 0:
        out.append(f'<span class="badge sale">{-it["change"]:,}円値下げ</span>')
    if is_lowest(it):
        out.append('<span class="badge low">記録上の最安</span>')
    if it["teiban"]:
        out.append('<span class="badge std">定番</span>')
    return "".join(out)


def stars(avg, count):
    if not count:
        return ""
    # as recorded (4.79, not 4.8): the videos show the same value
    shown = f"{float(avg or 0):.2f}".rstrip("0")
    shown += "0" if shown.endswith(".") else ""
    return f'<span class="review">★{shown}<small>（{count:,}）</small></span>'


def chart_svg(points, label, fmt, invert=False, flat_note=""):
    """Line chart of (date, value) with gaps for missing values."""
    vals = [v for _, v in points if v is not None]
    if len(vals) < 2:
        return '<p class="note">推移は記録が2日分たまると表示されます。</p>'
    lo, hi = min(vals), max(vals)
    if lo == hi:
        return f'<p class="note">{E(flat_note or f"記録期間中は {fmt(lo)} で変わっていません。")}</p>'
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
        f'<text x="{w - pad}" y="{h - 6}" text-anchor="end">{mmdd(points[0][0])}〜{mmdd(points[-1][0])}</text>'
        f'<g class="line">{lines}</g><g class="dots">{dots}</g></svg>'
    )


NAV = [("index.html", "トップ"), ("changes.html", "値下げ情報"), ("shindan.html", "残り枠から探す"), ("ryou.html", "1万円あたりの量")]


# Headings use the same extra-bold serif as the Shorts captions, on every page.
FONT_HEAD = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+JP:wght@900&display=swap">')


def page(title, description, path, body, depth, jsonld=None, ogp="ogp/default.png", noindex=False, scripts="", crumbs=None,
         body_class="", head=""):
    up = "../" * depth
    canonical = SITE_URL + path
    ld = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>' if jsonld else ""
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    here = path or "index.html"
    nav = "".join(
        f'<a href="{up}{href}"{" aria-current=page" if href == here else ""}>{label}</a>' for href, label in NAV
    )
    crumb = ""
    if crumbs:
        parts = [f'<a href="{up}index.html">トップ</a>'] + [
            f'<a href="{up}{href}">{E(label)}</a>' if href else f"<span>{E(label)}</span>" for href, label in crumbs
        ]
        crumb = f'<nav class="crumbs" aria-label="パンくずリスト">{" › ".join(parts)}</nav>'
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
<meta name="theme-color" content="#14213d">
<link rel="icon" href="{up}assets/mark.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="{up}assets/apple-touch-icon.png">
{FONT_HEAD}
<link rel="stylesheet" href="{up}assets/style.css">
{ld}
{head}
</head>
<body{f' class="{body_class}"' if body_class else ""}>
<header class="site">
  <div class="head">
    <a class="logo" href="{up}index.html"><img src="{up}assets/logo.svg" alt="{E(CFG["site_name"])}" width="155" height="40"></a>
  </div>
  <nav class="tabs">{nav}</nav>
</header>
<p class="pr">本ページはプロモーション（楽天アフィリエイト）を含みます。</p>
<main>
{crumb}
{body}
</main>
<footer class="site">
  <ul>
    <li><a href="{up}about.html">このサイトについて</a></li>
    <li><a href="{up}privacy.html">プライバシーポリシー</a></li>
    <li><a href="{up}v/index.html">動画で紹介した返礼品</a></li>
  </ul>
  <p>寄付額・レビュー数は記録時点のものです。申し込みの前に各返礼品ページで最新の情報をご確認ください。</p>
  <p><a href="https://developers.rakuten.com/" rel="noopener" target="_blank">Supported by Rakuten Developers</a></p>
</footer>
{scripts}
</body>
</html>
"""


def thumb(it, size=150):
    if it.get("image"):
        return f'<img src="{E(it["image"])}" alt="" loading="lazy" width="{size}" height="{size}">'
    return '<span class="noimg"></span>'


def tile(it, up, note=""):
    note = f'<p class="tile-note">{note}</p>' if note else ""
    old = f'<s>{yen(it["prev_price"])}</s>' if it["change"] < 0 and it["prev_price"] else ""
    return f"""<li class="tile">
  <a class="tile-link" href="{up}item/{it["slug"]}.html">
    <span class="tile-img">{thumb(it, 300)}</span>
    <span class="tile-badges">{badges(it)}</span>
    <span class="tile-name">{E(it["name"][:70])}</span>
    <span class="tile-shop">{E(it["shop"])}</span>
    <span class="tile-price">{old}{yen(it["price"])}</span>
  </a>
  {note}
</li>"""


def tiles(items, up, note=None):
    return '<ul class="tiles">' + "".join(tile(i, up, note(i) if note else "") for i in items) + "</ul>"


def section(title, items, up, empty, note=None, lead="", more=""):
    lead = f'<p class="lead">{lead}</p>' if lead else ""
    more = f'<p class="more">{more}</p>' if more else ""
    if not items:
        return f'<section><h2>{title}</h2>{lead}<p class="note">{empty}</p></section>'
    return f"<section><h2>{title}</h2>{lead}{tiles(items, up, note)}{more}</section>"


def sale_table(events, catalog, up, limit):
    rows = []
    for e in events[:limit]:
        info = catalog[e["code"]]
        diff = e["old"] - e["new"]
        rows.append(f"""<tr>
  <td class="t-img"><a href="{up}item/{slug_of(e["code"])}.html">{thumb(info, 56)}</a></td>
  <td class="t-name"><a href="{up}item/{slug_of(e["code"])}.html">{E(info["name"][:60])}</a><small>{E(info["shop"])}・{E(CATS.get(e["cat"], {}).get("name", ""))}・{mmdd(e["date"])}</small></td>
  <td class="t-price"><s>{yen(e["old"])}</s><b>{yen(e["new"])}</b><em class="down">{diff:,}円下がった</em><small>{diff / e["old"] * 100:.0f}%</small></td>
</tr>""")
    return f'<table class="sale"><thead><tr><th></th><th>返礼品</th><th>寄付額</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'


def by_momentum(items):
    return sorted(items, key=lambda i: (-i["growth_per_day"], -i["reviews"]))


def by_ryou(items):
    return sorted([i for i in items if i["per_kg"]], key=lambda i: i["per_kg"])


def growth_note(i):
    return f'直近のレビュー +{i["growth"]:,}件' if i["growth"] > 0 else ""


def ryou_note(i):
    return f'1万円あたり 約{kg_per_10k(i):g}kg'


WAIT = "まだ表示できる記録がありません。数日後に表示されます。"
TEIBAN_WAIT = f"定番の判定には{TEIBAN_MIN_DAYS}日分の記録が必要です。"


def shindan_form(action, value=""):
    return f"""<form class="finder" action="{action}" method="get">
  <label for="r">控除上限の残り</label>
  <div class="finder-row">
    <span class="field"><input id="r" name="r" type="number" inputmode="numeric" min="1000" step="1000" placeholder="23000" value="{E(value)}" required><span>円</span></span>
    <button type="submit">探す</button>
  </div>
</form>"""


def cat_grid(items, up):
    cells = "".join(
        f'<li><a href="{up}cat/{c["slug"]}.html"><b>{E(c["name"])}</b></a></li>'
        for c in CFG["categories"]
    )
    return f'<ul class="cat-grid">{cells}</ul>'


# ---------- pages ----------

def build_index(today, items, events7, catalog, videos=()):
    sales = [e for e in events7 if e["new"] < e["old"]]
    teiban = sorted([i for i in items if i["teiban"]], key=lambda i: -i["reviews"])[:8]
    momentum = by_momentum([i for i in items if i["growth"] > 0])[:8]
    best_ryou = []
    for c in CFG["categories"]:
        if c["per_kg"]:
            best_ryou += by_ryou([i for i in items if i["cat"] == c["slug"]])[:1]
    sale_html = (f'<section><h2>値下げされた返礼品（直近7日）</h2>{sale_table(sales, catalog, "", 10)}'
                 '<p class="more"><a href="changes.html">値下げ情報をすべて見る</a></p></section>') if sales else ""
    body = f"""{hero(best_ryou)}
<section class="tool">
  <h2 class="tool-h">控除上限の残りから返礼品を探す</h2>
  {shindan_form("shindan.html")}
</section>
<p class="status">{mmdd(today)} 6:00 更新</p>
{sale_html}
{video_teaser(videos)}
<section><h2>カテゴリから探す</h2>{cat_grid(items, "")}</section>
{section("申し込みが増えている返礼品", momentum, "", WAIT, growth_note)}
{section("定番の返礼品", teiban, "", TEIBAN_WAIT)}"""
    title = f"{CFG['site_name']}｜楽天ふるさと納税の値下げ情報・残り枠から探す"
    return page(title, CFG["site_description"], "", body, 0, ogp="ogp/index.png")


def hero(best_ryou):
    """Top-page opener in the Shorts look: red rays, and the three categories' best amount per 10,000 yen."""
    top = sorted(best_ryou, key=lambda i: -kg_per_10k(i))[:3]
    cards = "".join(
        f'<li><a href="item/{i["slug"]}.html"><span class="p-img">{thumb(i, 150)}</span>'
        f'<b>{kg_per_10k(i):g}<small>kg</small></b><span>{E(CATS[i["cat"]]["name"])}</span></a></li>'
        for i in top
    )
    if not top:
        return ""
    return f"""<section class="hero"><div class="rays" aria-hidden="true"></div>
<h1>寄付1万円あたり<br>量が多い返礼品</h1>
<ol class="podium">{cards}</ol>
<p class="hero-cap"><a href="ryou.html">カテゴリ別に見る</a></p>
</section>"""


def video_teaser(videos, limit=3):
    """Latest public videos on the top page, so site visitors find the channel and back."""
    vids = [v for v in videos if video_is_public(v)][:limit]
    if not vids:
        return ""
    rows = "".join(f'<li><a href="v/{E(v["id"])}.html"><b>{E(v["title"])}</b><small>{E(v["published"])}</small></a></li>' for v in vids)
    return f"""<section class="vteaser"><h2>ショート動画で紹介した返礼品</h2>
<ul class="vmore">{rows}</ul>
<p class="more"><a href="v/index.html">動画で紹介した返礼品をすべて見る</a></p></section>"""


def build_category(c, today, items):
    mine = [i for i in items if i["cat"] == c["slug"]]
    popular = sorted(mine, key=lambda i: i["rank"])
    momentum = by_momentum([i for i in mine if i["growth"] > 0])[:8]
    sales = sorted([i for i in mine if i["change"] < 0], key=lambda i: i["change"])[:8]
    ryou = ""
    if c["per_kg"]:
        ryou = f'<div id="ryou">{section("寄付額1万円あたりの量", by_ryou(mine)[:12], "../", "対象の返礼品がありません。", ryou_note)}</div>'
    body = f"""<h1>{E(c["name"])}の返礼品</h1>
{section("今日値下げされた返礼品", sales, "../", "") if sales else ""}
{ryou}
{section("申し込みが増えている返礼品", momentum, "../", WAIT, growth_note)}
{section("レビューの多い順", popular, "../", "データがありません。")}"""
    title = f"ふるさと納税 {c['name']}の返礼品一覧｜値下げ情報・寄付額の推移【{mmdd(today)}更新】"
    desc = f"楽天ふるさと納税「{c['name']}」の人気返礼品{len(mine)}品の寄付額を比べられます。値下げされた返礼品や寄付額の推移を確認できます。"
    ld = {"@context": "https://schema.org", "@type": "ItemList", "name": title,
          "itemListElement": [{"@type": "ListItem", "position": n, "name": i["name"][:100]} for n, i in enumerate(popular[:30], 1)]}
    return page(title, desc, f"cat/{c['slug']}.html", body, 1, ld, ogp=f"ogp/cat-{c['slug']}.png", crumbs=[(None, c["name"])])


def build_ryou(today, items):
    blocks = []
    for c in CFG["categories"]:
        if not c["per_kg"]:
            continue
        top = by_ryou([i for i in items if i["cat"] == c["slug"]])[:4]
        blocks.append(section(c["name"], top, "", "対象の返礼品がありません。", ryou_note,
                              more=f'<a href="cat/{c["slug"]}.html#ryou">{E(c["name"])}をもっと見る</a>'))
    body = f"""<h1>寄付額1万円あたりの量</h1>
{"".join(blocks)}"""
    return page(f"ふるさと納税 寄付額1万円あたりの量ランキング【{mmdd(today)}更新】",
                "楽天ふるさと納税の肉・魚介・米・果物・野菜を、寄付額1万円あたりの量で比べています。",
                "ryou.html", body, 0, ogp="ogp/ryou.png", crumbs=[(None, "1万円あたりの量")])


def build_item(it, today):
    hist = it["history"]
    price_chart = chart_svg([(d, p) for d, p, _, _ in hist], "寄付額の推移", yen,
                            flat_note=f"記録期間中（{it['tracked']}日）、寄付額は {yen(it['price'])} で変わっていません。")
    review_chart = chart_svg([(d, r) for d, _, r, _ in hist], "レビュー数の推移", lambda v: f"{v:,.0f}件")
    facts = [
        ("寄付額", yen(it["price"])),
        ("記録上の最安 / 最高", f'{yen(it["min_price"])} / {yen(it["max_price"])}'),
        ("最後の寄付額変更", f'{it["last_change"]["date"]}：{yen(it["last_change"]["from"])} → {yen(it["last_change"]["to"])}' if it["last_change"] else "記録期間中の変更なし"),
        ("カテゴリ内の順位（レビュー数）", f'{CATS[it["cat"]]["name"]} {it["rank"]}位'),
        ("直近のレビュー増加", f'+{it["growth"]:,}件' if it["tracked"] > 1 else "記録中"),
    ]
    if it["per_kg"]:
        facts.append(("1万円あたりの量（目安）", f'約{kg_per_10k(it):g}kg（内容量 約{it["kg"]:g}kgとして算出）'))
    facts.append(("記録日数", f'{it["tracked"]}日'))
    rows = "".join(f"<tr><th>{E(k)}</th><td>{E(v)}</td></tr>" for k, v in facts)
    img = f'<img src="{E(it["image"])}" alt="{E(it["name"][:60])}" width="300" height="300">' if it["image"] else ""
    body = f"""<article class="item">
<div class="item-head">
  <a class="item-img" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">{img}</a>
  <div class="item-main">
    <h1>{E(it["name"])}</h1>
    <p class="item-shop">{E(it["shop"])}</p>
    <div class="tile-badges">{badges(it)}</div>
    <p class="item-price"><small>寄付額</small>{yen(it["price"])}</p>
    <p>{stars(it["review_avg"], it["reviews"])}</p>
    <a class="btn" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天ふるさと納税で申し込む</a>
    <p class="status">{today} 時点の情報です</p>
  </div>
</div>
<section><h2>記録データ</h2><table class="facts">{rows}</table></section>
<section><h2>寄付額の推移</h2><div class="panel">{price_chart}</div></section>
<section><h2>レビュー数の推移</h2><div class="panel">{review_chart}</div></section>
</article>"""
    title = f"{it['name'][:45]}｜寄付額の推移"
    desc = f"「{it['name'][:60]}」（{it['shop']}）の寄付額とレビュー数の推移です。現在の寄付額は{yen(it['price'])}です。"
    return page(title, desc, f"item/{it['slug']}.html", body, 1,
                crumbs=[(f"cat/{it['cat']}.html", CATS[it["cat"]]["name"]), (None, it["name"][:24] + ("…" if len(it["name"]) > 24 else ""))])


def build_changes(today, events30, catalog, n_snaps):
    sales = [e for e in events30 if e["new"] < e["old"]]
    empty = WAIT if n_snaps < 2 else "直近30日間に値下げされた返礼品はありません。"
    body = f"""<h1>値下げ情報</h1>
<p class="status">{mmdd(today)} 6:00 更新</p>
{sale_table(sales, catalog, "", 200) if sales else f'<p class="note">{empty}</p>'}"""
    return page(f"ふるさと納税 値下げ情報【{mmdd(today)}更新】寄付額が下がった返礼品",
                "楽天ふるさと納税の人気返礼品のうち、寄付額が下がったものの一覧です。",
                "changes.html", body, 0, ogp="ogp/changes.png", crumbs=[(None, "値下げ情報")])


def build_shindan(today):
    chips = "".join(f'<label class="chk"><input type="checkbox" name="c" value="{c["slug"]}">{E(c["name"])}</label>' for c in CFG["categories"])
    body = f"""<section class="tool">
  <h1>控除上限の残りから返礼品を探す</h1>
  {shindan_form("shindan.html")}
  <fieldset class="chks" id="chips"><legend>カテゴリ（未選択はすべて）</legend>{chips}</fieldset>
</section>
<div id="result" aria-live="polite"></div>
<section class="guide">
  <h2>使い方と注意</h2>
  <ul>
    <li>{today}時点の寄付額をもとに、合計が残りの金額以内でなるべく近くなる1〜3品の組み合わせを表示します。</li>
    <li>控除上限額は年収や家族構成で変わります。<a href="https://www.soumu.go.jp/main_sosiki/jichi_zeisei/czaisei/czaisei_seido/furusato/" rel="noopener" target="_blank">総務省 ふるさと納税ポータルサイト</a>などでご確認ください。</li>
    <li>寄付額は変わることがあります。申し込みの前に各返礼品ページでご確認ください。</li>
  </ul>
</section>"""
    return page(f"控除上限の残りから返礼品を探す｜{CFG['site_name']}",
                "控除上限の残りの金額を入力すると、楽天ふるさと納税の人気返礼品から合計が近くなる組み合わせを表示します。",
                "shindan.html", body, 0, ogp="ogp/shindan.png", scripts='<script src="assets/shindan.js" defer></script>',
                crumbs=[(None, "残り枠から探す")])


def build_about():
    body = f"""<h1>このサイトについて</h1>
<p>{E(CFG["site_name"])}は、楽天ふるさと納税の人気返礼品の寄付額とレビュー数を毎日自動で記録し、一覧にしているサイトです。</p>
<h2>表示している項目</h2>
<dl class="defs">
<dt>値下げ</dt><dd>前回の記録より寄付額が下がった返礼品です。毎朝6時ごろに記録します。</dd>
<dt>1万円あたりの量</dt><dd>返礼品名に書かれた重さを寄付額で割った目安です。正確な内容量は返礼品ページでご確認ください。</dd>
<dt>申し込みが増えている</dt><dd>直近のレビュー増加数が多い返礼品です。</dd>
<dt>定番</dt><dd>直近{TEIBAN_WINDOW}日のうち{int(TEIBAN_RATIO * 100)}%以上の日で、カテゴリ内の上位に入った返礼品です。</dd>
<dt>記録上の最安</dt><dd>当サイトが記録を始めてからの最安の寄付額です。過去すべての最安値ではありません。</dd>
</dl>
<h2>広告について</h2>
<p>当サイトは楽天アフィリエイトに参加しています。リンク先で寄付が行われた場合、運営者が紹介料を受け取ることがあります。表示順はデータに基づいて機械的に決めており、紹介料の有無は関係ありません。</p>
<h2>データの取得元</h2>
<p>寄付額・レビュー情報は楽天ウェブサービスのAPIから取得しています。控除上限額などの税制については、お住まいの自治体や公式の情報をご確認ください。</p>
<h2>免責事項</h2>
<p>当サイトの情報によって生じた損害について、運営者は責任を負いかねます。</p>"""
    return page(f"このサイトについて｜{CFG['site_name']}", "表示項目の見方、広告表記、データの取得元について。", "about.html", body, 0,
                crumbs=[(None, "このサイトについて")])


def build_privacy():
    body = f"""<h1>プライバシーポリシー</h1>
<p class="status">制定日：2026年10月26日</p>
<p>{E(CFG["site_name"])}（以下「当サイト」）と、当サイトの運営者が運営する YouTube チャンネル「返礼品ランキングちゃんねる」（以下「当チャンネル」）における、情報の取り扱いについて定めます。</p>
<h2>当サイトが集める情報</h2>
<p>当サイトは、お名前・メールアドレスなどの個人情報の入力を求めません。アクセス解析ツール（Google アナリティクスなど）や、当サイト独自の Cookie は使っていません。「残り枠から探す」に入力した金額は、お使いの端末の中だけで計算に使い、どこにも送信・保存しません。</p>
<h2>外部のサービスに送られる情報</h2>
<ul>
<li>楽天市場の商品画像：楽天の画像サーバーから読み込むため、お使いの端末の IP アドレスなどが楽天グループ株式会社に送られます。</li>
<li>文字の表示（動画で紹介した返礼品のページ）：Google Fonts を使うため、IP アドレスなどが Google LLC に送られます。</li>
<li>ページの配信：当サイトは GitHub Pages（GitHub, Inc.）で配信しています。</li>
<li>楽天アフィリエイト：返礼品のリンクを押すと楽天のサイトに移動し、楽天が Cookie などで紹介元を記録します。取り扱いは<a href="https://privacy.rakuten.co.jp/" rel="noopener" target="_blank">楽天グループのプライバシーポリシー</a>をご覧ください。</li>
</ul>
<h2>YouTube API サービスの利用</h2>
<p>当チャンネルは、動画の投稿と再生数などの集計のために YouTube API サービスを使っています。使うのは運営者自身のチャンネルの情報だけで、視聴者の方の情報を取得・保存することはありません。YouTube の利用には<a href="https://www.youtube.com/t/terms" rel="noopener" target="_blank">YouTube 利用規約</a>が適用され、Google による情報の取り扱いは<a href="https://policies.google.com/privacy" rel="noopener" target="_blank">Google プライバシーポリシー</a>に従います。運営者のチャンネルへのアクセスは、<a href="https://security.google.com/settings/security/permissions" rel="noopener" target="_blank">Google アカウントの設定画面</a>からいつでも取り消せます。</p>
<h2>広告について</h2>
<p>当サイトと当チャンネルは楽天アフィリエイトに参加しています。リンク先で寄付が行われた場合、運営者が紹介料を受け取ることがあります。</p>
<h2>お問い合わせ</h2>
<p>このポリシーについてのお問い合わせは、<a href="https://github.com/matanemaboroshi/kifugaku/issues" rel="noopener" target="_blank">GitHub の Issues</a> からお願いします。</p>
<h2>改定</h2>
<p>このポリシーは、必要に応じて改定し、このページでお知らせします。</p>"""
    return page(f"プライバシーポリシー｜{CFG['site_name']}", "当サイトと YouTube チャンネルにおける情報の取り扱いについて。", "privacy.html", body, 0,
                crumbs=[(None, "プライバシーポリシー")])


def build_empty():
    body = '<h1>準備中</h1><p class="note">返礼品データの初回取得を待っています。</p>'
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
        "price_down_7d": [event(e) for e in events7 if e["new"] < e["old"]][:50],
        "kg_per_10k": {c["slug"]: [item_record(i) for i in by_ryou([i for i in items if i["cat"] == c["slug"]])[:10]]
                       for c in CFG["categories"] if c["per_kg"]},
        "momentum": [dict(item_record(i), review_growth=i["growth"]) for i in by_momentum([i for i in items if i["growth"] > 0])[:20]],
        "teiban": [item_record(i) for i in sorted([i for i in items if i["teiban"]], key=lambda i: -i["reviews"])[:20]],
        "by_category": {c["slug"]: [item_record(i) for i in sorted([i for i in items if i["cat"] == c["slug"]], key=lambda i: i["rank"])[:10]]
                        for c in CFG["categories"]},
    }


# ---------- per-video link pages ----------

def load_videos():
    vids = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((DATA / "videos").glob("*.json"))]
    # the id becomes a file name and a URL: letters, digits and hyphens only (docs/shorts-integration.md)
    vids = [v for v in vids if re.fullmatch(r"[0-9A-Za-z-]+", str(v.get("id", ""))) and v.get("published")]
    return sorted(vids, key=lambda v: v["published"], reverse=True)


def video_is_public(v, now=None):
    """Videos are scheduled ahead; list them only from their publish date (JST)."""
    return v["published"] <= (now or datetime.now(JST)).date().isoformat()


def yt_url(v):
    return f"https://www.youtube.com/shorts/{v['youtube_id']}" if v.get("youtube_id") else ""


def video_rows(v, by_code, catalog, today):
    rows = []
    for n, entry in enumerate(v["items"], 1):
        it = by_code.get(entry["code"])
        info = it or catalog.get(entry["code"])
        if not info:
            continue
        label = entry.get("label") or f"{n}位"
        short = entry.get("name") or info["name"][:24]
        if it:
            old = f'<s>{yen(it["prev_price"])}</s>' if it["change"] < 0 and it["prev_price"] else ""
            price = f'<p class="vprice">{old}{yen(it["price"])}<small>{mmdd(today)}時点</small></p>'
            per = f'<p class="vper">1万円あたり 約{kg_per_10k(it):g}kg（目安）</p>' if it.get("per_kg") else ""
            meta = f'<div class="vmeta">{badges(it)}{stars(it["review_avg"], it["reviews"])}</div>'
            hist = f'<a class="vhist" href="../item/{E(it["slug"])}.html">寄付額の推移を見る ›</a>'
        else:
            price = '<p class="vprice"><small>記録対象外のため、寄付額は返礼品ページでご確認ください。</small></p>'
            per = meta = hist = ""
        rows.append(f"""<li class="vcard">
  <p class="vrank">{E(label)}</p>
  <div class="vgrid">
    <a class="vimg" href="{E(info["url"])}" rel="sponsored nofollow noopener" target="_blank">{thumb(info, 120)}</a>
    <div class="vbody">
      <p class="vshop">{E(info["shop"])}</p>
      <p class="vshort">{E(short)}</p>
      {meta}
      {price}
      {per}
    </div>
  </div>
  <p class="vname">{E(info["name"][:90])}</p>
  <a class="vbtn" href="{E(info["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天ふるさと納税で見る</a>
  {hist}
</li>""")
    return rows


def video_more(videos, current_id=None, limit=3):
    others = [o for o in videos if video_is_public(o) and o["id"] != current_id][:limit]
    vids = "".join(f'<li><a href="{E(o["id"])}.html"><b>{E(o["title"])}</b><small>{E(o["published"])}</small></a></li>' for o in others)
    vids = f'<h2 class="vh2">ほかの動画で紹介した返礼品</h2><ul class="vmore">{vids}</ul>' if vids else ""
    return f"""{vids}
<h2 class="vh2">サイトでもっと探す</h2>
<ul class="vjump">
  <li><a href="../changes.html"><b>値下げ情報</b><small>前日より寄付額が下がった返礼品</small></a></li>
  <li><a href="../ryou.html"><b>1万円あたりの量</b><small>肉・魚介・米・果物・野菜</small></a></li>
  <li><a href="../shindan.html"><b>残り枠から探す</b><small>金額を入れると組み合わせを表示</small></a></li>
</ul>"""


def build_video(v, by_code, catalog, today, videos=()):
    rows = video_rows(v, by_code, catalog, today)
    notes = "".join(f'<p class="vnote">{E(v[k])}</p>' for k in ("ranking", "note") if v.get(k))
    watch = ""
    if yt_url(v) and video_is_public(v):
        watch = f'<a class="vwatch" href="{E(yt_url(v))}" rel="noopener" target="_blank">▶ 動画をもう一度見る</a>'
    body = f"""<section class="vhero">
  <p class="vkicker">ショート動画で紹介した返礼品</p>
  <h1>{E(v["title"])}</h1>
  {notes}
  <p class="vnote">動画の公開日 {E(v["published"])}</p>
  {watch}
</section>
<ol class="vlist">{"".join(rows)}</ol>
{video_more(list(videos), v["id"])}"""
    return page(f"{v['title']}｜紹介した返礼品", "動画で紹介した返礼品のリンクです。", f"v/{v['id']}.html", body, 1, noindex=True,
                crumbs=[("v/index.html", "動画で紹介した返礼品"), (None, v["title"])], body_class="vpage")


def build_video_index(videos, by_code=None, catalog=None):
    by_code, catalog = by_code or {}, catalog or {}
    cards = []
    for v in videos:
        if not video_is_public(v):
            continue
        thumbs = []
        for entry in v["items"][:3]:
            info = by_code.get(entry["code"]) or catalog.get(entry["code"])
            if info:
                thumbs.append(f'<span class="vt">{thumb(info, 96)}<small>{E(entry.get("label", ""))}</small></span>')
        cards.append(f"""<li class="vcard vindex">
  <a href="{E(v["id"])}.html">
    <p class="vdate">{E(v["published"])}</p>
    <p class="vtitle">{E(v["title"])}</p>
    <span class="vthumbs">{"".join(thumbs)}</span>
    <span class="vbtn">紹介した返礼品を見る</span>
  </a>
</li>""")
    lst = f'<ol class="vlist">{"".join(cards)}</ol>' if cards else '<p class="note">まだ動画はありません。</p>'
    body = f"""<section class="vhero">
  <p class="vkicker">YouTube ショートで紹介した返礼品</p>
  <h1>動画で紹介した返礼品</h1>
</section>
{lst}
{video_more([], None)}"""
    return page("動画で紹介した返礼品", "ショート動画で紹介した返礼品のリンク集です。", "v/index.html", body, 1, noindex=True,
                crumbs=[(None, "動画で紹介した返礼品")], body_class="vpage")


def make_ogp(rel, title, sub):
    """1200x630 share image: navy logo band, navy title, red footer band (the Shorts colors). Skipped without Pillow or the font."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    font_path = ROOT / "fonts" / "DelaGothicOne-Regular.ttf"
    if not font_path.exists():
        return False
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 130], fill="#14213d")
    logo_path = ROOT / "brand" / "logo-dark.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo = logo.resize((int(logo.width * 96 / logo.height), 96), Image.LANCZOS)
        img.paste(logo, (56, 17), logo)
    else:
        d.text((64, 65), CFG["site_name"], font=ImageFont.truetype(str(font_path), 44), fill="#ffffff", anchor="lm")
    size = 92
    lines = title.split("\n")
    while size > 48:
        f = ImageFont.truetype(str(font_path), size)
        if max(d.textlength(l, font=f) for l in lines) <= W - 128:
            break
        size -= 4
    f = ImageFont.truetype(str(font_path), size)
    y = 190
    for l in lines:
        d.text((64, y), l, font=f, fill="#14213d")
        y += int(size * 1.3)
    d.rectangle([0, H - 110, W, H], fill="#e0262b")
    d.text((64, H - 55), sub, font=ImageFont.truetype(str(font_path), 34), fill="#ffffff", anchor="lm")
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
    for f in ("style.css", "shindan.js", "logo.svg", "mark.svg", "apple-touch-icon.png"):
        shutil.copy(ROOT / "assets" / f, OUT / "assets" / f)
    write(".nojekyll", "")

    catalog, snaps = load()
    today, items = analyze(catalog, snaps)
    urls = ["", "about.html"]
    write("about.html", build_about())
    write("privacy.html", build_privacy())
    urls.append("privacy.html")

    events30 = price_events(snaps, catalog, 30)
    events7 = price_events(snaps, catalog, 7)
    downs7 = sum(1 for e in events7 if e["new"] < e["old"])
    stamp = f"{mmdd(today)} 更新" if today else "毎日更新"
    make_ogp("ogp/default.png", "ふるさと納税の返礼品を\n寄付額で比べる", "値下げ情報・残り枠から探す・1万円あたりの量")
    make_ogp("ogp/index.png", "ふるさと納税の返礼品を\n寄付額で比べる", f"{stamp}　直近7日の値下げ {downs7}件")

    if not items:
        write("index.html", build_empty())
    else:
        by_code = {i["code"]: i for i in items}
        videos = load_videos() if (DATA / "videos").exists() else []
        write("index.html", build_index(today, items, events7, catalog, videos))
        write("changes.html", build_changes(today, events30, catalog, len(snaps)))
        write("shindan.html", build_shindan(today))
        write("ryou.html", build_ryou(today, items))
        urls += ["changes.html", "shindan.html", "ryou.html"]
        make_ogp("ogp/changes.png", "ふるさと納税\n値下げ情報", f"{stamp}　直近7日の値下げ {downs7}件")
        make_ogp("ogp/shindan.png", "控除上限の残りから\n返礼品を探す", "合計が残りの金額に近い組み合わせを表示")
        make_ogp("ogp/ryou.png", "寄付額1万円あたりの量", f"{stamp}　肉・魚介・米・果物・野菜")
        for c in CFG["categories"]:
            write(f"cat/{c['slug']}.html", build_category(c, today, items))
            urls.append(f"cat/{c['slug']}.html")
            make_ogp(f"ogp/cat-{c['slug']}.png", f"ふるさと納税\n{c['name']}の返礼品", stamp)
        for it in items:
            write(f"item/{it['slug']}.html", build_item(it, today))
            urls.append(f"item/{it['slug']}.html")
        write("data/items.json", json.dumps(planner_data(items), ensure_ascii=False, separators=(",", ":")))
        write("api/feed.json", json.dumps(feed(today, items, events7, catalog, len(snaps)), ensure_ascii=False, indent=1))

        for v in videos:
            write(f"v/{v['id']}.html", build_video(v, by_code, catalog, today, videos))
        write("v/index.html", build_video_index(videos, by_code, catalog))

    lastmod = today or date.today().isoformat()
    sitemap = "".join(f"<url><loc>{E(SITE_URL + u)}</loc><lastmod>{lastmod}</lastmod></url>" for u in urls)
    write("sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sitemap}</urlset>')
    write("robots.txt", f"User-agent: *\nAllow: /\nDisallow: /v/\nSitemap: {SITE_URL}sitemap.xml\n")
    write("404.html", page("ページが見つかりません", "", "404.html",
                           f'<h1>ページが見つかりません</h1><p><a href="{urlsplit(SITE_URL).path}">トップへ戻る</a></p>', 0))
    print(f"built {len(urls)} pages -> {OUT}")


if __name__ == "__main__":
    main()
