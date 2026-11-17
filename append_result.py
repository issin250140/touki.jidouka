#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
append_result.py
=================
座標を1件検索し、その結果を「農地検索結果.xlsx」に1行追記する。

使い方:
    python append_result.py --lat 36.0302500 --lon 139.3481389
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

from land_finder import reverse_geocode, lookup_nouchi_navi, block_heavy_resources, VIEWPORT, USER_AGENT, STEALTH_INIT_SCRIPT
from playwright.sync_api import sync_playwright

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SHEET_PATH = Path(__file__).resolve().parent / "農地検索結果.xlsx"
HEADER_ROW = 3

# 住所の先頭の都道府県名から、書き込むシートを自動判定する
KYUSHU_PREFECTURES = ["福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"]
KANTO_PREFECTURES = ["東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県"]


def guess_sheet(address: str) -> str:
    """住所の文字列から、書き込み先シート名（九州／関東／その他）を判定する。"""
    for pref in KYUSHU_PREFECTURES:
        if address.startswith(pref):
            return "九州"
    for pref in KANTO_PREFECTURES:
        if address.startswith(pref):
            return "関東"
    return "その他"


def append_row(lat: float, lon: float, row: dict) -> str:
    wb = openpyxl.load_workbook(SHEET_PATH)
    sheet_name = guess_sheet(row.get("address", ""))
    ws = wb[sheet_name]

    target_row = HEADER_ROW + 1
    while ws.cell(row=target_row, column=1).value is not None:
        target_row += 1

    values = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        f"{lat}, {lon}",
        row.get("address", ""),
        row.get("所在地番", ""),
        row.get("地目", ""),
        row.get("面積(m2)", ""),
        row.get("農振区分", ""),
        row.get("note", ""),
    ]
    for col, v in enumerate(values, start=1):
        ws.cell(row=target_row, column=col, value=v)

    wb.save(SHEET_PATH)
    return sheet_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    args = parser.parse_args()

    row = {"address": "", "所在地番": "", "地目": "", "面積(m2)": "", "農振区分": "", "note": ""}

    geo = reverse_geocode(args.lat, args.lon)
    if geo is None:
        row["note"] = "住所が特定できませんでした"
    else:
        row["address"] = geo["address"]
        print(f"[住所] {geo['address']}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT)
            context.add_init_script(STEALTH_INIT_SCRIPT)
            block_heavy_resources(context)
            page = context.new_page()
            farm = lookup_nouchi_navi(page, args.lat, args.lon, geo["address"])
            browser.close()
        row.update(farm)

    if row.get("所在地番"):
        print(f"[農地] {row['所在地番']} / 地目:{row['地目']} / 面積:{row['面積(m2)']}m2 / 農振区分:{row['農振区分']}")
    else:
        print(f"[農地] {row.get('note', '該当なし')}")

    sheet_name = append_row(args.lat, args.lon, row)
    print(f"\n{SHEET_PATH.name} の「{sheet_name}」シートに追記しました。")


if __name__ == "__main__":
    sys.exit(main())
