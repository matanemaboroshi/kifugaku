"""Build the static site from data/ into ./public."""

import html
import json
import re
import shutil
import unicodedata
from datetime import date
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
    snaps = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((DATA / "daily").glob("*.json"))]
    return catalog, snaps


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
        out.append(f'<span class="badge up">値上げ +{it["change"]:,}円</span>')
    elif it["change"] < 0:
        out.append(f'<span class="badge down">値下げ {it["change"]:,}円</span>')
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


def page(title, description, path, body, depth, jsonld=None):
    up = "../" * depth
    canonical = SITE_URL + path
    ld = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>' if jsonld else ""
    nav = "".join(f'<a href="{up}cat/{c["slug"]}.html">{E(c["name"])}</a>' for c in CFG["categories"])
    nav += f'<a href="{up}changes.html">寄付額の変更履歴</a>'
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
  <p>寄付額・レビュー数は取得時点のものです。最新情報は各返礼品ページでご確認ください。</p>
  <p><a href="https://developers.rakuten.com/" rel="noopener" target="_blank">Supported by Rakuten Developers</a></p>
</footer>
</body>
</html>
"""


def card(it, up, lead=""):
    img = f'<img src="{E(it["image"])}" alt="" loading="lazy" width="150" height="150">' if it["image"] else '<div class="noimg"></div>'
    kg = f'<span class="perkg">1kgあたり 約{yen(it["per_kg"])}</span>' if it["per_kg"] else ""
    lead = f'<div class="lead-val">{lead}</div>' if lead else ""
    return f"""<li class="card">
  <a class="thumb" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">{img}</a>
  <div class="info">
    {lead}<div class="meta">{badges(it)}<span class="genre-tag">{E(CATS[it["cat"]]["name"])}</span></div>
    <a class="name" href="{up}item/{it["slug"]}.html">{E(it["name"][:90])}</a>
    <div class="price">寄付額 {yen(it["price"])} {kg}</div>
    <div class="sub">{stars(it["review_avg"], it["reviews"])}<span>{E(it["shop"])}</span></div>
    <a class="buy" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天で見る</a>
  </div>
</li>"""


def cards(items, up, lead=None):
    return '<ul class="cards">' + "".join(card(i, up, lead(i) if lead else "") for i in items) + "</ul>"


def section(title, items, up, empty, lead=None, intro=""):
    intro = f'<p class="intro">{intro}</p>' if intro else ""
    if not items:
        return f'<section><h2>{title}</h2>{intro}<p class="note">{empty}</p></section>'
    return f"<section><h2>{title}</h2>{intro}{cards(items, up, lead)}</section>"


def by_momentum(items):
    return sorted(items, key=lambda i: (-i["growth_per_day"], -i["reviews"]))


def by_cospa(items):
    return sorted([i for i in items if i["per_kg"]], key=lambda i: i["per_kg"])


def growth_lead(i):
    return f'直近 +{i["growth"]:,}件のレビュー' if i["growth"] > 0 else ""


def cospa_lead(i):
    return f'1kgあたり 約{yen(i["per_kg"])}（約{i["kg"]:g}kg）'


WAIT = "記録を始めたばかりです。毎日のデータがたまると表示されます。"
TEIBAN_WAIT = f"定番の判定には{TEIBAN_MIN_DAYS}日分の記録が必要です。データがたまり次第、自動で表示されます。"


# ---------- pages ----------

def build_index(today, items):
    days = max(i["tracked"] for i in items)
    changed = [i for i in items if i["change"]]
    changed.sort(key=lambda i: -abs(i["change"]))
    lows = [i for i in items if is_lowest(i)]
    teiban = sorted([i for i in items if i["teiban"]], key=lambda i: -i["reviews"])[:12]
    momentum = by_momentum([i for i in items if i["growth"] > 0])[:12]
    cospa_links = "".join(
        f'<li><a href="cat/{c["slug"]}.html#cospa"><strong>{E(c["name"])}</strong><span>1kgあたりの寄付額が安い順</span></a></li>'
        for c in CFG["categories"] if c["per_kg"]
    )
    cat_links = "".join(
        f'<li><a href="cat/{c["slug"]}.html"><strong>{E(c["name"])}</strong><span>人気・勢い・寄付額の推移</span></a></li>'
        for c in CFG["categories"]
    )
    body = f"""<h1>{E(CFG["site_name"])}</h1>
