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
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import unquote
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

def fetch(url, delay=1.5):
    time.sleep(delay + random.uniform(0, 0.8))
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


# ─── Amazon 個別ページから価格・レビュー取得 ──────────────────

def fetch_amazon_detail(asin):
    """ASIN から価格・レビュー数を取得（上位商品のみ）"""
    url = f"https://www.amazon.co.jp/dp/{asin}/"
    html = fetch(url, delay=1.5)
    if not html:
        return 0, 0

    # 価格
    price_num = 0
    price_patterns = [
        r'priceblock_ourprice[^>]*>(¥[\d,]+)<',
        r'"priceAmount":([\d.]+)',
        r'<span[^>]*class="[^"]*a-price-whole[^"]*"[^>]*>([\d,]+)<',
        r'\"price\":\s*\"([\d,]+)\"',
        r'([\d,]+)円\s*\(税込\)',
        r'([\d,]+)円',
    ]
    for pat in price_patterns:
        pm = re.search(pat, html)
        if pm:
            raw = pm.group(1).replace("¥", "").replace(",", "")
            try:
                num = int(float(raw))
                if 10 <= num <= 10000000:
                    price_num = num
                    break
            except ValueError:
                continue

    # レビュー数
    reviews = 0
    rev_patterns = [
        r'([\d,]+)個の評価',
        r'([\d,]+)件のカスタマーレビュー',
        r'"ratingCount":([\d]+)',
        r'ratingsCount[^>]*>([\d,]+)<',
    ]
    for pat in rev_patterns:
        rm = re.search(pat, html)
        if rm:
            reviews = int(rm.group(1).replace(",", ""))
            break

    return price_num, reviews


# ─── Amazon ベストセラー / ムーバーズ スクレイパー ────────────

def scrape_amazon_page(category_name, url, list_type="BS"):
    """Amazon ランキングページをスクレイプ"""
    html = fetch(url, delay=2)
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
                    "asin":      asin,
                    "rank":      rank,
                    "title":     title,
                    "category":  category_name,
                    "source":    f"Amazon_{list_type}",
                    "price":     "不明",
                    "price_num": 0,
                    "rating":    0.0,
                    "reviews":   0,
                    "url":       f"https://www.amazon.co.jp/dp/{asin}/",
                }

    # Step2: 評価をASIN近傍HTMLから紐付け
    asin_rating = extract_amazon_ratings(html)
    for asin, rating in asin_rating.items():
        if asin in products:
            products[asin]["rating"] = rating

    # Step3: 価格はページ内にない→上位10件のみ個別ページで取得
    sorted_prods = sorted(products.values(), key=lambda x: x["rank"])
    print(f"    ランキング {len(sorted_prods)}件取得, 上位10件の価格を個別取得中...")

    for p in sorted_prods[:10]:
        price_num, reviews = fetch_amazon_detail(p["asin"])
        if price_num:
            p["price_num"] = price_num
            p["price"]     = f"¥{price_num:,}"
        if reviews:
            p["reviews"] = reviews

    return sorted_prods


def scrape_amazon(category_name, use_bs=True, use_ms=False):
    all_prods = []

    if use_bs and category_name in AMAZON_BS_URLS:
        print(f"  Amazon ベストセラー [{category_name}]...")
        prods = scrape_amazon_page(category_name, AMAZON_BS_URLS[category_name], "BS")
        all_prods.extend(prods)

    if use_ms and category_name in AMAZON_MS_URLS:
        print(f"  Amazon 急上昇 [{category_name}]...")
        prods = scrape_amazon_page(category_name, AMAZON_MS_URLS[category_name], "MS")
        all_prods.extend(prods)

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
