#!/usr/bin/env python3
"""
物販リサーチツール - 売れ筋商品ピックアップ
Amazon JP ベストセラー / ムーバーズ＆シェイカーズ から売れ筋をスコアリングしてCSV出力
"""

import sys
import time
import random
import csv
import math
import re
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import unquote, quote_plus
from collections import defaultdict

# ─── 設定 ────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

AMAZON_BS_URLS = {
    "おもちゃ":         "https://www.amazon.co.jp/gp/bestsellers/toys/",
    "家電":             "https://www.amazon.co.jp/gp/bestsellers/electronics/",
    "ビューティー":     "https://www.amazon.co.jp/gp/bestsellers/beauty/",
    "スポーツ":         "https://www.amazon.co.jp/gp/bestsellers/sports/",
    "ホーム＆キッチン": "https://www.amazon.co.jp/gp/bestsellers/kitchen/",
    "文房具":           "https://www.amazon.co.jp/gp/bestsellers/office-products/",
    "ペット用品":       "https://www.amazon.co.jp/gp/bestsellers/pet-supplies/",
    "食品":             "https://www.amazon.co.jp/gp/bestsellers/food-beverage/",
    "ヘルス":           "https://www.amazon.co.jp/gp/bestsellers/hpc/",
}

AMAZON_MS_URLS = {
    "おもちゃ":         "https://www.amazon.co.jp/gp/movers-and-shakers/toys/",
    "家電":             "https://www.amazon.co.jp/gp/movers-and-shakers/electronics/",
    "ビューティー":     "https://www.amazon.co.jp/gp/movers-and-shakers/beauty/",
    "スポーツ":         "https://www.amazon.co.jp/gp/movers-and-shakers/sports/",
    "ホーム＆キッチン": "https://www.amazon.co.jp/gp/movers-and-shakers/kitchen/",
    "文房具":           "https://www.amazon.co.jp/gp/movers-and-shakers/office-products/",
    "ペット用品":       "https://www.amazon.co.jp/gp/movers-and-shakers/pet-supplies/",
    "食品":             "https://www.amazon.co.jp/gp/movers-and-shakers/food-beverage/",
    "ヘルス":           "https://www.amazon.co.jp/gp/movers-and-shakers/hpc/",
}

# ─── ユーティリティ ───────────────────────────────────────────

def fetch(url, delay=0.5):
    time.sleep(delay + random.uniform(0, 0.3))
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=20) as res:
            return res.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError) as e:
        print(f"    [警告] 取得失敗: {e}")
        return ""


def decode_url_title(slug):
    try:
        decoded = unquote(slug)
        return decoded.split("/")[0].replace("-", " ").strip()
    except Exception:
        return ""


def extract_amazon_ratings(html):
    """HTMLから ASIN→評価 の辞書を作る"""
    asin_rating = {}
    for m in re.finditer(r'5つ星のうち([\d.]+)', html):
        start = max(0, m.start() - 400)
        snippet = html[start: m.end()]
        asin_m = re.search(r'/dp/([A-Z0-9]{10})', snippet)
        if asin_m:
            asin = asin_m.group(1)
            if asin not in asin_rating:
                asin_rating[asin] = float(m.group(1))
    return asin_rating


# ─── BSRから月間販売数を推定 ─────────────────────────────────

def estimate_monthly_sales(bsr, category="general"):
    """BSRランクから月間販売数を推定（近似値）"""
    multipliers = {
        "おもちゃ": 3.0, "家電": 2.0, "ビューティー": 4.0,
        "スポーツ": 2.5, "ホーム＆キッチン": 3.5, "文房具": 2.0,
        "ペット用品": 2.5, "食品": 5.0, "ヘルス": 3.0,
    }
    m = multipliers.get(category, 3.0)
    if   bsr <= 10:    return int(m * 5000)
    elif bsr <= 50:    return int(m * 2000)
    elif bsr <= 100:   return int(m * 1000)
    elif bsr <= 500:   return int(m * 400)
    elif bsr <= 1000:  return int(m * 200)
    elif bsr <= 3000:  return int(m * 80)
    elif bsr <= 5000:  return int(m * 40)
    else:              return int(m * 20)