<p class="lead">{E(CFG["site_description"])}</p>
<p class="updated">{today} 更新 ／ 記録 {days}日分 ／ 掲載 {len(items)}品</p>
{section("今日、寄付額が変わった返礼品", changed[:12], "", "本日、寄付額が変わった返礼品はありません。" if days > 1 else WAIT,
         intro=f'過去30日の変更は<a href="changes.html">寄付額の変更履歴</a>にまとめています。')}
{section("いま勢いのある返礼品", momentum, "", WAIT, growth_lead, "直近のレビュー増加数が多い＝いま申し込みが集まっている返礼品です。")}
{section("記録上の最安値になっている返礼品", lows[:12], "", "現在、記録上の最安値の返礼品はありません。" if days >= 7 else WAIT)}
{section("本当に売れ続けている定番", teiban, "", TEIBAN_WAIT, None, f"直近{TEIBAN_WINDOW}日のうち{int(TEIBAN_RATIO * 100)}%以上、人気上位に入り続けている返礼品です。一時的なブームではない、実績のある品だけを選んでいます。")}
<section><h2>コスパランキング（1kgあたり）</h2><ul class="genre-list">{cospa_links}</ul></section>
<section><h2>カテゴリから探す</h2><ul class="genre-list">{cat_links}</ul></section>"""
    title = f"{CFG['site_name']}｜値上げ前・最安値・コスパがわかる楽天ふるさと納税の比較"
    return page(title, CFG["site_description"], "", body, 0)


def build_category(c, today, items):
    mine = [i for i in items if i["cat"] == c["slug"]]
    popular = sorted(mine, key=lambda i: i["rank"])
    momentum = by_momentum([i for i in mine if i["growth"] > 0])[:10]
    teiban = [i for i in popular if i["teiban"]][:10]
    cospa = ""
    if c["per_kg"]:
        cospa_items = by_cospa(mine)[:20]
        cospa = f'<div id="cospa">{section(f"{c["name"]}のコスパランキング（1kgあたり寄付額）", cospa_items, "../", "対象の返礼品がありません。", cospa_lead, "重さは返礼品名から自動で算出した目安です。内容量は必ず返礼品ページでご確認ください。")}</div>'
    body = f"""<h1>ふるさと納税「{E(c["name"])}」の人気返礼品と寄付額の推移</h1>
<p class="updated">{today} 更新 ／ {len(mine)}品</p>
{section("いま勢いのある返礼品", momentum, "../", WAIT, growth_lead)}
{cospa}
{section("定番の返礼品", teiban, "../", TEIBAN_WAIT)}
{section(f"{c['name']}の人気返礼品（レビュー数順）", popular, "../", "データがありません。")}"""
    title = f"【{mmdd(today)}更新】ふるさと納税 {c['name']}のおすすめ｜値上げ・最安値・コスパを毎日チェック"
    desc = f"楽天ふるさと納税「{c['name']}」の人気返礼品{len(mine)}品を毎日記録。寄付額の値上げ・値下げ、1kgあたりのコスパ、売れ続けている定番品がわかります。"
    ld = {"@context": "https://schema.org", "@type": "ItemList", "name": title,
          "itemListElement": [{"@type": "ListItem", "position": n, "name": i["name"][:100]} for n, i in enumerate(popular[:30], 1)]}
    return page(title, desc, f"cat/{c['slug']}.html", body, 1, ld)


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
        facts.append(("1kgあたりの寄付額（目安）", f'約{yen(it["per_kg"])}（約{it["kg"]:g}kg として算出）'))
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
    <p class="price big">寄付額 {yen(it["price"])}</p>
    <p>{stars(it["review_avg"], it["reviews"])}</p>
    <p>{E(it["shop"])}</p>
    <a class="buy big" href="{E(it["url"])}" rel="sponsored nofollow noopener" target="_blank">楽天ふるさと納税で見る</a>
    <p class="updated">{today} 時点</p>
  </div>
</div>
<section><h2>買い時チェック</h2><table class="facts">{rows}</table></section>
<section class="hist"><h2>寄付額の推移</h2>{price_chart}</section>
<section class="hist"><h2>レビュー数の推移（申し込みの勢い）</h2>{review_chart}</section>
<p><a href="../cat/{it["cat"]}.html">{E(CATS[it["cat"]]["name"])}の返礼品をもっと見る</a></p>
</article>"""
    title = f"{it['name'][:45]}｜寄付額の推移・値上げ情報"
    desc = f"「{it['name'][:60]}」の寄付額の推移、値上げ・値下げ履歴、レビュー数の伸びを毎日記録。現在{yen(it['price'])}。"
    return page(title, desc, f"item/{it['slug']}.html", body, 1)


