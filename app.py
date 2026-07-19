#!/usr/bin/env python3
"""物販リサーチツール Web版"""

import re
import time
import random
import threading
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import unquote
from flask import Flask, request, jsonify

from picker import (
    AMAZON_BS_URLS, AMAZON_MS_URLS, score,
    decode_url_title, extract_amazon_ratings,
    fetch_amazon_detail, fetch_yahoo_price,
    discover_subcategories, batch_fetch_prices,
)

app = Flask(__name__)
tasks = {}  # task_id → dict

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── スクレイピング ──────────────────────────────────────────────

def fetch_html(url, delay=1.5):
    time.sleep(delay + random.uniform(0, 0.4))
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

def scrape_ranking(cat, url, list_type, log, fetch_details=True):
    html = fetch_html(url, delay=1.0)
    if not html:
        log(f"  [{cat}] 取得失敗")
        return []

    products = {}
    for slug, asin, rank_str in re.findall(
        r'href=\"/([^\"]+)/dp/([A-Z0-9]{10})/ref=zg_[a-z]+[^\"]+sccl_(\d+)', html
    ):
        rank = int(rank_str)
        if asin not in products or products[asin]["rank"] > rank:
            title = decode_url_title(slug)
            if title:
                products[asin] = {
                    "asin": asin, "rank": rank, "title": title,
                    "category": cat, "source": f"Amazon_{list_type}",
                    "price": "不明", "price_num": 0,
                    "rating": 0.0, "reviews": 0,
                    "url": f"https://www.amazon.co.jp/dp/{asin}/",
                }

    for asin, rating in extract_amazon_ratings(html).items():
        if asin in products:
            products[asin]["rating"] = rating

    sorted_prods = sorted(products.values(), key=lambda x: x["rank"])
    log(f"  ランキング {len(sorted_prods)}件取得")

    # 詳細取得（メインページのみ・並列）
    if fetch_details:
        def _fetch(p):
            return p, fetch_amazon_detail(p["asin"], p.get("title", ""), cat)

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_fetch, p): p for p in sorted_prods[:5]}
            for future in as_completed(futures):
                p, detail = future.result()
                log(f"  詳細取得: {p['title'][:20]}... {detail['price']}")
                if detail["price_num"]:
                    p["price_num"] = detail["price_num"]
                    p["price"]     = detail["price"]
                if detail["reviews"]:
                    p["reviews"]   = detail["reviews"]
                p["maker"]            = detail.get("maker", "")
                p["monthly_sales"]    = detail.get("monthly_sales", 0)
                p["supply_price"]     = detail.get("supply_price", 0)
                p["supply_price_str"] = detail.get("supply_price_str", "不明")

    return sorted_prods

TARGET_PER_CAT = 200

