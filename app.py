#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
土地探しツール - ローカルWebアプリ (app.py)
=========================================

ブラウザの画面から緯度経度を入力すると、住所と農地情報
（所在地番・地目・面積・農振区分）を自動で調べ、結果を一覧表にまとめて
表示するツールです。land_finder.py と同じ仕組み（HeartRails Geo API +
農地ナビの地図データ直接取得）をWeb画面から使えるようにしたものです。

起動方法:
    python app.py

起動すると自動でブラウザが開き、次のURLが表示されます:
    http://127.0.0.1:5000

※ このURLは、このアプリを起動しているパソコンの中だけで有効な
   「ローカルURL」です。他の人のパソコンやスマホから開くことはできません。
   （他の人と共有できる公開URLにするには、別途サーバーへのデプロイが必要です）
"""

import csv
import io
import os
import re
import secrets
import threading
import webbrowser
from datetime import datetime
from functools import wraps

from flask import Flask, request, redirect, url_for, send_file, render_template_string, Response
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from land_finder import (
    reverse_geocode,
    lookup_nouchi_navi,
    USER_AGENT,
    STEALTH_INIT_SCRIPT,
)

COORD_SPLIT_RE = re.compile(r"[,\s、，]+")


def parse_latlon(text: str) -> tuple[float, float]:
    """「37.7700, 139.0500」のような1つの文字列を緯度・経度に分解する。

    カンマ・全角カンマ・スペース・全角スペースのいずれの区切りにも対応。
    Googleマップで座標をコピーした際の書式（緯度, 経度／(緯度, 経度)）を想定。
    """
    text = text.strip().strip("()（）[]「」　 ")
    parts = [p for p in COORD_SPLIT_RE.split(text) if p]
    if len(parts) != 2:
        raise ValueError("緯度と経度の2つの数値を、カンマまたはスペース区切りで入力してください")
    lat, lon = float(parts[0]), float(parts[1])
    return lat, lon


app = Flask(__name__)

# ---------------------------------------------------------------------------
# アクセス制限（パスワード保護 + いつでも止められるスイッチ）
# ---------------------------------------------------------------------------
# SITE_PASSWORD: このパスワードを知っている人だけがアクセスできる。
#                共有したい相手にだけパスワードを伝え、やめたいときは
#                この環境変数を変更するだけで、それまでの相手も含めて
#                全員のアクセスを即座に無効化できる。
# SITE_ENABLED : "false" にすると、パスワードを知っていても誰もアクセス
#                できない「一時停止」状態になる（ホスティング側の環境変数を
#                変更するだけで、コードの変更・再デプロイ不要）。
SITE_USERNAME = os.environ.get("SITE_USERNAME", "guest")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
SITE_ENABLED = os.environ.get("SITE_ENABLED", "true").strip().lower() not in ("false", "0", "off")
# ホスティング環境（Render/Railway/Fly.io等）は起動時にPORTを自動的に設定するため、
# それを「公開環境かどうか」の目印として使う。ローカルでの動作確認時は
# SITE_PASSWORD を設定しない限り、これまで通りパスワード無しで使える。
IS_DEPLOYED = "PORT" in os.environ

MAINTENANCE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>土地探しツール</title></head>
<body style="font-family:sans-serif;text-align:center;padding:5rem 1rem;color:#444;">
<h1>現在このツールは一時停止中です</h1>
<p>公開者が再開するまでお待ちください。</p>
</body></html>"""


def _check_password(username: str, password: str) -> bool:
    if not SITE_PASSWORD:
        return False  # パスワード未設定の場合は誰も入れない（安全側デフォルト）
    return secrets.compare_digest(username, SITE_USERNAME) and secrets.compare_digest(password, SITE_PASSWORD)


def _auth_required() -> bool:
    return IS_DEPLOYED or bool(SITE_PASSWORD)


@app.before_request
def _access_control():
    if not SITE_ENABLED:
        return MAINTENANCE_HTML, 503

    if not _auth_required():
        return  # ローカルで動かしていて、かつパスワード未設定ならそのまま使える

    auth = request.authorization
    if not auth or not _check_password(auth.username or "", auth.password or ""):
        return Response(
            "パスワードが必要です。共有元から伝えられたユーザー名・パスワードを入力してください。\n"
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Land Finder"'},
        )


LOCK = threading.Lock()
RESULTS: list[dict] = []  # メモリ上に貯める検索結果（サーバーを止めると消えます）

DISPLAY_COLUMNS = [
    ("searched_at", "検索日時"),
    ("lat", "緯度"),
    ("lon", "経度"),
    ("address", "住所（目安）"),
    ("所在地番", "所在地番"),
    ("地目", "地目"),
    ("面積(m2)", "面積(m2)"),
    ("農振区分", "農振区分"),
    ("距離m", "座標との距離(m)"),
    ("近隣区画数", "近隣区画数"),
    ("note", "備考"),
]

# ---------------------------------------------------------------------------
# Playwright ブラウザはアプリ起動時に1つだけ立ち上げて使い回す（毎回起動すると遅いため）
# ---------------------------------------------------------------------------
_playwright = None
_browser = None
_context = None
_page = None


def get_page():
    global _playwright, _browser, _context, _page
    if _page is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=True)
        _context = _browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
        _context.add_init_script(STEALTH_INIT_SCRIPT)
        _page = _context.new_page()
    return _page


