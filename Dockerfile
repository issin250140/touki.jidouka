# 土地探しツール - 公開用コンテナ定義
#
# Render / Railway / Fly.io など「Dockerfileから直接デプロイできる」
# ホスティングサービス向けです。Playwright(Chromium)が動くように
# 必要なライブラリを含めてビルドします。

FROM python:3.12-slim

WORKDIR /app

# Playwright(Chromium)が必要とするOS側の共有ライブラリを含めてインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

ENV PORT=8080
EXPOSE 8080

# 1ワーカー・スレッド分割なしで起動。
# ブラウザ自動操作(Playwright)は「作成したスレッドからしか操作できない」制約があるため、
# 複数スレッドで動かすと「別のスレッドには切り替えられません」というエラーになる。
# リクエストを1件ずつ順番に処理することで、常に同じスレッドから操作するようにしている。
CMD ["sh", "-c", "gunicorn -w 1 --timeout 120 -b 0.0.0.0:${PORT} app:app"]