def run_task(task_id, categories, source):
    t = tasks[task_id]
    lock = threading.Lock()

    def log(msg):
        with lock:
            t["logs"].append(msg)

    def scrape_cat(cat):
        """1カテゴリ分を取得（メイン=詳細あり、サブ=高速）"""
        use_bs = source in ("bs", "both")
        use_ms = source in ("ms", "both")
        seen = set()
        prods = []
        pages_needed = max(1, math.ceil(TARGET_PER_CAT / 30))

        def fetch_pages(base_url, list_type):
            cat_slug = base_url.rstrip("/").split("/")[-1]
            subs = discover_subcategories(base_url, cat_slug)
            log(f"[{list_type}] {cat}: メイン+サブ{len(subs)}件")

            # メインページ: 詳細取得あり
            main = scrape_ranking(cat, base_url, list_type, log, fetch_details=True)
            for p in main:
                with lock:
                    if p["asin"] not in seen:
                        seen.add(p["asin"])
                        prods.append(p)

            # サブカテゴリ: 詳細なし（並列・高速）
            sub_urls = subs[: pages_needed - 1]
            if sub_urls:
                with ThreadPoolExecutor(max_workers=5) as ex:
                    futures = [ex.submit(scrape_ranking, cat, u, f"{list_type}_sub", log, False) for u in sub_urls]
                    for f in as_completed(futures):
                        for p in f.result():
                            with lock:
                                if p["asin"] not in seen:
                                    seen.add(p["asin"])
                                    prods.append(p)

            log(f"  ✓ [{list_type}] {cat}: {len(prods)}件")

        if use_bs and cat in AMAZON_BS_URLS:
            fetch_pages(AMAZON_BS_URLS[cat], "BS")
        if use_ms and cat in AMAZON_MS_URLS:
            fetch_pages(AMAZON_MS_URLS[cat], "MS")

        return prods

    try:
        use_bs = source in ("bs", "both")
        use_ms = source in ("ms", "both")
        all_prods = []
        total = len(categories)

        # カテゴリを2並列で処理（Amazonへの負荷を考慮）
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {ex.submit(scrape_cat, cat): cat for cat in categories}
            done = 0
            for f in as_completed(futures):
                cat = futures[f]
                prods = f.result()
                all_prods.extend(prods)
                done += 1
                t["pct"] = int(done / total * 90)
                t["label"] = f"{cat} 完了 ({done}/{total})"
                log(f"✓ {cat}: {len(prods)}件")

        # 価格未取得商品を一括並列取得
        unpriced = [p for p in all_prods if not p.get("price_num")]
        if unpriced:
            log(f"価格一括取得中... ({len(unpriced)}件)")
            t["label"] = f"価格取得中 ({len(unpriced)}件)..."
            batch_fetch_prices(unpriced, workers=10)
            priced = sum(1 for p in all_prods if p.get("price_num"))
            log(f"  価格取得完了: {priced}/{len(all_prods)}件")

        log("スコアリング中...")
        for p in all_prods:
            p["score"] = score(p)
        all_prods.sort(key=lambda x: x["score"], reverse=True)

        seen, unique = set(), []
        for p in all_prods:
            key = p.get("asin") or p.get("title", "")
            if key and key not in seen:
                seen.add(key)
                unique.append(p)

        log(f"✅ 完了: {len(unique)}件")
        t["results"] = unique
        t["pct"]     = 100
        t["label"]   = f"完了 ({len(unique)}件)"
        t["status"]  = "done"

    except Exception as e:
        log(f"❌ エラー: {e}")
        t["status"] = "error"
        t["error"]  = str(e)

# ── ルート ────────────────────────────────────────────────────

@app.route("/")
def index():
    cats = list(AMAZON_BS_URLS.keys())
    cat_checkboxes = "\n".join(
        f'<label class="cat-item"><input type="checkbox" name="categories" value="{c}" checked> {c}</label>'
        for c in cats
    )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>物販リサーチツール</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--text:#e8eaf6;--muted:#7986cb;--orange:#ff6b35;--green:#4caf50}}