def run_search(lat: float, lon: float) -> dict:
    row = {"searched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "lat": lat, "lon": lon, "address": "", "所在地番": "", "地目": "",
           "面積(m2)": "", "農振区分": "", "距離m": "", "近隣区画数": "", "note": ""}
    try:
        geo = reverse_geocode(lat, lon)
        if geo is None:
            row["note"] = "住所が特定できませんでした"
            return row
        row["address"] = geo["address"]

        page = get_page()
        farm = lookup_nouchi_navi(page, lat, lon, geo["address"])
        row.update(farm)
    except PWTimeoutError:
        row["note"] = "タイムアウトが発生しました。もう一度お試しください。"
    except Exception as e:  # 想定外のエラーもUI上に出す
        row["note"] = f"エラー: {e}"
    return row


# ---------------------------------------------------------------------------
# 画面
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>土地探しツール</title>
<style>
  body { font-family: "Segoe UI", "Hiragino Sans", "Meiryo", sans-serif; background: #f5f6f8; color: #222; margin: 0; padding: 2rem; }
  h1 { font-size: 1.4rem; margin-bottom: 0.3rem; }
  .sub { color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }
  form.search { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1.2rem 1.5rem; display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap; margin-bottom: 1.5rem; }
  .field { display: flex; flex-direction: column; gap: 0.3rem; }
  label { font-size: 0.8rem; color: #555; }
  input[type=text] { padding: 0.5rem 0.6rem; border: 1px solid #ccc; border-radius: 6px; font-size: 0.95rem; width: 12rem; }
  button { padding: 0.55rem 1.2rem; border: none; border-radius: 6px; background: #2f6f4f; color: #fff; font-size: 0.95rem; cursor: pointer; }
  button:hover { background: #255a3f; }
  button.secondary { background: #888; }
  button.secondary:hover { background: #666; }
  .actions { margin-bottom: 0.8rem; display: flex; gap: 0.6rem; }
  table { border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #ddd; border-radius: 8px; overflow: hidden; font-size: 0.85rem; }
  th, td { padding: 0.5rem 0.7rem; border-bottom: 1px solid #eee; text-align: left; white-space: nowrap; }
  th { background: #eef2ef; color: #333; position: sticky; top: 0; }
  tr:hover td { background: #fafcfb; }
  .empty { color: #888; padding: 2rem; text-align: center; background: #fff; border: 1px dashed #ccc; border-radius: 8px; }
  .table-wrap { overflow-x: auto; }
  .note-warn { color: #a35b00; }
  .note-ok { color: #2f6f4f; }
</style>
</head>
<body>
  <h1>土地探しツール</h1>
  <div class="sub">緯度経度を入力すると、住所と農地情報（所在地番・地目・面積・農振区分）を自動で調べます。</div>

  <form class="search" method="post" action="{{ url_for('search') }}">
    <div class="field">
      <label for="latlon">座標（緯度, 経度）</label>
      <input type="text" id="latlon" name="latlon" placeholder="例: 37.7700, 139.0500" style="width: 20rem;" required autofocus>
    </div>
    <button type="submit">検索する</button>
  </form>

  <div class="actions">
    <form method="get" action="{{ url_for('download') }}"><button class="secondary" type="submit">結果をCSVでダウンロード</button></form>
    <form method="post" action="{{ url_for('clear') }}" onsubmit="return confirm('検索結果を全て消去します。よろしいですか？');"><button class="secondary" type="submit">結果をクリア</button></form>
  </div>

  {% if results %}
  <div class="table-wrap">
  <table>
    <thead>
      <tr>{% for key, label in columns %}<th>{{ label }}</th>{% endfor %}</tr>
    </thead>
    <tbody>
      {% for row in results %}
      <tr>
        {% for key, label in columns %}
          <td class="{{ 'note-warn' if key == 'note' and row.get(key) else '' }}">{{ row.get(key, '') }}</td>
        {% endfor %}
      </tr>
      {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="empty">まだ検索結果がありません。上のフォームから緯度経度を入力して検索してください。<br>
  （1件あたり数秒〜十数秒かかります）</div>
  {% endif %}
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(PAGE_TEMPLATE, results=RESULTS, columns=DISPLAY_COLUMNS)


@app.route("/search", methods=["POST"])
def search():
    raw = request.form.get("latlon", "")
    try:
        lat, lon = parse_latlon(raw)
    except ValueError:
        with LOCK:
            RESULTS.insert(0, {
                "searched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "lat": "", "lon": "", "address": "", "所在地番": "", "地目": "",
                "面積(m2)": "", "農振区分": "", "距離m": "", "近隣区画数": "",
                "note": f"入力形式が正しくありません（例: 37.7700, 139.0500）: 「{raw}」",
            })
        return redirect(url_for("index"))

    with LOCK:
        row = run_search(lat, lon)
        RESULTS.insert(0, row)  # 新しい結果を上に追加

    return redirect(url_for("index"))


@app.route("/clear", methods=["POST"])
def clear():
    with LOCK:
        RESULTS.clear()
    return redirect(url_for("index"))


@app.route("/download")
def download():
    fieldnames = [key for key, _ in DISPLAY_COLUMNS]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(RESULTS)
    data = output.getvalue().encode("utf-8-sig")
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name="land_finder_result.csv",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    if IS_DEPLOYED:
        if not SITE_PASSWORD:
            print("警告: SITE_PASSWORD が設定されていません。公開環境では誰もアクセスできません。")
        print(f"起動しました（公開モード / ポート:{port}）。")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=False)
    else:
        url = f"http://127.0.0.1:{port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        print(f"起動しました。ブラウザで {url} を開いてください。")
        if SITE_PASSWORD:
            print(f"パスワード保護が有効です（ユーザー名: {SITE_USERNAME}）。")
        print("終了するには、このウィンドウで Ctrl+C を押してください。")
        app.run(host="127.0.0.1", port=port, debug=False, threaded=False)