# ─── Yahoo!ショッピングから仕入れ参考価格を取得 ──────────────

def fetch_yahoo_price(title):
    """Yahoo!ショッピングで最安値を検索（仕入れ参考値）"""
    query = title[:25]  # 短くして検索精度を上げる
    url = f"https://shopping.yahoo.co.jp/search?p={quote_plus(query)}&sort=price&order=a"
    html = fetch(url, delay=1.2)
    if not html:
        return 0

    # JSONデータから価格抽出
    prices = re.findall(r'"price":([\d]+)', html)
    candidates = [int(p) for p in prices if 100 <= int(p) <= 500000]
    if candidates:
        return min(candidates)

    # フォールバック: テキストから円価格抽出
    raw = re.findall(r'([\d,]+)円', html)
    nums = sorted([int(p.replace(",", "")) for p in raw if 100 <= int(p.replace(",", "")) <= 500000])
    return nums[0] if nums else 0


# ─── Amazon 個別ページから詳細情報を取得 ────────────────────

def fetch_price_only(asin):
    """価格だけ高速取得（詳細なし）"""
    html = fetch(f"https://www.amazon.co.jp/dp/{asin}/", delay=0.4)
    if not html:
        return 0
    for pat in [
        r'class="[^"]*a-price-whole[^"]*"[^>]*>([\d,]+)<',
        r'"priceAmount":([\d.]+)',
    ]:
        m = re.search(pat, html)
        if m:
            try:
                n = int(float(m.group(1).replace(",", "")))
                if 10 <= n <= 10000000:
                    return n
            except ValueError:
                pass
    return 0


def batch_fetch_prices(products, workers=8):
    """全商品の価格を並列一括取得"""
    need = [p for p in products if not p.get("price_num")]
    if not need:
        return

    def _fetch(p):
        price = fetch_price_only(p["asin"])
        if price:
            p["price_num"] = price
            p["price"]     = f"¥{price:,}"

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_fetch, need))


def fetch_amazon_detail(asin, title="", category=""):
    """ASIN から価格・レビュー・メーカー・月間販売数を取得"""
    url = f"https://www.amazon.co.jp/dp/{asin}/"
    html = fetch(url, delay=0.5)
    result = {
        "price_num": 0, "price": "不明",
        "reviews": 0, "maker": "", "monthly_sales": 0,
        "supply_price": 0, "supply_price_str": "不明",
    }
    if not html:
        return result

    # 価格（複数パターン試行）
    for pat in [
        r'<span[^>]*class="[^"]*a-price-whole[^"]*"[^>]*>([\d,]+)<',
        r'"priceAmount":([\d.]+)',
        r'"price":\{"amount":([\d.]+)',
        r'data-a-color="price"[^>]*>.*?<span[^>]*>([\d,]+)<',
    ]:
        pm = re.search(pat, html, re.DOTALL)
        if pm:
            try:
                num = int(float(pm.group(1).replace(",", "")))
                if 10 <= num <= 10000000:
                    result["price_num"] = num
                    result["price"]     = f"¥{num:,}"
                    break
            except ValueError:
                pass

    # レビュー数
    for pat in [r'([\d,]+)個の評価', r'([\d,]+)件のカスタマーレビュー']:
        rm = re.search(pat, html)
        if rm:
            result["reviews"] = int(rm.group(1).replace(",", ""))
            break

    # メーカー名（ブランド）
    brand_m = re.search(r'<a[^>]*id="bylineInfo"[^>]*>([^<]+)<', html)
    if brand_m:
        brand = brand_m.group(1).strip()
        brand = re.sub(r'のストアを表示|のストア|ブランド:|Visit the .+ Store|のブランドページ', '', brand).strip()
        brand = re.sub(r'\s+', ' ', brand).strip()
        if brand:
            result["maker"] = brand

    # メーカー（商品詳細テーブルから）
    if not result["maker"]:
        for pat in [
            r'<th[^>]*>[^<]*ブランド[^<]*</th>\s*<td[^>]*><span[^>]*>([^<]{2,40})</span>',
            r'ブランド</th>.*?<td[^>]*>([^<]{2,40})</td>',
        ]:
            mk = re.search(pat, html, re.DOTALL)
            if mk:
                result["maker"] = mk.group(1).strip()
                break

    # 月間販売数（ページに表示されている場合）
    monthly_m = re.search(r'([\d,+]+)\s*個購入（過去|過去1ヶ月で([\d,+]+)', html)
    if monthly_m:
        raw = (monthly_m.group(1) or monthly_m.group(2) or "0").replace(",", "").replace("+", "")
        result["monthly_sales"] = int(raw)
    else:
        # BSR推定
        result["monthly_sales"] = estimate_monthly_sales(
            category_name_to_bsr_rank(title, html), category
        )

    # 仕入れ参考価格（Yahoo!ショッピング）
    if title:
        supply = fetch_yahoo_price(title)
        if supply:
            result["supply_price"]     = supply
            result["supply_price_str"] = f"¥{supply:,}"

    return result


