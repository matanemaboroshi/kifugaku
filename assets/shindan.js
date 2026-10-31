// Planner: find gift combinations whose total is as close as possible to the
// remaining deduction amount without going over it.
(function () {
  "use strict";
  var result = document.getElementById("result");
  var form = document.querySelector(".finder");
  var input = document.getElementById("r");
  var items = null;

  function yen(n) { return n.toLocaleString("ja-JP") + "円"; }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function selectedCats() {
    var out = {};
    document.querySelectorAll('#chips input:checked').forEach(function (el) { out[el.value] = true; });
    return out;
  }

  // Items grouped by price, most-reviewed first, so each price maps to its best picks.
  function groupByPrice(list) {
    var map = new Map();
    list.forEach(function (it) {
      if (!map.has(it.p)) map.set(it.p, []);
      map.get(it.p).push(it);
    });
    map.forEach(function (arr) { arr.sort(function (a, b) { return b.r - a.r; }); });
    return map;
  }

  // Largest value in ascending array `arr` that is <= x and >= min, or -1.
  function floorIn(arr, x, min) {
    var lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (arr[mid] <= x) { ans = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return ans >= 0 && arr[ans] >= min ? arr[ans] : -1;
  }

  function pick(map, prices) {
    // Take distinct items for repeated prices (e.g. 10,000 + 10,000).
    var used = {}, out = [];
    for (var k = 0; k < prices.length; k++) {
      var p = prices[k], arr = map.get(p), idx = used[p] || 0;
      if (!arr || idx >= arr.length) return null;
      out.push(arr[idx]);
      used[p] = idx + 1;
    }
    return out;
  }

  function search(R, cats) {
    var list = items.filter(function (it) {
      return it.p <= R && (Object.keys(cats).length === 0 || cats[it.c]);
    });
    var map = groupByPrice(list);
    var P = Array.from(map.keys()).sort(function (a, b) { return a - b; });
    var combos = [];

    P.forEach(function (a) { combos.push([a]); });
    P.forEach(function (a) {
      var b = floorIn(P, R - a, a);
      if (b > 0) combos.push([a, b]);
    });
    P.forEach(function (a) {
      P.forEach(function (b) {
        if (b < a || a + b >= R) return;
        var c = floorIn(P, R - a - b, b);
        if (c > 0) combos.push([a, b, c]);
      });
    });

    var seen = {}, scored = [];
    combos.forEach(function (prices) {
      var key = prices.join("+");
      if (seen[key]) return;
      seen[key] = true;
      var its = pick(map, prices);
      if (!its) return;
      var total = prices.reduce(function (s, p) { return s + p; }, 0);
      var reviews = its.reduce(function (s, it) { return s + it.r; }, 0);
      scored.push({ items: its, total: total, left: R - total, reviews: reviews });
    });
    // Closest to the remaining amount first; fewer items, then more reviews break ties.
    scored.sort(function (x, y) {
      return x.left - y.left || x.items.length - y.items.length || y.reviews - x.reviews;
    });
    return { combos: scored.slice(0, 5), within: list.slice().sort(function (a, b) { return b.r - a.r; }) };
  }

  var PAGE = 20;

  function itemRow(it) {
    var img = it.i ? '<img src="' + esc(it.i) + '" alt="" loading="lazy" width="56" height="56">' : '<span class="noimg"></span>';
    return '<li><a href="item/' + esc(it.h) + '.html">' + img + '<span><b>' + esc(it.n) + '</b><small>' + esc(it.s) +
      '</small></span><em>' + yen(it.p) + '</em></a></li>';
  }

  function showMore(list, shown) {
    var ul = document.getElementById("within");
    var btn = document.getElementById("more");
    var next = list.slice(shown, shown + PAGE);
    ul.insertAdjacentHTML("beforeend", next.map(itemRow).join(""));
    shown += next.length;
    if (shown >= list.length) { btn.remove(); return; }
    btn.textContent = "もっと見る（残り" + (list.length - shown).toLocaleString("ja-JP") + "件）";
    btn.onclick = function () { showMore(list, shown); };
  }

  function render(R, res) {
    var combos = res.combos, within = res.within;
    if (!within.length) {
      result.innerHTML = '<p class="note">' + yen(R) + '以内の返礼品が見つかりませんでした。金額かカテゴリを変えてください。</p>';
      return;
    }
    var html = '<section><h2>' + yen(R) + 'に近い組み合わせ</h2><table class="combos"><thead><tr><th>合計</th><th>残り</th><th>返礼品</th></tr></thead><tbody>';
    combos.forEach(function (c) {
      html += '<tr><td class="c-total">' + yen(c.total) + '</td><td class="c-left' + (c.left === 0 ? ' zero' : '') + '">' + yen(c.left) + '</td><td><ul>';
      c.items.forEach(function (it) {
        var img = it.i ? '<img src="' + esc(it.i) + '" alt="" loading="lazy" width="48" height="48">' : '<span class="noimg"></span>';
        html += '<li><a href="item/' + esc(it.h) + '.html">' + img + '<span><b>' + esc(it.n) + '</b><small>' + esc(it.s) + '・' + yen(it.p) + '</small></span></a></li>';
      });
      html += '</ul></td></tr>';
    });
    html += '</tbody></table></section>';
    html += '<section><h2>' + yen(R) + '以内の返礼品 ' + within.length.toLocaleString("ja-JP") + '件</h2>' +
      '<ul class="within" id="within"></ul><button type="button" class="more-btn" id="more"></button></section>';
    result.innerHTML = html;
    showMore(within, 0);
  }

  function run() {
    var R = parseInt(input.value, 10);
    if (!(R >= 1000)) return;
    var go = function () { render(R, search(R, selectedCats())); };
    if (items) return go();
    result.innerHTML = '<p class="note">読み込み中です。</p>';
    fetch("data/items.json").then(function (r) { return r.json(); }).then(function (data) {
      items = data;
      go();
    }).catch(function () {
      result.innerHTML = '<p class="note">データを読み込めませんでした。時間をおいて再度お試しください。</p>';
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var url = new URL(location.href);
    url.searchParams.set("r", input.value);
    history.replaceState(null, "", url);
    run();
  });
  document.querySelectorAll("#chips input").forEach(function (el) {
    el.addEventListener("change", function () { if (input.value) run(); });
  });

  var r = new URLSearchParams(location.search).get("r");
  if (r) { input.value = r; run(); }
})();