def build_changes(today, snaps, catalog):
    """All price changes seen in the last 30 snapshots, newest first."""
    window = snaps[-31:]
    events = []
    for a, b in zip(window, window[1:]):
        pa = {code: p for rows in a["categories"].values() for code, p, _ in rows}
        for slug, rows in b["categories"].items():
            for code, p, _ in rows:
                if code in pa and pa[code] and p and pa[code] != p and code in catalog:
                    events.append((b["date"], code, slug, pa[code], p))
    events.sort(key=lambda e: e[0], reverse=True)
    if events:
        rows = "".join(
            f'<tr><td>{mmdd(d)}</td><td><a href="item/{slug_of(code)}.html">{E(catalog[code]["name"][:60])}</a></td>'
            f'<td>{E(CATS.get(slug, {}).get("name", ""))}</td><td>{yen(old)} → {yen(new)}</td>'
            f'<td class="{"up" if new > old else "down"}">{"+" if new > old else ""}{new - old:,}円</td></tr>'
            for d, code, slug, old, new in events[:300]
        )
        table = f'<div class="table-wrap"><table class="changes"><thead><tr><th>日付</th><th>返礼品</th><th>カテゴリ</th><th>寄付額</th><th>差額</th></tr></thead><tbody>{rows}</tbody></table></div>'
    else:
        table = f'<p class="note">{WAIT if len(snaps) < 2 else "直近30日の寄付額の変更はありません。"}</p>'
    ups = sum(1 for e in events if e[4] > e[3])
    body = f"""<h1>ふるさと納税 寄付額の変更履歴（値上げ・値下げ）</h1>
<p class="updated">{today} 更新 ／ 直近30日：値上げ {ups}件・値下げ {len(events) - ups}件</p>
<p class="intro">人気返礼品の寄付額を毎日記録し、変更があったものを一覧にしています。気になる返礼品が値上げされる前の参考にどうぞ。</p>
{table}"""
    return page("ふるさと納税 寄付額の値上げ・値下げ履歴【毎日更新】", "楽天ふるさと納税の人気返礼品で、寄付額が値上げ・値下げされたものを毎日記録した一覧です。", "changes.html", body, 0)


def build_about():
    body = f"""<h1>このサイトについて</h1>
<p>{E(CFG["site_name"])}は、楽天ふるさと納税の人気返礼品について、寄付額とレビュー数を毎日自動で記録し、値上げ・値下げや人気の推移をわかりやすくまとめたサイトです。</p>
<h2>各指標について</h2>
<ul>
<li><strong>勢い</strong>：直近のレビュー増加数。申し込みが集まっている目安です。</li>
<li><strong>定番</strong>：直近{TEIBAN_WINDOW}日のうち{int(TEIBAN_RATIO * 100)}%以上、カテゴリ内の人気上位に入っていた返礼品です。</li>
<li><strong>記録上の最安</strong>：当サイトが記録を始めてからの最安の寄付額です。過去すべての最安値ではありません。</li>
<li><strong>1kgあたり</strong>：返礼品名に書かれた重さから自動で算出した目安です。正確な内容量は返礼品ページでご確認ください。</li>
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


def write(rel, content):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    shutil.copy(ROOT / "assets" / "style.css", OUT / "assets" / "style.css")
    write(".nojekyll", "")

    catalog, snaps = load()
    today, items = analyze(catalog, snaps)
    urls = ["", "about.html"]
    write("about.html", build_about())

    if not items:
        write("index.html", build_empty())
    else:
        write("index.html", build_index(today, items))
        write("changes.html", build_changes(today, snaps, catalog))
        urls.append("changes.html")
        for c in CFG["categories"]:
            write(f"cat/{c['slug']}.html", build_category(c, today, items))
            urls.append(f"cat/{c['slug']}.html")
        for it in items:
            write(f"item/{it['slug']}.html", build_item(it, today))
            urls.append(f"item/{it['slug']}.html")

    lastmod = today or date.today().isoformat()
    sitemap = "".join(f"<url><loc>{E(SITE_URL + u)}</loc><lastmod>{lastmod}</lastmod></url>" for u in urls)
    write("sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sitemap}</urlset>')
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n")
    write("404.html", page("ページが見つかりません", "", "404.html",
                           f'<h1>ページが見つかりません</h1><p><a href="{urlsplit(SITE_URL).path}">トップへ戻る</a></p>', 0))
    print(f"built {len(urls)} pages -> {OUT}")


if __name__ == "__main__":
    main()