def category_name_to_bsr_rank(title, html):
    """HTMLからBSRランクを取得（なければデフォルト）"""
    for pat in [
        r'売れ筋ランキング.*?(\d[\d,]+)\s*位',
        r'Best Sellers Rank.*?#([\d,]+)',
        r'(\d[\d,]+)\s*位\s*（',
    ]:
        bsr_m = re.search(pat, html, re.DOTALL)
        if bsr_m:
            return int(bsr_m.group(1).replace(",", ""))
    return 500  # デフォルト


# ─── Amazon ベストセラー / ムーバーズ スクレイパー ────────────

def scrape_amazon_page(category_name, url, list_type="BS", fetch_details=True):
    """Amazon ランキングページをスクレイプ
    fetch_details=False のときはランク・タイトル・評価のみ取得（高速）
    """
    html = fetch(url, delay=1.0)
    if not html:
        return []

    # Step1: ランク付きASINをURLパターンから抽出
    products = {}
    for slug, asin, rank_str in re.findall(
        r'href=\"/([^\"]+)/dp/([A-Z0-9]{10})/ref=zg_[a-z]+[^\"]+sccl_(\d+)',
        html
    ):
        rank = int(rank_str)
        if asin not in products or products[asin]["rank"] > rank:
            title = decode_url_title(slug)
            if title:
                products[asin] = {
                    "asin":           asin,
                    "rank":           rank,
                    "title":          title,
                    "category":       category_name,
                    "source":         f"Amazon_{list_type}",
                    "price":          "不明",
                    "price_num":      0,
                    "rating":         0.0,
                    "reviews":        0,
                    "maker":          "",
                    "monthly_sales":  0,
                    "supply_price":   0,
                    "supply_price_str": "不明",
                    "url":            f"https://www.amazon.co.jp/dp/{asin}/",
                }

    # Step2: 評価をASIN近傍HTMLから紐付け
    asin_rating = extract_amazon_ratings(html)
    for asin, rating in asin_rating.items():
        if asin in products:
            products[asin]["rating"] = rating

    sorted_prods = sorted(products.values(), key=lambda x: x["rank"])

    # Step3: メインページのみ詳細を並列取得（サブカテゴリはスキップで高速化）
    if fetch_details:
        def _fetch(p):
            return p, fetch_amazon_detail(p["asin"], p.get("title", ""), category_name)

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_fetch, p): p for p in sorted_prods[:5]}
            for future in as_completed(futures):
                p, detail = future.result()
                if detail["price_num"]:
                    p["price_num"] = detail["price_num"]
                    p["price"]     = detail["price"]
                if detail["reviews"]:
                    p["reviews"]   = detail["reviews"]
                if detail["maker"]:
                    p["maker"]     = detail["maker"]
                p["monthly_sales"]    = detail["monthly_sales"]
                p["supply_price"]     = detail["supply_price"]
                p["supply_price_str"] = detail["supply_price_str"]

    return sorted_prods