body{{background:var(--bg);color:var(--text);font-family:'Segoe UI','Hiragino Sans',sans-serif;min-height:100vh}}
header{{background:#12151e;border-bottom:1px solid var(--border);padding:18px 32px;display:flex;align-items:center;gap:14px}}
header h1{{font-size:18px;font-weight:700}}
header p{{font-size:11px;color:var(--muted)}}
.wrap{{max-width:1200px;margin:0 auto;padding:28px 20px;display:grid;grid-template-columns:280px 1fr;gap:20px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}}
.card h2{{font-size:13px;font-weight:700;color:var(--orange);margin-bottom:14px;letter-spacing:.5px}}
.cat-grid{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:10px}}
.cat-item{{display:flex;align-items:center;gap:7px;padding:7px 10px;border:1px solid var(--border);border-radius:7px;cursor:pointer;font-size:12px;transition:.15s}}
.cat-item:hover{{border-color:var(--orange);background:rgba(255,107,53,.08)}}
input[type=checkbox],input[type=radio]{{accent-color:var(--orange)}}
.row-btns{{display:flex;gap:6px;margin-bottom:4px}}
.mini-btn{{flex:1;padding:5px;background:var(--border);border:none;border-radius:6px;color:var(--text);cursor:pointer;font-size:11px}}
.src-group{{display:flex;flex-direction:column;gap:7px}}
.src-item{{display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:12px;transition:.15s}}
.src-item:hover{{border-color:var(--orange)}}
.src-title{{font-weight:600}}
.src-desc{{font-size:10px;color:var(--muted)}}
.btn-run{{width:100%;margin-top:16px;padding:14px;background:var(--orange);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;transition:.15s}}
.btn-run:hover{{background:#e55a25}}
.btn-run:disabled{{background:#555;cursor:not-allowed}}
#prog-wrap{{margin-top:14px;display:none}}
.prog-info{{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:5px}}
.prog-bg{{background:var(--border);border-radius:99px;height:7px;overflow:hidden}}
.prog-fill{{height:100%;width:0%;background:linear-gradient(90deg,var(--orange),#ff9a5c);transition:width .4s;border-radius:99px}}
#log-box{{margin-top:10px;background:#0a0c12;border:1px solid var(--border);border-radius:7px;padding:10px;font-family:monospace;font-size:11px;color:#81c784;height:130px;overflow-y:auto;white-space:pre-wrap}}
#right{{min-width:0}}
.empty{{text-align:center;padding:80px 20px;color:var(--muted)}}
.empty .ico{{font-size:44px;margin-bottom:12px}}
.sum-row{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:18px}}
.sum-card{{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:14px;text-align:center}}
.sum-num{{font-size:24px;font-weight:800;color:var(--orange)}}
.sum-lbl{{font-size:10px;color:var(--muted);margin-top:3px}}
.tbl-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}}
.tbl-head h2{{font-size:15px;font-weight:700}}
.badge{{background:var(--orange);color:#fff;font-size:11px;padding:2px 9px;border-radius:99px;margin-left:8px}}
.btn-csv{{padding:7px 16px;background:#2e7d32;color:#fff;border:none;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer}}
.btn-csv:hover{{background:#388e3c}}
.tbl-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#12151e;padding:9px 10px;text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}}
th:hover{{color:var(--orange)}}
td{{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:hover td{{background:rgba(255,107,53,.04)}}
.sc{{display:inline-block;padding:2px 7px;border-radius:5px;font-weight:700}}
.sc-hi{{background:rgba(76,175,80,.2);color:#81c784}}
.sc-md{{background:rgba(255,179,0,.2);color:#ffca28}}
.sc-lo{{background:rgba(239,83,80,.15);color:#ef9a9a}}
.stars{{color:#ffb300}}
a.tlink{{color:var(--text);text-decoration:none}}
a.tlink:hover{{color:var(--orange);text-decoration:underline}}
#results-area{{display:none}}
</style>
</head>
<body>
<header>
  <div style="font-size:26px">📦</div>
  <div>
    <h1>物販リサーチツール</h1>
    <p>Amazon JP ベストセラー 売れ筋ピックアップ</p>
  </div>
</header>
<div class="wrap">
  <!-- 左パネル -->
  <div>
    <div class="card">
      <h2>📂 カテゴリ</h2>
      <div class="cat-grid">
        {cat_checkboxes}
      </div>
      <div class="row-btns">
        <button class="mini-btn" onclick="setAll(true)">すべて選択</button>
        <button class="mini-btn" onclick="setAll(false)">クリア</button>
      </div>
    </div>
    <div class="card">
      <h2>🔍 データソース</h2>
      <div class="src-group">
        <label class="src-item">
          <input type="radio" name="source" value="bs" checked>
          <div><div class="src-title">ベストセラー</div><div class="src-desc">安定した売れ筋</div></div>
        </label>
        <label class="src-item">
          <input type="radio" name="source" value="ms">
          <div><div class="src-title">急上昇（ムーバーズ）</div><div class="src-desc">今伸びている商品</div></div>
        </label>
        <label class="src-item">
          <input type="radio" name="source" value="both">
          <div><div class="src-title">両方 <span style="color:var(--orange);font-size:10px">推奨</span></div><div class="src-desc">より多くのデータ</div></div>
        </label>
      </div>
    </div>
    <button class="btn-run" id="runBtn" onclick="startSearch()">🚀 リサーチ開始</button>
    <div id="prog-wrap">
      <div class="prog-info">
        <span id="progLabel">準備中...</span>
        <span id="progPct">0%</span>
      </div>
      <div class="prog-bg"><div class="prog-fill" id="progBar"></div></div>
      <div id="log-box"></div>
    </div>
  </div>

  <!-- 右: 結果 -->
  <div id="right">
    <div class="empty" id="emptyState">
      <div class="ico">🔎</div>
      <p>カテゴリを選んで「リサーチ開始」を押してください</p>
    </div>
    <div id="results-area">
      <div class="sum-row" id="sumRow"></div>
      <div class="tbl-head">
        <div><h2 style="display:inline">検索結果</h2><span class="badge" id="cnt">0件</span></div>
        <button class="btn-csv" onclick="dlCSV()">⬇ CSV ダウンロード</button>
      </div>
      <div class="card" style="padding:0">
        <div class="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th onclick="srt('rank')">#</th>
                <th onclick="srt('score')">スコア↕</th>
                <th>カテゴリ</th>
                <th onclick="srt('price_num')">販売価格↕</th>
                <th onclick="srt('supply_price')">仕入れ参考値↕</th>
                <th onclick="srt('monthly_sales')">月間販売数↕</th>
                <th onclick="srt('rating')">評価↕</th>
                <th onclick="srt('reviews')">レビュー↕</th>
                <th>メーカー</th>
                <th>商品名</th>
              </tr>
            </thead>
            <tbody id="tbody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
var allData = [];
var taskId  = null;
var timer   = null;
var sortAsc = {{}};

function setAll(v) {{
  document.querySelectorAll('input[name=categories]').forEach(function(el){{ el.checked = v; }});
}}

function startSearch() {{
  var cats = [];
  document.querySelectorAll('input[name=categories]:checked').forEach(function(el){{ cats.push(el.value); }});
  if (cats.length === 0) {{ alert('カテゴリを1つ以上選んでください'); return; }}
  var src = document.querySelector('input[name=source]:checked').value;

  document.getElementById('runBtn').disabled = true;
  document.getElementById('runBtn').textContent = '⏳ 取得中...';
  document.getElementById('prog-wrap').style.display = 'block';
  document.getElementById('results-area').style.display = 'none';
  document.getElementById('emptyState').style.display = 'none';
  document.getElementById('log-box').textContent = '';
  setProg(5, '開始中...');

  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/search');
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = function() {{
    if (xhr.status === 200) {{
      var res = JSON.parse(xhr.responseText);
      taskId = res.task_id;
      timer  = setInterval(poll, 1500);
    }} else {{
      alert('エラー: サーバーに接続できません');
      resetBtn();
    }}
  }};
  xhr.onerror = function() {{ alert('ネットワークエラー'); resetBtn(); }};
  xhr.send(JSON.stringify({{categories: cats, source: src}}));
}}

function poll() {{
  var xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/status/' + taskId);
  xhr.onload = function() {{
    if (xhr.status !== 200) return;
    var d = JSON.parse(xhr.responseText);
    setProg(d.pct || 0, d.label || '');
    var box = document.getElementById('log-box');
    box.textContent = (d.logs || []).join('\\n');
    box.scrollTop = box.scrollHeight;

    if (d.status === 'done') {{
      clearInterval(timer);
      allData = d.results || [];
      render(allData);
      resetBtn();
    }} else if (d.status === 'error') {{
      clearInterval(timer);
      alert('エラー: ' + (d.error || '不明'));
      resetBtn();
    }}
  }};
  xhr.send();
}}

function setProg(pct, label) {{
  document.getElementById('progBar').style.width  = pct + '%';
  document.getElementById('progPct').textContent  = pct + '%';
  document.getElementById('progLabel').textContent = label;
}}

function resetBtn() {{
  document.getElementById('runBtn').disabled    = false;
  document.getElementById('runBtn').textContent = '🚀 リサーチ開始';
}}

function scClass(s) {{
  return s >= 60 ? 'sc-hi' : s >= 40 ? 'sc-md' : 'sc-lo';
}}

function stars(r) {{
  if (!r) return '-';
  var s = '';
  for (var i = 0; i < Math.floor(r); i++) s += '★';
  if (r - Math.floor(r) >= 0.5) s += '½';
  return '<span class="stars">' + s + '</span> ' + r.toFixed(1);
}}

function render(data) {{
  document.getElementById('emptyState').style.display   = 'none';
  document.getElementById('results-area').style.display = 'block';
  document.getElementById('cnt').textContent = data.length + '件';

  var priced = data.filter(function(p){{ return p.price_num > 0; }});
  var avgSc  = data.length ? (data.reduce(function(s,p){{return s+p.score;}},0)/data.length).toFixed(1) : 0;
  var avgPr  = priced.length ? Math.round(priced.reduce(function(s,p){{return s+p.price_num;}},0)/priced.length) : 0;
  var cats   = [...new Set(data.map(function(p){{return p.category;}}))];

  document.getElementById('sumRow').innerHTML =
    '<div class="sum-card"><div class="sum-num">'+data.length+'</div><div class="sum-lbl">総商品数</div></div>' +
    '<div class="sum-card"><div class="sum-num">'+cats.length+'</div><div class="sum-lbl">カテゴリ数</div></div>' +
    '<div class="sum-card"><div class="sum-num">'+(data.length?data[0].score.toFixed(1):0)+'</div><div class="sum-lbl">最高スコア</div></div>' +
    '<div class="sum-card"><div class="sum-num">'+avgSc+'</div><div class="sum-lbl">平均スコア</div></div>' +
    '<div class="sum-card"><div class="sum-num">¥'+avgPr.toLocaleString()+'</div><div class="sum-lbl">平均価格</div></div>';

  var html = '';
  for (var i = 0; i < data.length; i++) {{
    var p = data[i];
    var t = p.title ? (p.title.length > 46 ? p.title.substring(0,46)+'…' : p.title) : '-';
    var profit = (p.price_num && p.supply_price)
      ? Math.round(p.price_num * 0.85 - p.supply_price)  // Amazon手数料15%引き後の利益
      : null;
    var profitCell = profit !== null
      ? '<span style="color:'+(profit>0?'#81c784':'#ef9a9a')+';font-weight:600">'+(profit>0?'+':'')+profit.toLocaleString()+'円</span>'
      : '-';
    html += '<tr>' +
      '<td style="color:var(--muted)">'+(i+1)+'</td>' +
      '<td><span class="sc '+scClass(p.score)+'">'+p.score+'</span></td>' +
      '<td style="font-size:11px">'+p.category+'</td>' +
      '<td style="font-weight:600">'+(p.price||'-')+'</td>' +
      '<td style="color:#7986cb">'+(p.supply_price_str||'-')+'</td>' +
      '<td style="color:#ffca28">'+(p.monthly_sales?'約'+p.monthly_sales.toLocaleString()+'個':'-')+'</td>' +
      '<td>'+stars(p.rating)+'</td>' +
      '<td style="font-size:11px">'+(p.reviews?p.reviews.toLocaleString():'-')+'</td>' +
      '<td style="font-size:11px;color:var(--muted)">'+(p.maker||'-')+'</td>' +
      '<td><a class="tlink" href="'+(p.url||'#')+'" target="_blank">'+t+'</a></td>' +
    '</tr>';
  }}
  document.getElementById('tbody').innerHTML = html;
}}

function srt(key) {{
  sortAsc[key] = !sortAsc[key];
  allData.sort(function(a,b) {{
    var av = a[key]||0, bv = b[key]||0;
    if (typeof av === 'string') return sortAsc[key] ? av.localeCompare(bv) : bv.localeCompare(av);
    return sortAsc[key] ? av-bv : bv-av;
  }});
  render(allData);
}}

function dlCSV() {{
  var h = ['スコア','ランク','カテゴリ','ソース','商品名','販売価格','仕入れ参考値','月間販売数(推定)','評価','レビュー数','メーカー','ASIN','URL'];
  var rows = allData.map(function(p) {{
    return [p.score,p.rank,p.category,p.source,p.title,p.price||'',p.supply_price_str||'',p.monthly_sales||'',p.rating||'',p.reviews||'',p.maker||'',p.asin||'',p.url||''];
  }});
  var csv = [h].concat(rows).map(function(r) {{
    return r.map(function(v){{ return '"'+String(v).replace(/"/g,'""')+'"'; }}).join(',');
  }}).join('\\n');
  var blob = new Blob(['\\uFEFF'+csv], {{type:'text/csv;charset=utf-8;'}});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'result_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
}}
</script>
</body>
</html>"""

@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json()
    cats = data.get("categories", [])
    src  = data.get("source", "bs")
    tid  = datetime.now().strftime("%Y%m%d%H%M%S%f")
    tasks[tid] = {"status": "running", "pct": 0, "label": "開始中...", "logs": [], "results": [], "error": ""}
    t = threading.Thread(target=run_task, args=(tid, cats, src), daemon=True)
    t.start()
    return jsonify({"task_id": tid})

@app.route("/api/status/<tid>")
def api_status(tid):
    return jsonify(tasks.get(tid, {"status": "error", "error": "not found"}))

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
