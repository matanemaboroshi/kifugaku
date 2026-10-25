// Planner: find gift combinations whose total is as close as possible to the
// remaining deduction amount without going over it.
(function () {
  "use strict";
  var result = document.getElementById("result");
  var form = document.querySelector(".shindan-form");
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
    return scored.slice(0, 8);
  }

  function render(R, combos) {
    if (!combos.length) {
      result.innerHTML = '<p class="note">' + yen(R) + '以内の返礼品が見つかりませんでした。金額を変えるか、カテゴリの絞り込みを外してください。</p>';
      return;
    }
    var html = '<h2>' + yen(R) + 'にぴったりの組み合わせ</h2><ol class="combos">';
    combos.forEach(function (c) {
      var tag = c.left === 0 ? '<span class="just">ぴったり</span>' : '<span class="left">残り ' + yen(c.left) + '</span>';
      html += '<li class="combo"><div class="combo-head"><b>合計 ' + yen(c.total) + '</b>' + tag + '<small>' + c.items.length + '品</small></div><ul>';
      c.items.forEach(function (it) {
        var img = it.i ? '<img src="' + esc(it.i) + '" alt="" loading="lazy" width="64" height="64">' : '<div class="noimg"></div>';
        html += '<li><a class="ct" href="' + esc(it.u) + '" rel="sponsored nofollow noopener" target="_blank">' + img + '</a>' +
          '<div><a class="cn" href="item/' + esc(it.h) + '.html">' + esc(it.n) + '</a>' +
          '<div class="cm">' + esc(it.s) + '・<b>' + yen(it.p) + '</b></div>' +
          '<a class="buy" href="' + esc(it.u) + '" rel="sponsored nofollow noopener" target="_blank">楽天で見る</a></div></li>';
      });
      html += '</ul></li>';
    });
    html += '</ol><p class="intro">寄付額は記録時点のものです。申し込む前に各返礼品ページでご確認ください。</p>';
    result.innerHTML = html;
  }

  function run() {
    var R = parseInt(input.value, 10);
    if (!(R >= 1000)) return;
    var go = function () { render(R, search(R, selectedCats())); };
    if (items) return go();
    result.innerHTML = '<p class="note">探しています…</p>';
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