def discover_subcategories(base_url, cat_slug):
    """カテゴリページからサブカテゴリURLを自動取得"""
    html = fetch(base_url, delay=1.5)
    if not html:
        return []
    pattern = rf'/gp/bestsellers/{cat_slug}/(\d+)/[^\"]*'
    seen, subs = set(), []
    for m in re.finditer(rf'href=\"({pattern})\"', html):
        path = m.group(1)
        cat_id = m.group(2)
        if cat_id not in seen:
            seen.add(cat_id)
            subs.append(f"https://www.amazon.co.jp{path}")
    return subs


def scrape_amazon(category_name, use_bs=True, use_ms=False, target=200):
    """サブカテゴリを並列取得して合計 target 件返す"""
    seen_asins = set()
    lock = __import__("threading").Lock()

    def fetch_page(url, list_type):
        return scrape_amazon_page(category_name, url, list_type)

    def collect(base_url, list_type):
        cat_slug = base_url.rstrip("/").split("/")[-1]
        subs = discover_subcategories(base_url, cat_slug)
        pages_needed = max(1, math.ceil(target / 30))

        results = []

        # メインページ: 詳細あり
        main_prods = scrape_amazon_page(category_name, base_url, list_type, fetch_details=True)
        for p in main_prods:
            with lock:
                if p["asin"] not in seen_asins:
                    seen_asins.add(p["asin"])
                    results.append(p)

        # サブカテゴリ: 詳細なし（高速）
        sub_urls = subs[: pages_needed - 1]
        if sub_urls:
            with ThreadPoolExecutor(max_workers=5) as ex:
                futures = [ex.submit(scrape_amazon_page, category_name, u, f"{list_type}_sub", False) for u in sub_urls]
                for f in as_completed(futures):
                    for p in f.result():
                        with lock:
                            if p["asin"] not in seen_asins:
                                seen_asins.add(p["asin"])
                                results.append(p)

        return results

    all_prods = []
    if use_bs and category_name in AMAZON_BS_URLS:
        prods = collect(AMAZON_BS_URLS[category_name], "BS")
        all_prods.extend(prods)
        print(f"  [BS] {category_name}: {len(prods)}件")

    if use_ms and category_name in AMAZON_MS_URLS:
        prods = collect(AMAZON_MS_URLS[category_name], "MS")
        all_prods.extend(prods)
        print(f"  [MS] {category_name}: {len(prods)}件")

    # 価格未取得商品を一括並列取得
    print(f"  価格一括取得中...")
    batch_fetch_prices(all_prods, workers=8)
    priced = sum(1 for p in all_prods if p.get("price_num"))
    print(f"  → {category_name} 合計 {len(all_prods)}件（価格取得: {priced}件）")
    return all_prods


# ─── スコアリング ─────────────────────────────────────────────

def score(product):
    """
    売れ筋スコア（0〜100）
      ランク上位       : 40点
      レビュー数       : 30点（需要の証拠）
      評価             : 20点
      価格帯（仕入れ） : 10点
    """
    rank    = product.get("rank", 30)
    reviews = product.get("reviews", 0)
    rating  = product.get("rating", 0.0)
    price   = product.get("price_num", 0)

    rank_score   = max(0.0, 40.0 - (rank - 1) * (40.0 / 29.0))
    review_score = (min(30.0, math.log10(reviews + 1) / math.log10(10001) * 30)
                    if reviews > 0 else 0)
    rating_score = (rating / 5.0) * 20 if rating > 0 else 10

    if   500  <= price <= 3000:  price_score = 10
    elif 3000 <  price <= 8000:  price_score = 7
    elif price > 8000:           price_score = 4
    else:                        price_score = 5   # 価格不明

    return round(rank_score + review_score + rating_score + price_score, 1)


# ─── メイン ──────────────────────────────────────────────────

