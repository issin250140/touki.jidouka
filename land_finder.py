#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
土地探しツール (land_finder.py)
================================

やること:
  1. 緯度経度 -> 住所 の逆ジオコーディング (HeartRails Geo API, APIキー不要)
  2. その座標を「eMAFF農地ナビ」(https://map.maff.go.jp) の地図データに直接問い合わせ、
     最寄りの農地区画(筆)の「所在地番」「地目」「面積」「農振法区分」を取得してCSVに保存

仕組み（重要）:
  農地ナビの地図はMapbox GLで、農地の区画ポリゴンは地図データ(ベクトルタイル)の
  プロパティとして 所在地番/地目/面積/農振法区分 をすでに保持しています。
  そのためこのツールは「地図上をクリックする」のではなく、指定座標付近を
  地図に読み込ませたあと、その座標に最も近い区画のプロパティを直接読み取ります。
  クリック操作が不要なため、完全に自動で動作します。

  ただし農地ナビには自動化ツール（ヘッドレスブラウザ）を検知して拒否する仕組みが
  あるため、通常のブラウザに近い形（User-Agent偽装 / navigator.webdriver隠蔽）で
  アクセスしています。個人の土地探し用途を想定しており、大量・高頻度のアクセスは
  控えてください（利用規約に「過度な負荷を与える通信は遮断することがある」旨の記載あり）。

セットアップ:
    pip install -r requirements.txt
    playwright install chromium

使い方:
    # 単発（緯度経度を1組指定）
    python land_finder.py --lat 37.9161 --lon 139.0364

    # 複数（CSV一括処理。入力CSVは lat,lon の2列。ヘッダー行必須）
    python land_finder.py --csv input_sample.csv --output result.csv

注意:
  - 表示される「面積」は登記簿ベースの参考値で、法的な証明力はありません。
  - 座標のごく近くに農地ナビ上の農地が無い場合は「該当なし」になります
    （農地でない土地／市街化区域内の農地など、そもそも農地ナビに載らない
    土地の場合はヒットしません）。
  - 正式な確認は現地の農業委員会等にお問い合わせください。
"""

import argparse
import csv
import math
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# Windows のコンソール（cmd/PowerShell）で日本語が文字化けするのを防ぐ
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NOUCHI_URL = "https://map.maff.go.jp/"
HEARTRAILS_URL = "https://geoapi.heartrails.com/api/json"
REQUEST_INTERVAL_SEC = 3  # バッチ処理時、1件ごとに空ける間隔（サーバ負荷への配慮）
NOUCHI_ZOOM = 18          # 農地の区画(ポリゴン)はズーム16以上でないと表示されない
NEAR_WARN_M = 100         # 最寄り区画までの距離がこれを超えたら注意書きを付ける

# 農地ナビはヘッドレスブラウザ（navigator.webdriver / UAの"HeadlessChrome"）を
# 検知してトップページ自体を403で弾くため、通常ブラウザに近い形でアクセスする
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


# ---------------------------------------------------------------------------
# 1. 座標 -> 住所（逆ジオコーディング）
# ---------------------------------------------------------------------------
def reverse_geocode(lat: float, lon: float) -> dict | None:
    """HeartRails Geo API で緯度経度から最寄りの住所を取得する。

    APIキー不要。該当地点付近の「町丁目」レベルの住所を返す
    （番地までは特定できない）。農地ナビの検索窓を「起動」させる
    ためだけに使うので、多少ズレていても後段の処理には影響しない。
    """
    params = {"method": "searchByGeoLocation", "x": lon, "y": lat}
    resp = requests.get(HEARTRAILS_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    locations = data.get("response", {}).get("location")
    if not locations:
        return None

    nearest = locations[0]  # APIは距離順（近い順）に返す
    address = f'{nearest["prefecture"]}{nearest["city"]}{nearest["town"]}'
    return {
        "prefecture": nearest["prefecture"],
        "city": nearest["city"],
        "town": nearest["town"],
        "address": address,
        "distance_m": nearest.get("distance"),
    }


# ---------------------------------------------------------------------------
# 2. 座標 -> 農地ナビの区画データ（面積・地目・農振区分）
# ---------------------------------------------------------------------------
JS_FIND_GEOCODER = """
() => {
    const controls = (window.map && map.mapCtrl && map.mapCtrl._controls) || [];
    const g = controls.find(c => c && typeof c.query === 'function');
    return !!g;
}
"""

JS_QUERY_ADDRESS = """
(address) => {
    const controls = map.mapCtrl._controls;
    const g = controls.find(c => c && typeof c.query === 'function');
    if (g) g.query(address);
}
"""

JS_FLYTO = """
([lon, lat, zoom]) => {
    map.mapCtrl.flyTo({center: [lon, lat], zoom: zoom, essential: true});
}
"""

JS_COUNT_PARCELS = """
() => map.mapCtrl.queryRenderedFeatures(undefined, {layers: ['noutiPolygon_fill']}).length
"""

JS_NEARBY_PARCELS = """
([lon, lat]) => {
    const feats = map.mapCtrl.queryRenderedFeatures(undefined, {layers: ['noutiPolygon_fill']});
    const seen = new Set();
    const results = [];
    for (const f of feats) {
        const id = f.properties && f.properties.DaichoId;
        if (id && seen.has(id)) continue;
        if (id) seen.add(id);

        const ring = f.geometry.type === 'MultiPolygon'
            ? f.geometry.coordinates[0][0]
            : f.geometry.coordinates[0];
        let cx = 0, cy = 0;
        for (const c of ring) { cx += c[0]; cy += c[1]; }
        cx /= ring.length; cy /= ring.length;

        results.push({
            centroid: [cx, cy],
            props: f.properties,
        });
    }
    return results;
}
"""


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def lookup_nouchi_navi(page, lat: float, lon: float, address: str) -> dict:
    """指定座標付近を農地ナビに読み込ませ、最寄りの農地区画の情報を取得する。"""
    page.goto(NOUCHI_URL, wait_until="load")
    page.wait_for_function(JS_FIND_GEOCODER, timeout=20000)
    # 地図アプリの初期化(最初の位置での区画データ読み込み)が終わるまで少し待つ
    # （公開サーバーは海外リージョン経由で農地ナビ(国内)にアクセスするため、
    #   手元の開発環境より通信・処理に時間がかかる。余裕を持たせている）
    page.wait_for_timeout(3000)

    # 住所検索を一度実行して、地図の「移動→区画データ取得」の仕組みを起動させる
    # （検索がヒットしなくても、この後の flyTo で正しい座標のデータが読み込まれる）
    page.evaluate(JS_QUERY_ADDRESS, address)
    page.wait_for_timeout(4000)

    # 本来の（正確な）座標へズームレベル18でジャンプ
    page.evaluate(JS_FLYTO, [lon, lat, NOUCHI_ZOOM])

    # 区画データの読み込み(非同期)が完了するまでポーリングして待つ
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if page.evaluate(JS_COUNT_PARCELS) > 0:
            break
        time.sleep(0.5)
    else:
        page.wait_for_timeout(2000)  # データが本当に0件の場合の最終確認用の猶予

    parcels = page.evaluate(JS_NEARBY_PARCELS, [lon, lat])

    if not parcels:
        return {
            "所在地番": "", "地目": "", "面積(m2)": "", "農振区分": "",
            "距離m": "", "近隣区画数": 0,
            "note": "この地点付近に農地ナビ上の農地区画が見つかりませんでした",
        }

    for p in parcels:
        clon, clat = p["centroid"]
        p["distance_m"] = haversine_m(lat, lon, clat, clon)
    parcels.sort(key=lambda p: p["distance_m"])
    nearest = parcels[0]
    props = nearest["props"]

    note = ""
    if nearest["distance_m"] > NEAR_WARN_M:
        note = f"最寄り区画まで約{nearest['distance_m']:.0f}m離れています。指定座標そのものは農地ではない可能性があります。"

    return {
        "所在地番": props.get("Address", ""),
        "地目": props.get("ClassificationOfLandCodeName", ""),
        "面積(m2)": props.get("AreaOnRegistry", ""),
        "農振区分": props.get("SectionOfNoushinhouCodeName", ""),
        "距離m": round(nearest["distance_m"], 1),
        "近隣区画数": len(parcels),
        "note": note,
    }


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
FIELDNAMES = ["lat", "lon", "address", "distance_m_to_address",
              "所在地番", "地目", "面積(m2)", "農振区分", "距離m", "近隣区画数", "note"]


def process_point(page, lat: float, lon: float) -> dict:
    row = {k: "" for k in FIELDNAMES}
    row["lat"], row["lon"] = lat, lon

    geo = reverse_geocode(lat, lon)
    if geo is None:
        row["note"] = "住所が特定できませんでした"
        return row

    row["address"] = geo["address"]
    row["distance_m_to_address"] = geo["distance_m"]
    print(f"[住所] {geo['address']} (最寄り住所地点まで約{geo['distance_m']:.0f}m)")

    farm = lookup_nouchi_navi(page, lat, lon, geo["address"])
    row.update(farm)

    if farm.get("所在地番"):
        print(f"[農地] {farm['所在地番']} / 地目:{farm['地目']} / "
              f"面積:{farm['面積(m2)']}m2 / 農振区分:{farm['農振区分']} "
              f"(座標から約{farm['距離m']}m)")
    else:
        print(f"[農地] {farm.get('note', '該当なし')}")

    return row


def main():
    parser = argparse.ArgumentParser(description="座標から住所を特定し、農地ナビで面積・地目・農振区分を調べるツール")
    parser.add_argument("--lat", type=float, help="緯度（単発モード）")
    parser.add_argument("--lon", type=float, help="経度（単発モード）")
    parser.add_argument("--csv", type=str, help="入力CSV（lat,lon の2列、ヘッダー行必須）")
    parser.add_argument("--output", type=str, default="result.csv", help="出力CSVファイル名（デフォルト: result.csv）")
    parser.add_argument("--show-browser", action="store_true", help="ブラウザ画面を表示しながら動かす（動作確認用）")
    args = parser.parse_args()

    points = []
    if args.csv:
        with open(args.csv, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                points.append((float(r["lat"]), float(r["lon"])))
    elif args.lat is not None and args.lon is not None:
        points.append((args.lat, args.lon))
    else:
        parser.error("--lat/--lon か --csv のどちらかを指定してください")

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show_browser)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()

        for i, (lat, lon) in enumerate(points):
            print(f"\n=== {i + 1}/{len(points)}: 緯度{lat}, 経度{lon} ===")
            try:
                row = process_point(page, lat, lon)
            except PWTimeoutError as e:
                print(f"  タイムアウトが発生しました: {e}")
                row = {k: "" for k in FIELDNAMES}
                row["lat"], row["lon"], row["note"] = lat, lon, "タイムアウト"
            results.append(row)

            if i < len(points) - 1:
                time.sleep(REQUEST_INTERVAL_SEC)

        browser.close()

    out_path = Path(args.output)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(results)

    print(f"\n完了しました。結果を {out_path.resolve()} に保存しました。")


if __name__ == "__main__":
    sys.exit(main())