def print_header(text, width=72):
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def main():
    print_header("物販リサーチツール  売れ筋ピックアップ")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    cats = list(AMAZON_BS_URLS.keys())

    print("カテゴリを選んでください（複数: カンマ区切り）:")
    for i, c in enumerate(cats, 1):
        print(f"  {i}. {c}")
    print("  0. すべて")
    choice = input("\n番号を入力 [デフォルト: 0]: ").strip() or "0"

    if choice == "0":
        selected = cats
    else:
        try:
            idxs   = [int(x.strip()) - 1 for x in choice.split(",")]
            selected = [cats[i] for i in idxs if 0 <= i < len(cats)]
        except (ValueError, IndexError):
            selected = cats

    print(f"\n選択: {', '.join(selected)}")

    print("\nデータソース:")
    print("  1. ベストセラーのみ")
    print("  2. 急上昇（ムーバーズ＆シェイカーズ）のみ")
    print("  3. 両方（推奨）")
    src_choice = input("選択 [デフォルト: 1]: ").strip() or "1"

    use_bs = src_choice in ("1", "3")
    use_ms = src_choice in ("2", "3")

    all_products = []
    print("\n[データ収集中...]\n")

    for cat in selected:
        prods = scrape_amazon(cat, use_bs=use_bs, use_ms=use_ms)
        all_products.extend(prods)
        print(f"  ✓ {cat}: {len(prods)}件\n")

    if not all_products:
        print("[エラー] 商品が取得できませんでした。")
        sys.exit(1)

    # スコアリング & ソート
    for p in all_products:
        p["score"] = score(p)
    all_products.sort(key=lambda x: x["score"], reverse=True)

    # 重複除去（同ASIN）
    seen_asin  = set()
    unique_products = []
    for p in all_products:
        key = p.get("asin", p.get("title", ""))
        if key not in seen_asin:
            seen_asin.add(key)
            unique_products.append(p)
    all_products = unique_products

    # ─── TOP20 表示 ───────────────────────────────────────────
    print_header("TOP 20 売れ筋商品")
    print(f"{'#':<4}{'スコア':<7}{'カテゴリ':<14}{'ソース':<10}{'価格':<10}{'評価':<6}{'レビュー':<9}タイトル")
    print("-" * 90)

    for i, p in enumerate(all_products[:20], 1):
        title   = (p.get("title") or "")[:36]
        rating  = f"{p['rating']:.1f}" if p.get("rating") else "-"
        reviews = f"{p.get('reviews', 0):,}" if p.get("reviews") else "-"
        src     = p.get("source", "")[:9]
        print(
            f"{i:<4}{p['score']:<7.1f}{p['category']:<14}{src:<10}"
            f"{p.get('price', '不明'):<10}{rating:<6}{reviews:<9}{title}"
        )

    # ─── CSV出力 ──────────────────────────────────────────────
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    out_file = f"result_{ts}.csv"
    fields   = ["score", "rank", "category", "source", "title",
                "price", "price_num", "rating", "reviews", "asin", "url"]

    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_products)

    print(f"\n[完了] {out_file} に保存しました（{len(all_products)}件）")
    print("  → Excelで開いてスコア列で並び替えると仕入れ候補リストとして使えます\n")

    # ─── カテゴリ別サマリー ──────────────────────────────────
    print("[カテゴリ別 平均スコア (高い=売れやすい)]")
    cat_scores = defaultdict(list)
    for p in all_products:
        cat_scores[p["category"]].append(p["score"])
    for cat, scores in sorted(cat_scores.items(), key=lambda x: -(sum(x[1]) / len(x[1]))):
        avg = sum(scores) / len(scores)
        bar = "█" * int(avg / 5)
        print(f"  {cat:<16}: {avg:5.1f}点  {bar}")

    # ─── 価格帯分布 ──────────────────────────────────────────
    priced = [p for p in all_products if p.get("price_num", 0) > 0]
    if priced:
        print(f"\n[価格帯分布] ({len(priced)}件)")
        bands = {"~¥500": 0, "¥500-3K": 0, "¥3K-8K": 0, "¥8K+": 0}
        for p in priced:
            n = p["price_num"]
            if n < 500:       bands["~¥500"]   += 1
            elif n <= 3000:   bands["¥500-3K"] += 1
            elif n <= 8000:   bands["¥3K-8K"]  += 1
            else:             bands["¥8K+"]    += 1
        for band, cnt in bands.items():
            bar = "█" * cnt
            print(f"  {band:<10}: {cnt:3}件  {bar}")


if __name__ == "__main__":
    main()
