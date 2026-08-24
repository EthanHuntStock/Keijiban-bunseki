# -*- coding: utf-8 -*-
"""
public_export.py - 一般公開用(Googleサイト「掲示板の分析による投資情報」)集計値
エクスポート層。既存の export_signal.py(トレPJ向け内部シグナル・別スキーマ・別目的)
には一切触れない、完全に独立した新規モジュール。

【最重要・絶対厳守】個別投稿のテキスト・ユーザー名・投稿者ハッシュ(author)・投稿ID等は
一切出力しない。集計済みの数値(強気率・弱気率・投稿量等)だけを出力する。
  - build_public_record() は生コメントのリストを引数として受け取らない設計にすることで、
    混入を「構造的に」防ぐ(呼び手が signals.compute_signals() 等の集計済み結果だけを渡す)。
  - validate_no_leak() が出力レコードを再帰的に走査し、個別投稿由来と疑われるキー名
    (text/user/author/id等)が万一まぎれ込んでいないかを機械検証する。
  - write_public_export() は validate_no_leak() を通らないレコードは書き込まずに
    例外を送出して中止する(fail-closed)。

I/Oパターンは export_signal.py を参考にしつつ独立実装:
  - history.jsonl に1行 append(退避主義=書換/削除しない)
  - latest.json を atomic 置換(temp -> os.replace = Dropbox/OneDrive WinError5 安全)

raw_comments.jsonl / analyzed.jsonl / snapshots.jsonl 等の追記専用ログは、このモジュールでは
読み取り専用として扱う(書込み・削除・切り詰め一切なし)。

Phase 1: ローカルのエクスポート層構築のみ。run_once.py への自動結線(毎時実行への組込み)は
次フェーズで対応(今回は行わない)。
"""
import os
import sys
import json
import datetime as dt

import config


SCHEMA_VERSION = config.PUBLIC_EXPORT_SCHEMA_VERSION
COMPANY_NAME = "キオクシアホールディングス"
DISCLAIMER = ("本情報は掲示板投稿を統計的に集計したものであり、投資助言ではありません。"
              "個別の投稿内容は含まれていません。研究・エンタメ用途です。")

# snapshots.jsonl 等を読む際の窓(営業日ではなく暦日。14日トレンドに十分な安全マージン)。
TREND_READ_WINDOW_DAYS = 60

# 個別投稿由来の疑いがあるキー名(小文字化して完全一致で判定=構造的なキー汚染を検出)。
# 「含む(部分一致)」ではなく「完全一致」なので、post_count_today 等の集計キー名を
# 誤検知しない。
_LEAK_KEY_HINTS = {
    "text", "body", "comment", "comments", "content", "message", "raw_text",
    "user", "username", "user_id", "userid", "author", "author_id", "authorid",
    "handle", "poster", "nickname", "screen_name", "screenname",
    "id", "post_id", "postid", "comment_id", "commentid", "raw_id",
    "reply_to", "in_reply_to", "url", "link", "permalink",
    "votes_yes", "votes_no", "feel",
}

# 唯一の意図的な例外(パス完全一致・かつ値が文字列の時のみ): ai_commentary.text は
# public_insight.generate_public_insight() が「①で validate_no_leak() を通過済みの
# 公開レコード(集計値のみ)」だけを入力に生成する公開用AI考察文であり、個別投稿由来
# ではない。ここだけキー名"text"での自動検出対象から除外する(値がdict/listなら
# 除外せず通常どおり再帰検査=中に個別投稿ぽいキーが紛れ込んでいれば引き続き検出する)。
# これ以外の場所(board/trend_14d/price_sentiment_series 等)に出現する"text"キーは
# これまでどおり漏洩として検出する。
_LEAK_KEY_EXEMPT_EXACT_PATHS = {"$.ai_commentary.text"}


# ============================================================================
# 純関数: 日次トレンド組み立て(snapshots.jsonl の日次集計値だけを使う・個別投稿は不参照)
# ============================================================================
def _last_snapshot_per_day(snapshot_rows):
    """snapshots.jsonl の行(1日に複数回書かれる)から、日付ごとに「その日最後の1件」
    (=その日の累計を最も反映したもの)を抽出する。純関数。個別投稿は一切参照しない
    (snapshot_rows は snapshot.write_snapshot() が既に集計したロールアップの並び)。"""
    by_date = {}
    for r in snapshot_rows or []:
        d = r.get("date")
        if not d:
            continue
        by_date[d] = r  # append-only前提=後に出てくるほど新しい -> 後勝ちでOK
    return by_date


def trend_14d_from_snapshots(snapshot_rows, days=14):
    """直近 days 日分の [{date, post_count, bear_ratio, bull_ratio}, ...] を
    snapshots.jsonl の日次最終スナップショットから組み立てる。純関数。

    signals.compute_signals() の日次ロールアップ(snapshot.write_snapshot() が
    'signals' キーに埋めている sig_rollup)を再利用するだけで、生コメントを
    再走査しない。sig_rollup が無い(rollup失敗)行は day_cumulative へフォールバック。
    """
    by_date = _last_snapshot_per_day(snapshot_rows)
    out = []
    for d in sorted(by_date)[-days:]:
        snap = by_date[d] or {}
        sig = snap.get("signals") or {}
        post_count = sig.get("true_volume")
        if post_count is None:
            post_count = snap.get("day_cumulative")
        out.append({
            "date": d,
            "post_count": int(post_count) if post_count is not None else 0,
            "bear_ratio": sig.get("bear_ratio"),
            "bull_ratio": sig.get("bull_ratio"),
        })
    return out


# ============================================================================
# 純関数: 価格つきセンチメント系列(price_sentiment_series)組み立て
# ============================================================================
def _price_ohlc_by_date(price_daily, gmtoffset=None):
    """price_fetch.load_price(config.PRICE_DAILY_PATH) の戻り値(日足パース済みdict)
    から {date(JST, 'YYYY-MM-DD'): {open, high, low, close}} を組み立てる。純関数
    (ネット非依存)。price_daily / bars / close 欠損時は {} を返す(=その日は
    「実際に取引があった営業日」の集合に含まれないという意味を持つ。呼び手の
    price_sentiment_series_from_snapshots() はこの辞書のキー集合そのものを
    「直近N営業日」の判定に使うため、ここで値を捏造しないことが重要)。

    ★2026-08-19: 従来は close だけを抜き出していたが(_price_close_by_date)、
    ユーザー依頼「価格推移をローソク足にしてほしい」を受けopen/high/lowも合わせて
    抜き出すよう拡張。日足バー自体が既にYahoo Finance側で1日分のOHLCとして確定
    済みの値なので、ここでの追加集計(リサンプル等)は不要でそのまま使う。

    ★2026-08-19(追加): ユーザー依頼「価格推移に出来高を足せますか」を受けvolumeも
    抜き出す。日足バーは1日=1本のため合算不要でそのまま使う(欠損はNone)。

    既存の日足取得関数(price_fetch.parse_chart_json)が返す bars[].ts は UNIX epoch
    (UTC秒)なので、dashboard.py の _bars_to_dt と同じ式(+gmtoffset→JST日付)で変換する。
    新規にCSV等を再パースせず、既存の price_daily 構造をそのまま使う。
    """
    price_daily = price_daily or {}
    bars = price_daily.get("bars") or []
    meta = price_daily.get("meta") or {}
    off = gmtoffset if gmtoffset is not None else (meta.get("gmtoffset") or 32400)
    out = {}
    for b in bars:
        ts = b.get("ts")
        c = b.get("close")
        if ts is None or c is None:
            continue
        d = dt.datetime.utcfromtimestamp(ts + off).strftime("%Y-%m-%d")
        out[d] = {  # 1日1本前提。複数本あれば後(=時系列で後方)のものを採用。
            "open": b.get("open"), "high": b.get("high"),
            "low": b.get("low"), "close": c, "volume": b.get("volume"),
        }
    return out


def _daily_ohlc_from_intraday(price_intraday, gmtoffset=None):
    """★2026-08-20追加(ユーザー指摘: 「過去14日間の推移は自己データで作成して
    いるのでは」を受けた改善)。price_fetch.load_price(config.PRICE_INTRADAY_PATH)
    の5分足バーをJST日付ごとに集計し、{date: {open, high, low, close, volume}}を
    組み立てる純関数(ネット非依存)。_price_ohlc_by_date()と同じ形の辞書を返す。

    背景: Yahoo Finance APIの日足(interval=1d)は日付切替直後の数十分〜1時間程度、
    直近営業日の終値が一時的にnullで返ってくることが実測で確認された(2026-08-20
    未明に実際に発生・price_fetch.parse_chart_jsonがnull終値のbarを正しく除外する
    ため、その日がohlc_by_dateから丸ごと欠落する)。一方、日中足(interval=5m)は
    その営業日の間ずっと収集済みの実データを保持しており(直近5日分)、そこから
    自前でOHLCVを組み立てれば同じ値が独立に得られる(実測でYahoo公式日足の
    OHLCと完全一致することを確認済み・volumeのみ5分足の合算のため若干のズレが
    生じ得る)。_price_ohlc_by_date()の呼び手は、この関数の結果を「日足に無い日を
    埋める補完専用」として使う(日足が既に持つ日は上書きしない=公式値を優先)。
    """
    price_intraday = price_intraday or {}
    bars = price_intraday.get("bars") or []
    meta = price_intraday.get("meta") or {}
    off = gmtoffset if gmtoffset is not None else (meta.get("gmtoffset") or 32400)
    buckets = {}
    for b in bars:
        ts = b.get("ts")
        c = b.get("close")
        if ts is None or c is None:
            continue
        d = dt.datetime.utcfromtimestamp(ts + off).strftime("%Y-%m-%d")
        o, h, low = b.get("open"), b.get("high"), b.get("low")
        vol = b.get("volume") or 0
        if d not in buckets:
            buckets[d] = {"open": o if o is not None else c,
                         "high": h if h is not None else c,
                         "low": low if low is not None else c,
                         "close": c, "volume": vol}
        else:
            cur = buckets[d]
            if h is not None:
                cur["high"] = max(cur["high"], h)
            if low is not None:
                cur["low"] = min(cur["low"], low)
            cur["close"] = c
            cur["volume"] += vol
    return buckets


def _today_yahoo_has_volume(price_intraday, today=None):
    """★2026-08-20追加(ユーザー指摘「最新の株価が反映されていない」への対応)。
    本日分のYahoo日中足(price_intraday)に、出来高>0のbarが1本でも含まれているかを
    判定する純関数。実測で確認した障害(2026-08-20): Yahoo Finance chart API
    (query1.finance.yahoo.com)自体が、寄り付き直後の出来高0の合成バー1本を
    返したまま、以降ずっと最新の実取引バーを返さなくなる不具合が発生することが
    ある(regularMarketTimeだけはリクエストのたびに進むが価格・出来高は凍結された
    まま)。0件/全て出来高0(または欠損)なら False を返す。呼び手はこれを
    「Yahoo側が本日まだ有効な実データを返していない」の判定に使い、
    nikkei225jp.comのTSE現在値フィード(adr_pts)へフォールバックする
    (寄り前の特別気配等で本当にまだ出来高が無い場合にも同じ理由で有効に働く)。
    """
    today = today or dt.date.today().isoformat()
    price_intraday = price_intraday or {}
    bars = price_intraday.get("bars") or []
    meta = price_intraday.get("meta") or {}
    off = meta.get("gmtoffset") or 32400
    for b in bars:
        ts = b.get("ts")
        if ts is None:
            continue
        d = dt.datetime.utcfromtimestamp(ts + off).strftime("%Y-%m-%d")
        if d != today:
            continue
        if b.get("volume"):
            return True
    return False


def _previous_close_price(price_daily, today=None):
    """★2026-08-20追加。price_daily(日足)から「today より前で最も新しい」日の
    close を返す純関数(本日の日足バー自体がまだ確定/信頼できない時間帯でも、
    前営業日の確定終値だけは安全に参照できる)。無ければNone。"""
    today = today or dt.date.today().isoformat()
    ohlc_by_date = _price_ohlc_by_date(price_daily)
    prior_dates = sorted(d for d in ohlc_by_date if d < today)
    if not prior_dates:
        return None
    return ohlc_by_date[prior_dates[-1]].get("close")


def _latest_tse_price_from_adr_pts(adr_pts_data, today=None):
    """★2026-08-20追加。nikkei225jp.comのTSE現在値フィード(adr_pts.tse列)から、
    本日分の最新値を返す純関数(公開ダッシュボードのヘッダー現在値のフォールバック用)。
    このフィードはYahoo Finance chart APIとは完全に独立したドメイン・別ファイル
    なので、Yahoo側が本日のデータを返せていない時でも取得できていることがある
    (2026-08-20に実測確認済み)。無ければNone。"""
    today = today or dt.date.today().isoformat()
    rows = (adr_pts_data or {}).get("rows") or []
    for r in sorted(rows, key=lambda r: r.get("ts") or 0, reverse=True):
        ts = r.get("ts")
        price = r.get("tse")
        if ts is None or price is None:
            continue
        d = (dt.datetime.utcfromtimestamp(ts) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
        if d == today:
            return {"price": price, "ts": ts}
    return None


def _today_tse_ohlc_from_adr_pts(adr_pts_data, today=None):
    """★2026-08-20追加。nikkei225jp.comのTSE現在値フィード(adr_pts.tse列)の本日分を
    intraday_today_series()と同じ10分足OHLCへリサンプルする純関数(Yahoo側が本日の
    有効な日中足を返せていない時の「本日の推移」フォールバック用)。このフィードには
    出来高が含まれないため price_volume は常にNone(捏造しない)。"""
    today = today or dt.date.today().isoformat()
    rows = (adr_pts_data or {}).get("rows") or []
    buckets = {}
    for r in sorted(rows, key=lambda r: r.get("ts") or 0):
        ts = r.get("ts")
        price = r.get("tse")
        if ts is None or price is None:
            continue
        d = dt.datetime.utcfromtimestamp(ts) + dt.timedelta(hours=9)
        if d.strftime("%Y-%m-%d") != today:
            continue
        bucket_minute = (d.minute // 10) * 10
        key = d.replace(minute=bucket_minute, second=0, microsecond=0).strftime("%H:%M")
        if key not in buckets:
            buckets[key] = {"open": price, "high": price, "low": price, "close": price}
        else:
            cur = buckets[key]
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
    return [{"time": t, "price_open": buckets[t]["open"], "price_high": buckets[t]["high"],
            "price_low": buckets[t]["low"], "price_close": buckets[t]["close"],
            "price_volume": None}
           for t in sorted(buckets)]


def _prev_close_from_price_sentiment_series(pss, today=None):
    """★2026-08-20追加(ユーザー依頼「60秒ごとの更新で、その時の株価になるように」
    への対応)。price_sentiment_series(既に公開レコードに含まれる日足OHLC系列)から、
    「today より前で最も新しい」日の price_close を返す純関数。公開ダッシュボード
    (Streamlit Cloud)側にはローカルの price_daily.json が無いため、既にSheets経由で
    受け取り済みの直近14営業日系列を前日終値の代わりに使う。無ければNone。"""
    today = today or dt.date.today().isoformat()
    prior = [p for p in (pss or [])
            if p.get("date") and p["date"] < today and p.get("price_close") is not None]
    if not prior:
        return None
    prior.sort(key=lambda p: p["date"])
    return prior[-1]["price_close"]


def fetch_live_price_header(price_sentiment_series=None, timeout=None):
    """★2026-08-20追加(ユーザー依頼「株価の更新が遅すぎる。60秒ごとの更新時に、
    その時の株価になるようにしてください」)。

    背景: 公開ダッシュボード(Streamlit Cloud)はこれまで、ローカルPCの本番パイプライン
    (10分毎のcatchup)→Google Sheetsブリッジ経由でしか価格を受け取っておらず、
    ページ自体は60秒毎に自動更新されていても、中身のデータは最大10分古いままだった
    (streamlit-autorefreshは「再描画」するだけで、Sheets側のデータ自体は別スケジュール
    でしか進まないため)。この関数は公開ダッシュボードのプロセス自身から、Yahoo Finance
    chart API(+本日ボリュームが無ければnikkei225jp.comのTSE現在値フィード。
    _today_yahoo_has_volume/_latest_tse_price_from_adr_ptsの障害検知ロジックを再利用)を
    直接叩き、「今この瞬間の株価」を取得する。60秒毎のオートリフレッシュのたびに
    呼ばれる想定の軽量フェッチ(BBS集計等は一切含まない・価格1点だけ)。

    price_sentiment_series を渡すと(公開レコードに既に含まれる)、そこから前営業日
    終値を求めて変化率も計算する。省略時はchange_pctはNone。

    fail-soft: Yahoo・nikkei225jp.comのどちらも失敗すればNoneを返す(呼び手は
    Sheets由来のrec['price']をそのまま表示し続ければよい=既存動作への後退にしか
    ならない)。ここでのネットワーク呼び出しは意図的にpublic_export.py内へ閉じる
    (公開ダッシュボード側=public_dashboard.pyはネットワークコードを持たない、
    という既存の役割分担を維持する)。
    """
    import price_fetch
    import requests

    # ★2026-08-20修正: このファイル内の「JST今日/現在時刻」取得は全て
    # dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + timedelta(hours=9)
    # という形式に統一(旧: dt.datetime.utcnow())。utcnow()はPython 3.12+で非推奨
    # (3.14でも動作はするがDeprecationWarningがStreamlit Cloudのログに実行の
    # たび大量出力されることを実測で確認)。.replace(tzinfo=None)でutcnow()と
    # 全く同じ「naiveなUTC値」に戻すため、このモジュール全体がnaive datetime
    # 前提の設計(既存の全比較・strftime呼び出し)への影響はゼロ。
    today = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    prev_close = _prev_close_from_price_sentiment_series(price_sentiment_series, today=today)

    def _chg(price):
        if price is None or not prev_close:
            return None
        return round((price - prev_close) / prev_close * 100.0, 2)

    try:
        raw = price_fetch._fetch(config.PRICE_INTRADAY_PARAMS)
        parsed = price_fetch.parse_chart_json(raw) if raw else None
    except Exception:
        parsed = None
    if parsed and not parsed.get("error") and _today_yahoo_has_volume(parsed, today=today):
        bars = parsed.get("bars") or []
        price = bars[-1]["close"] if bars else None
        if price is not None:
            return {"last": price, "change_pct": _chg(price), "source": "yahoo"}

    try:
        symbol = config.SYMBOL
        url = config.ADR_PTS_URL.format(symbol=symbol)
        r = requests.get(url, headers={"User-Agent": config.CHROME_UA,
                                       "Referer": config.ADR_PTS_REFERER.format(symbol=symbol)},
                         timeout=timeout or 8)
        if r.status_code == 200:
            rows = price_fetch.parse_adr_pts_js(r.text)
            fallback = _latest_tse_price_from_adr_pts({"rows": rows}, today=today)
            if fallback:
                return {"last": fallback["price"], "change_pct": _chg(fallback["price"]),
                       "source": "adr_pts"}
    except Exception:
        pass
    return None


def kabu_tick_today_summary(rows, today=None):
    """★2026-08-20追加(ユーザー指示: 公開ダッシュボードの価格をYahooでなく自己取得の
    kabuティックデータへ切替え・60秒毎に最新値へ更新する)。

    株取引API_プロト1が既に自己収集しているkabuステーションAPIのティックCSV
    (ticks_285A_YYYY-MM-DD.csv・列= time,price,vwap,volume,bid,ask,tickvol。
    time="YYYY-MM-DD HH:MM:SS.mmm"・volume=その日の累積出来高[単調増加]・
    tickvolは先頭行等で空欄になり得るため使わずvolumeの差分から算出する)を
    csv.DictReader で読み込んだ行のリストを受け取り、intraday_today_series()と
    同じ10分足OHLC系列・当日1本のOHLC(14日足の当日ぶん差し替え用)・最新値を
    組み立てる純関数(ファイル読込自体は呼び手[live_price_bridge.py]が行う)。

    本日(today、省略時はJST今日)以外の行は無視する。価格/時刻いずれかが不正な
    行は無視する(fail-soft・捏造しない)。本日分の行が1つも無ければ
    {"price_pts": [], "day_bar": None, "last": None, "last_time": None} を返す。
    """
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    buckets = {}
    first_price = day_high = day_low = last_price = last_time = None
    prev_cum_vol = 0.0

    for r in rows or []:
        t = (r.get("time") or "").strip()
        if len(t) < 16 or not t.startswith(today):
            continue
        try:
            price = float(r.get("price"))
            hh, mm = int(t[11:13]), int(t[14:16])
        except (TypeError, ValueError):
            continue
        try:
            cum_vol = float(r.get("volume") or 0)
        except (TypeError, ValueError):
            cum_vol = prev_cum_vol
        bucket_vol = max(0.0, cum_vol - prev_cum_vol)
        prev_cum_vol = cum_vol

        bucket_minute = (mm // 10) * 10
        key = f"{hh:02d}:{bucket_minute:02d}"
        if key not in buckets:
            buckets[key] = {"open": price, "high": price, "low": price,
                            "close": price, "volume": bucket_vol}
        else:
            b = buckets[key]
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            b["volume"] += bucket_vol

        if first_price is None:
            first_price = price
        day_high = price if day_high is None else max(day_high, price)
        day_low = price if day_low is None else min(day_low, price)
        last_price = price
        last_time = t

    price_pts = [{"time": k, "price_open": buckets[k]["open"], "price_high": buckets[k]["high"],
                 "price_low": buckets[k]["low"], "price_close": buckets[k]["close"],
                 "price_volume": buckets[k]["volume"]}
                for k in sorted(buckets)]
    day_bar = None
    if first_price is not None:
        day_bar = {"date": today, "price_open": first_price, "price_high": day_high,
                  "price_low": day_low, "price_close": last_price, "price_volume": prev_cum_vol}
    return {"price_pts": price_pts, "day_bar": day_bar, "last": last_price, "last_time": last_time}


def intraday_today_high_low(price_pts):
    """★2026-08-21追加(ユーザー依頼「本日の価格推移のところに、最高値、最安値を
    書くようにしましょう」)。本日イントラデイの10分足系列(price_pts・各要素に
    price_high/price_lowを持つ)から、当日の最高値/最安値を1点ずつ拾う純関数。
    price_high/price_lowがNone/欠損の点は無視する(捏造しない)。有効な点が1つも
    無ければ(None, None)を返す(fail-soft・呼び手はNoneなら非表示にする)。"""
    highs = [p.get("price_high") for p in (price_pts or []) if p.get("price_high") is not None]
    lows = [p.get("price_low") for p in (price_pts or []) if p.get("price_low") is not None]
    return (max(highs) if highs else None, min(lows) if lows else None)


_ITAYOSE_RATIO = 15.0   # board_totals_60s_series()の板寄せ検出閾値(docstring参照)


def board_totals_60s_series(rows, today=None):
    """★2026-08-21追加(ユーザー依頼「板の買い・売り総計(成行を含めた全価格帯)を
    過去60秒間の平均値・60秒毎更新・折れ線グラフで公開ダッシュボードへ」。
    おにや10:42投稿で仕様確定・トレPJ10:47投稿で記録側に4列追加=
    over_sell_qty/under_buy_qty/market_sell_qty/market_buy_qty)。

    株取引API_プロト1が既に自己収集している板CSV(board_285A_YYYY-MM-DD.csv・
    time="YYYY-MM-DD HH:MM:SS.mmm"・buy1px..buy10px/buy1qty..buy10qty・
    sell1px..sell10px/sell1qty..sell10qty・[2026-08-21 11:30以降追加]
    over_sell_qty/under_buy_qty/market_sell_qty/market_buy_qty)をcsv.DictReaderで
    読み込んだ行のリストを受け取る純関数(ファイル読込自体は呼び手
    [board_totals_bridge.py]が行う)。

    ①各行の買い総計=buy1qty+...+buy10qty(表示10本)+under_buy_qty(表示外側の
    買い累計・OVER/UNDERは需給圧力比ではなく外側累計=[[reference-tse-preopen-mechanics]]
    参照)+market_buy_qty(成行買い)。売り総計も対称に計算。
    ②新4列(over_sell_qty等)が無い/不正な行(2026-08-21 11:30より前の記録・
    トレPJの記録拡張前)は、この4列だけを欠いたまま「表示10本のみの部分合計」を
    出すと『全価格帯』という前提と食い違い誤解を招くため、その行自体をこの指標
    からは除外する(捏造しない・fail-soft)。
    ③60秒バケット(時刻の秒を60で切り捨て)ごとに、バケット内の全行の買い/売り
    総計の単純平均を取る(intraday_today_series等と同じ「10分足→60秒足」の
    バケット平均パターン)。バケット内に有効な行が1件も無ければそのバケット自体を
    出力しない(0を捏造しない)。

    本日(today、省略時はJST今日)以外の行は無視する。戻り値は時刻昇順のリスト
    [{"time": "HH:MM", "buy_total": float, "sell_total": float}, ...]。

    ★2026-08-21修正(ユーザー指摘「グラフの横軸の秒の単位は不要」): 60秒
    バケットは分の境界(常に:00)へ切り捨てる設計のため、バケットキーに秒の桁を
    含めても常に"00"で情報量が無く冗長だった。キー形式を"HH:MM:SS"から
    "HH:MM"へ単純化する(バケット化のロジック自体=分単位でグルーピングする点は
    無変更)。

    ★2026-08-21修正(ユーザー指摘「15:30近辺が跳ね上がっており、計算を間違って
    いると思われる」): 15:30ちょうど(後場の大引け板寄せ)の板スナップショットは
    buy1qty/sell1qty(最良気配の数量)が板寄せで約定した数量をそのまま反映するため、
    通常の連続売買中の「気配に並んでいる残数量」とは意味が異なり桁違いに巨大な値
    になる(実測: 通常は数百〜数千のところ472,500/474,300等)。この行はRECORD_END=
    15:31の意図どおり「終値」を捉えるためには必要だが、板の買い/売り圧力を表す
    このtotals指標にとっては実態と異なるノイズであり、しかもこの瞬間は記録間隔が
    空いてバケット内で唯一の行になりがちなため平均が丸ごとこの異常値になり
    グラフが跳ね上がって見える。_is_trading_hours()が後場を15:30未満(15:30を
    含まない)と定義しているのと同じ境界に合わせ、15:30以降の行はこの指標から
    除外する(価格チャート側は従来どおりこの行を使い続けてよい・totals専用の除外)。

    ★2026-08-21追加(ユーザー依頼「改善提案①=開場直後の板寄せ希薄化リスクに
    先回りで対応」): 上の15:30除外は「大引け」という時刻境界に依存する対処だが、
    寄り付き板寄せ(前場9:00・後場12:30の開始直後)にも同種の現象が起きうる
    (実測: 2026-08-21 09:01:41にbuy1qty=267,100 vs buy2qty=200など、他の価格帯が
    薄いまま最良気配だけ桁違いに積み上がる)。時刻境界(いつ寄り付き板寄せが記録
    されるかは記録開始タイミング次第で一定しない)ではなく、板の"形"そのもので
    itayose行を検出する: buy1qty(またはsell1qty)がbuy2〜10qty(sell2〜10qty)の
    合計の_ITAYOSE_RATIO倍を超える行は、連続売買中の通常の板とは形が異なる
    (板寄せで最良気配へ約定数量が集中)とみなしこの指標から除外する。実測では
    通常時の比率は概ね1〜3倍、板寄せ時は40〜1300倍と桁で分離できるため、
    15倍という閾値には十分な安全マージンがある。他方の気配(rest計=0)は
    比較不能なため対象外とする(誤って通常の薄い板を除外しない)。
    """
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    buckets = {}   # bucket_key -> {"buy": [...], "sell": [...]}

    for r in rows or []:
        t = (r.get("time") or "").strip()
        if len(t) < 19 or not t.startswith(today):
            continue
        try:
            hh, mm = int(t[11:13]), int(t[14:16])
        except (TypeError, ValueError):
            continue
        if hh * 60 + mm >= 15 * 60 + 30:
            # 大引け板寄せ(15:30以降)は連続売買の板状態ではないため除外(上記docstring参照)
            continue
        try:
            buy_qtys = [float(r.get(f"buy{i}qty") or 0) for i in range(1, 11)]
            sell_qtys = [float(r.get(f"sell{i}qty") or 0) for i in range(1, 11)]
            under_buy = float(r["under_buy_qty"])
            over_sell = float(r["over_sell_qty"])
            market_buy = float(r["market_buy_qty"])
            market_sell = float(r["market_sell_qty"])
        except (TypeError, ValueError, KeyError):
            # 新4列が無い/空/不正な行(拡張前の記録)は「全価格帯」の前提が崩れる
            # ため、この指標からは行ごと除外する(部分合計を捏造しない)。
            continue
        # ★2026-08-21追加: 板寄せ(itayose)行の検出(上記docstring参照)。最良気配
        # (buy1qty/sell1qty)が他の価格帯(buy2〜10/sell2〜10の合計)の
        # _ITAYOSE_RATIO倍を超えて突出している行は除外する。
        rest_buy = sum(buy_qtys[1:])
        rest_sell = sum(sell_qtys[1:])
        if ((rest_buy > 0 and buy_qtys[0] > _ITAYOSE_RATIO * rest_buy) or
                (rest_sell > 0 and sell_qtys[0] > _ITAYOSE_RATIO * rest_sell)):
            continue
        buy_visible = sum(buy_qtys)
        sell_visible = sum(sell_qtys)
        buy_total = buy_visible + under_buy + market_buy
        sell_total = sell_visible + over_sell + market_sell

        key = f"{hh:02d}:{mm:02d}"   # 分単位バケット(60秒平均・秒の桁は表示しない)
        b = buckets.setdefault(key, {"buy": [], "sell": []})
        b["buy"].append(buy_total)
        b["sell"].append(sell_total)

    return [
        {"time": k,
         "buy_total": round(sum(buckets[k]["buy"]) / len(buckets[k]["buy"]), 1),
         "sell_total": round(sum(buckets[k]["sell"]) / len(buckets[k]["sell"]), 1)}
        for k in sorted(buckets)
    ]


def board_score_daily_series(history_rows, days=14, today=None):
    """★2026-08-20追加(ユーザー提案「灼熱/阿鼻叫喚メーターに推移スパークラインを」)。
    history.jsonl相当の行リスト(古い→新しい順の想定・append-onlyなので実際そうなる)
    から、日付(generated_atの先頭10文字・JST日付文字列)ごとに「その日最後に記録された」
    board.overheat_score/capitulation_scoreを1点だけ拾い、直近days日ぶんを日付昇順で
    返す純関数。today(省略時はJST今日)以降の行は除外する(=今日ぶんは含めない・
    呼び手[_build_from_live_data]が今まさに計算した最新値を別途1点追加する設計。
    理由: history.jsonlは1日に何度もappendされるため、今日分をここに混ぜると
    「今日の最後の値」ではなく「このrunより前の直近の値」を拾ってしまい、
    呼び手が本来渡したい"今この瞬間"の値と二重管理になる)。
    データ蓄積が浅くdays日に満たない場合はある分だけを返す(fail-soft・捏造しない)。
    """
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    by_date = {}
    for row in history_rows or []:
        gen_at = (row or {}).get("generated_at")
        if not gen_at:
            continue
        date = str(gen_at)[:10]
        if date >= today:
            continue
        board = (row or {}).get("board") or {}
        by_date[date] = {"date": date, "overheat_score": board.get("overheat_score"),
                         "capitulation_score": board.get("capitulation_score")}
    ordered = [by_date[d] for d in sorted(by_date)]
    return ordered[-days:] if days else ordered


def signal_cards_daily_series(history_rows, days=14, today=None):
    """★2026-08-25追加(ユーザー指摘「公開用ダッシュボード(streamlit版)は直ってないのでは」
    =画像版のみに実装していたシグナル推移スパークラインをstreamlit版にも追加する際、
    board_score_daily_series()と同じ「読み取り専用モジュールへ集約する」設計に揃えた)。

    history.jsonl相当の行リスト(各行はrec相当のdictで"signal_cards"キーに
    [{name, value, threshold, state, note}, ...]を持つ)から、9指標それぞれの
    name毎に「日付ごとにその日最後に記録されたvalue」を1点だけ拾い、直近days日ぶんを
    日付昇順で返す純関数。board_score_daily_series()と全く同じ日次集約パターン
    (1日1スナップショット・today以降の行は除外=呼び手が今日ぶんの現在値を別途1点
    追加する設計)を、固定2キー(overheat_score/capitulation_score)ではなく
    signal_cardsに含まれる可変個のnameへ一般化したもの。

    戻り値: [{"date": "YYYY-MM-DD", "<指標名1>": value1, "<指標名2>": value2, ...}, ...]
    (board_score_daily_seriesと同型の「1行=1日・複数キー」形式)。
    signal_cards自体が無い行(機能追加以前の古いhistory行等)はスキップする
    (fail-soft・捏造しない)。データ蓄積が浅くdays日に満たない場合はある分だけを返す。
    """
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                      + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    by_date = {}
    for row in history_rows or []:
        gen_at = (row or {}).get("generated_at")
        if not gen_at:
            continue
        date = str(gen_at)[:10]
        if date >= today:
            continue
        cards = (row or {}).get("signal_cards") or []
        if not cards:
            continue
        entry = {"date": date}
        for c in cards:
            name = (c or {}).get("name")
            if name:
                entry[name] = c.get("value")
        by_date[date] = entry
    ordered = [by_date[d] for d in sorted(by_date)]
    return ordered[-days:] if days else ordered


def signal_state_changes(current_cards, history_rows, today=None):
    """★2026-08-20追加(ユーザー提案「9指標の状態変化が分かるように」)。
    現在の9指標カード(current_cards・signals.compute_signals()のS['cards']相当、
    各要素は{name, state, ...})と、history.jsonlの中で直近に記録された取引日
    (today[省略時はJST今日]より前で最新の日)の最終スナップショットのsignal_cardsを
    比較し、発火状態(OK/警戒/発火)が変わったカードだけを返す純関数。
    比較対象となる過去日のスナップショットが1件も無い(運用開始直後でhistory.jsonl
    が浅い等)場合は、比較不能を「変化なし」と誤表示しないよう空リストを返す
    (fail-soft)。
    """
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    by_date = {}
    for row in history_rows or []:
        gen_at = (row or {}).get("generated_at")
        if not gen_at:
            continue
        date = str(gen_at)[:10]
        if date >= today:
            continue
        cards = (row or {}).get("signal_cards")
        if cards:
            by_date[date] = cards
    if not by_date:
        return []
    last_date = sorted(by_date)[-1]
    prev_state = {c.get("name"): c.get("state") for c in (by_date[last_date] or [])}
    changes = []
    for c in current_cards or []:
        name = c.get("name")
        cur_state = c.get("state")
        old_state = prev_state.get(name)
        if old_state is not None and old_state != cur_state:
            changes.append({"name": name, "from": old_state, "to": cur_state,
                            "compared_date": last_date})
    return changes


def previous_deltas(rec):
    """★2026-08-20追加(ユーザー提案「AI考察の前回比較を視覚的なバッジでも」)。
    rec['previous'](前回生成時点のスナップショット・previous_snapshot_for_ai_commentary
    が組み立てたもの)と、rec自身の現在値(price/board)から、価格・強気比率・弱気比率・
    投稿数の差分(現在−前回)を計算する純関数。UI側(st.metricのdelta引数等)にそのまま
    渡せる形で返す。比較不能な項目(値欠損・previous自体が無い等)はNone(fail-soft・
    ゼロで埋めて「変化なし」と誤表示しない)。
    """
    prev = (rec or {}).get("previous") or {}
    price = (rec or {}).get("price") or {}
    board = (rec or {}).get("board") or {}

    def _diff(cur, old):
        if cur is None or old is None:
            return None
        return cur - old

    return {
        "price_last": _diff(price.get("last"), prev.get("price_last")),
        "bull_ratio": _diff(board.get("bull_ratio"), prev.get("bull_ratio")),
        "bear_ratio": _diff(board.get("bear_ratio"), prev.get("bear_ratio")),
        "post_count_today": _diff(board.get("post_count_today"), prev.get("post_count_today")),
        "previous_generated_at": prev.get("generated_at"),
    }


def next_commentary_failure_streak(prev_streak, succeeded):
    """★2026-08-20追加(ユーザー提案「AI考察生成の失敗が静かに握りつぶされないように」)。
    AI考察(ai_commentary)生成の成功/失敗から、次の「連続失敗回数」を返す純関数。
    成功なら0にリセット、失敗ならprev_streak+1。ファイルI/O自体は呼び手
    (run_once.py)が担う(状態の永続化とロジックを分離し、ロジックをselftest
    対象にする=既存の「純関数とI/Oの分離」パターンを踏襲)。"""
    if succeeded:
        return 0
    return (prev_streak or 0) + 1


def next_lock_busy_streak(prev_streak, was_busy):
    """★2026-08-21追加(おにや提案・連携ログ2026-08-21 01:38投稿=エンジニアの深堀質問③
    「analyzeロック長時間占有の自動検知は無いのでは」への回答で発覚)。
    analyze_lock/export_lockの取得busy(取れなかった)から、次の「連続busy回数」を返す
    純関数。ロジックはnext_commentary_failure_streak()と同型(busyでなければ0にリセット、
    busyならprev_streak+1)だが、2026-08-19に実際に起きた「おにやが3回連続で手動の実データ
    再検証でしか気づけなかった」障害の再発防止として、意味を明確にするため別名の関数として
    独立させる。ファイルI/O自体は呼び手(run_once.py)が担う(既存の純関数とI/Oの分離パターン
    を踏襲)。"""
    if not was_busy:
        return 0
    return (prev_streak or 0) + 1


def _is_trading_hours(now=None):
    """★2026-08-20追加。現在時刻(JST)が東証の取引時間帯(前場9:00-11:30・
    後場12:30-15:30・土日は除外。祝日カレンダーまでは見ない簡易判定)かどうかを
    返す純関数。live_price_bridge.pyは市場が開いていない間(夜間・週末・昼休み)は
    意図的に何もしない設計のため、鮮度警告(live_price_staleness_minutes)をこの
    時間外にも適用すると常に警告になってしまう(=正常な休止を異常と誤検知する)。
    呼び手はこれを鮮度警告の表示要否の判定に使う。"""
    now = now or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9))
    if now.weekday() >= 5:   # 土(5)・日(6)
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 <= hm < 11 * 60 + 30) or (12 * 60 + 30 <= hm < 15 * 60 + 30)


def market_session_label(now=None):
    """★2026-08-21追加(ユーザー依頼「公開ダッシュボードのAI考察では、日本市場の
    開場時間帯を考慮した考察をするように」)。現在時刻(JST)が東証のどの時間帯に
    あるかを、_is_trading_hours()の真偽二値よりも粒度細かく返す純関数。
    寄り付き前/前場中/昼休み/後場中/取引終了後/休場(土日)の6区分。祝日カレンダー
    までは見ない簡易判定(_is_trading_hours()と同じ制約・既存の呼び出し元互換のため
    _is_trading_hours()自体は変更しない)。
    public_insight.pyのAI考察プロンプトへ渡し、取引時間外に「現在の値動きは」の
    ような書き方を避け、時間帯に即した表現(「前場の終値時点では」「本日の終値は」等)
    をさせるために使う。"""
    now = now or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9))
    if now.weekday() >= 5:   # 土(5)・日(6)
        return "休場(土日)"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60:
        return "寄り付き前"
    if hm < 11 * 60 + 30:
        return "前場中"
    if hm < 12 * 60 + 30:
        return "昼休み"
    if hm < 15 * 60 + 30:
        return "後場中"
    return "取引終了後"


def live_price_staleness_minutes(generated_at, now=None):
    """★2026-08-20追加(ユーザー提案「live_price_bridgeの死活監視」への対応)。
    live_priceタブ(live_price_bridge.pyが1分毎に書き込む)のgenerated_at(ISO文字列)
    と現在時刻(JST)との差分を分単位で返す純関数。live_price_bridge.py自体が何らかの
    理由で止まっても(プロト1のティックファイルが更新されない・タスクスケジューラの
    停止・Sheets書込み失敗等)、公開ダッシュボード側はfail-softなフォールバック
    (Yahoo直接取得→Sheets由来のrec)へ黙って落ちるため、「古い値のまま更新が
    止まっている」ことに閲覧者もこちら側も気づけないリスクがあった
    (2026-08-20に実際に起きたトレPJ側の記録停止と同型のリスクをこちら自身にも
    予防的に入れる)。generated_at欠損/パース失敗はNone(fail-soft・呼び手は
    「鮮度不明」として警告を出さない)。"""
    if not generated_at:
        return None
    try:
        gen = dt.datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return None
    now = now or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9))
    return (now - gen).total_seconds() / 60.0


def load_live_price_from_url(url, timeout=None):
    """★2026-08-20追加。live_price_bridge.pyが書いたlive_priceタブ(「ウェブに公開」
    CSV書き出し・A1セルにJSON文字列)を公開ダッシュボード自身から読む。
    load_public_latest_from_url()と全く同じCSVブリッジパターン(_parse_public_json_csv
    に委譲)の別インスタンス。fail-soft: 失敗時はNone(呼び手は次のフォールバック
    [直接Yahoo取得→Sheets由来のrec['price']]へ進めばよい)。"""
    import requests
    try:
        r = requests.get(url, timeout=timeout or 8)
        if r.status_code != 200:
            return None
        return _parse_public_json_csv(r.text)
    except Exception:
        return None


def load_board_totals_from_url(url, timeout=None):
    """★2026-08-21追加。board_totals_bridge.pyが書いたboard_totalsタブ(「ウェブに
    公開」CSV書き出し・A1セルにJSON文字列=board_totals_60s_series()の当日ぶん)を
    公開ダッシュボード自身から読む。load_live_price_from_url()と全く同じCSV
    ブリッジパターン。fail-soft: 失敗時はNone(呼び手はこのチャート自体を表示しない
    フォールバックへ進む・他フィールドの表示には影響しない)。"""
    import requests
    try:
        r = requests.get(url, timeout=timeout or 8)
        if r.status_code != 200:
            return None
        return _parse_public_json_csv(r.text)
    except Exception:
        return None


def load_sentiment_24h_from_url(url, timeout=None):
    """★2026-08-20緊急追加(ユーザー報告「過去24時間のセンチメント推移が表示
    されない」への対応)。sentiment_last_24h(過去24時間・10分毎)をjson_blobへ
    そのまま含めるとGoogle Sheetsの1セル上限(50,000字)を超過し、json_blob全体の
    同期がAPIError[400]で毎回失敗する重大障害を引き起こしたため、
    public_sheets_sync.build_sentiment_24h_values()が書く専用タブ(sentiment_24h)
    から個別に読む。load_live_price_from_url()と同じCSVブリッジパターン。
    fail-soft: 失敗時はNone(呼び手はrec['sentiment_last_24h']へフォールバック)。"""
    import requests
    try:
        r = requests.get(url, timeout=timeout or 8)
        if r.status_code != 200:
            return None
        return _parse_public_json_csv(r.text)
    except Exception:
        return None


def price_sentiment_series_from_snapshots(snapshot_rows, price_daily, days=14,
                                          price_intraday=None):
    """直近 days **営業日**分の [{date, price_open, price_high, price_low, price_close,
    bull_ratio, bear_ratio}, ...] を組み立てる。純関数。

    ★2026-08-19: 日付の基準を「snapshots.jsonl の日次最終スナップショットの日付」
    (=土日祝も含む暦日。collect-onlyが24時間365日走るため)から「実際に価格barが
    存在する日(=取引所が開いていた営業日)」へ変更(ユーザー依頼「過去14日間は、
    非営業日はなしにしましょう」「営業日のみとして」)。これにより週末・祝日の
    "動きの無い横ばい足"がローソク足チャートに混ざらなくなる。price_daily に
    実在するbarの日付だけを対象にするため、_forward_fill_ohlc(前日終値の
    キャリーフォワード)はもう不要(=対象は常に実データがある日のみ)。

    price_daily は price_fetch.load_price(config.PRICE_DAILY_PATH) の戻り値をそのまま
    渡す想定(新規にCSV等を再パースしない・既存の日足取得関数を再利用)。センチメントは
    その営業日に一致するsnapshotがあればその値、無ければNone(捏造しない・値が
    無いことをそのまま示す)。個別投稿は一切参照しない。

    ★2026-08-19(別依頼): price_close 単独から OHLC 4フィールドへ拡張(ユーザー依頼
    「価格推移をローソク足にしてほしい」)。price_close は後方互換のため引き続き含める。

    ★2026-08-19(追加): ユーザー依頼「価格推移に出来高を足せますか」を受け
    price_volume(その日の出来高)も追加。

    ★2026-08-20追加(ユーザー指摘への対応): price_intraday(省略可)を渡すと、
    Yahoo日足(price_daily)に無い日(=日付切替直後の一時的なnull終値など)を
    自前の日中足集計(_daily_ohlc_from_intraday)で補完する。日足に既にある日は
    公式値を優先し上書きしない(補完はあくまでフォールバック)。price_intraday
    省略時は従来通り日足のみを使う(既存呼び出し元の動作を壊さない)。

    ★2026-08-20追加(ユーザー依頼「センチメント推移のグラフに、投稿量の棒グラフを
    足せますか」): post_count(その営業日の投稿数)も追加する。trend_14d_from_snapshots()
    と同じ抽出ロジック(signals.true_volume・無ければday_cumulativeへフォールバック)
    だが、trend_14dは暦日(collect-onlyが24時間365日走るため土日祝も含む)ベースで
    日付集合がこの関数(営業日のみ)と一致しないため、あえて重複実装しマージしない
    設計にする(=このpss自身の営業日集合に対して直接投稿数を引く)。
    """
    by_date = _last_snapshot_per_day(snapshot_rows)
    ohlc_by_date = _price_ohlc_by_date(price_daily)
    if price_intraday:
        intraday_by_date = _daily_ohlc_from_intraday(price_intraday)
        for d, ohlc in intraday_by_date.items():
            if d not in ohlc_by_date:
                ohlc_by_date[d] = ohlc
    out = []
    for d in sorted(ohlc_by_date)[-days:]:
        snap = by_date.get(d) or {}
        sig = snap.get("signals") or {}
        ohlc = ohlc_by_date[d]
        post_count = sig.get("true_volume")
        if post_count is None:
            post_count = snap.get("day_cumulative")
        out.append({
            "date": d,
            "price_open": ohlc["open"],
            "price_high": ohlc["high"],
            "price_low": ohlc["low"],
            "price_close": ohlc["close"],
            "price_volume": ohlc.get("volume"),
            "bull_ratio": sig.get("bull_ratio"),
            "bear_ratio": sig.get("bear_ratio"),
            "post_count": int(post_count) if post_count is not None else None,
        })
    return out


def intraday_today_series(snapshot_rows, price_intraday, today=None, adr_pts=None):
    """★2026-08-19追加(ユーザー依頼「当日の価格推移とセンチメント推移も入れる」)。
    本日分のイントラデイ推移を集計値のみで組み立てる純関数。個別投稿は一切参照しない。

    ★2026-08-20追加(ユーザー指摘「最新の株価が反映されていない」への対応・
    ユーザー指示「取得しているデータで更新しましょう」): adr_pts(省略可・
    price_fetch.load_price(config.ADR_PTS_PATH)の戻り値)を渡すと、本日分の
    Yahoo日中足に出来高>0のbarが1本も無い場合(_today_yahoo_has_volume()==False。
    寄り前特別気配、またはYahoo Finance chart API側の一時的なデータ不整合の
    いずれか)に限り、nikkei225jp.comのTSE現在値フィードから組み立てた本日分の
    10分足OHLC(_today_tse_ohlc_from_adr_pts()・volumeは無いフィードのため常にNone)
    へ丸ごと差し替える。Yahoo側に本日の実データがある通常時は従来どおり(挙動不変)。

    価格: price_intraday(price_fetch.load_price(config.PRICE_INTRADAY_PATH)の戻り値。
    5分足バー)から本日分のbarを抽出し、**10分足のOHLCへリサンプル**(ユーザー依頼
    「本日のは10分足、14日分は日足に」「価格推移をローソク足にしてほしい」)して
    {time(HH:MM), price_open, price_high, price_low, price_close}へ変換する。10分
    バケット(分を10で切り捨てた時刻)ごとに、open=バケット内で最初に観測されたbarの
    始値・high=バケット内の最高値・low=バケット内の最安値・close=バケット内で最後に
    観測されたbarの終値、という標準的なローソク足リサンプルの定義に従う。barのts は
    UNIX epoch秒のためmeta.gmtoffset(既定32400=JST+9h)を足してJSTローカル時刻へ変換する
    (dashboard.py._bars_to_dtと同じ変換方式)。

    センチメント: snapshot_rows(snapshots.jsonl)の本日分の行を{time, bull_ratio,
    bear_ratio}へ変換する。snapshots.jsonlは1run毎(catchup=10分毎/フル実行=毎時)に
    追記される時系列ロールアップで、各行の"signals"サブフィールド(=そのrunまでの
    累積センチメント比率)を使う(price_sentiment_series_from_snapshotsの日次版と
    同じ"signals"参照パターン)。

    戻り値: {"price": [{time, price_open, price_high, price_low, price_close,
    price_volume}, ...], "sentiment": [{time, bull_ratio, bear_ratio}, ...]}。
    データが無ければ両方とも空リスト(fail-soft・呼び手はキー自体の欠落は起きない)。

    ★2026-08-19(追加): ユーザー依頼「価格推移に出来高を足せますか」を受け
    price_volume(バケット内の5分足volumeの合計)も追加。1本のみのvolume=Noneな
    barはその分だけ加算をスキップする(欠損を0扱いで捏造しない)。
    """
    today = today or dt.date.today().isoformat()

    buckets = {}   # "HH:M0"(10分刻み) -> {open, high, low, close, volume, n}(バケット内で集計中)
    if price_intraday:
        bars = price_intraday.get("bars") or []
        meta = price_intraday.get("meta") or {}
        off = meta.get("gmtoffset") or 32400
        for b in bars:
            o, h, low, close = b.get("open"), b.get("high"), b.get("low"), b.get("close")
            vol = b.get("volume")
            ts = b.get("ts")
            if close is None or ts is None:
                continue
            d = dt.datetime.utcfromtimestamp(ts + off)
            if d.date().isoformat() != today:
                continue
            bucket_minute = (d.minute // 10) * 10
            key = d.replace(minute=bucket_minute, second=0, microsecond=0).strftime("%H:%M")
            # barは時系列順に並んでいる前提(price_fetch.py)。同じバケット内では
            # open=最初のbarのopenを保持・high/lowは全barの最大/最小へ更新・
            # close=最後に処理したbarの値で上書き・volume=バケット内の全bar合計、
            # という標準的なOHLCVリサンプルの定義に従う。
            if key not in buckets:
                buckets[key] = {"open": o if o is not None else close,
                                "high": h if h is not None else close,
                                "low": low if low is not None else close,
                                "close": close, "volume": vol or 0, "n": 1}
            else:
                cur = buckets[key]
                if h is not None:
                    cur["high"] = max(cur["high"], h)
                if low is not None:
                    cur["low"] = min(cur["low"], low)
                cur["close"] = close
                cur["volume"] += vol or 0
                cur["n"] += 1

    # ★2026-08-19(ユーザー指摘「引けの時の出来高は出ませんか」): price_fetch.pyが
    # 明記する通り、末尾バーはvolume=0の「現在値合成バー」(実取引のバーではなく
    # その時点の気配/終値だけを表す点)である場合がある。このbarがちょうど新しい
    # 10分境界(例:15:30:00)に乗ると、実取引ゼロの単独バーだけで新規バケットが
    # 作られ、「引けの足だけ出来高0」という誤解を招く見た目になる(直前の
    # 15:20-15:29台の実出来高は既にその手前のバケットへ正しく集計済みなのに、
    # 引け値を表示するためだけの空のローソクが最後に追加されて見える)。
    # 対策: 最後のバケットが「単独bar・出来高0」で、かつ直前バケットが存在する
    # 場合は、その終値/高値/安値だけを直前バケットへ吸収し(=引け値を正しく
    # 反映)、出来高ゼロの空バケット自体は作らない(出来高を捏造せず単に併合)。
    if buckets:
        keys_sorted = sorted(buckets)
        last_key = keys_sorted[-1]
        last = buckets[last_key]
        if last["n"] == 1 and last["volume"] == 0 and len(keys_sorted) >= 2:
            prev_key = keys_sorted[-2]
            prev = buckets[prev_key]
            prev["high"] = max(prev["high"], last["high"])
            prev["low"] = min(prev["low"], last["low"])
            prev["close"] = last["close"]
            del buckets[last_key]

    price_pts = [{"time": t, "price_open": buckets[t]["open"], "price_high": buckets[t]["high"],
                 "price_low": buckets[t]["low"], "price_close": buckets[t]["close"],
                 "price_volume": buckets[t]["volume"]}
                for t in sorted(buckets)]

    if adr_pts and not _today_yahoo_has_volume(price_intraday, today=today):
        price_pts = _today_tse_ohlc_from_adr_pts(adr_pts, today=today)

    # ★2026-08-20追加(ユーザー依頼「センチメント推移のグラフに、投稿量の棒グラフを
    # 足せますか」): signals.true_volume はその営業日でリセットされ単調増加する
    # 累積投稿数(実測確認済み: 前日23:57時点17650->当日00:02時点6)。todayで
    # フィルタ済みの行どうしの単純な差分だけで、日境界をまたがず安全に「直前の
    # スナップショットから何件増えたか」(=そのスナップショット区間の新規投稿数)
    # を求められる。先頭点は直前が無いのでtrue_volumeの値そのものを使う。
    sent_pts = []
    prev_true_volume = None
    for r in snapshot_rows or []:
        if (r or {}).get("date") != today:
            continue
        ts = r.get("timestamp") or ""
        if len(ts) < 16:
            continue
        sig = r.get("signals") or {}
        bull = sig.get("bull_ratio")
        bear = sig.get("bear_ratio")
        if bull is None and bear is None:
            continue
        tv = sig.get("true_volume")
        post_count = None
        if tv is not None:
            post_count = tv if prev_true_volume is None else max(0, tv - prev_true_volume)
            prev_true_volume = tv
        sent_pts.append({"time": ts[11:16], "bull_ratio": bull, "bear_ratio": bear,
                         "post_count": post_count})

    return {"price": price_pts, "sentiment": sent_pts}


def intraday_today_sentiment_10min(sent_pts):
    """★2026-08-21追加(ユーザー依頼「過去24時間センチメント推移を『本日の
    センチメント推移』に変更。本日の価格推移・板の買い・売り総計のグラフと
    横軸が合うように」)。

    ★2026-08-21同日中に非推奨化(ユーザー指摘「投稿量は取得時刻でなく投稿時刻で
    ならすことにしたはずです」): 本関数はsnapshots.jsonl(=巡回/取得実行のたびの
    時刻)基準でバケット化するため、収集側の一括バックログ取得(実例=本日12:37の
    Yahoo 19,801件一括取得)が起きると投稿量が1バケットへ跳ね上がる、
    2026-08-20に一度是正済みだったのと同種の不具合を再導入してしまっていた。
    「本日のセンチメント推移」チャートは現在sentiment_today_from_last_24h()
    (投稿自身のtsでバケット化済みのsentiment_last_24h_10min()から本日ぶんを
    抜き出すだけ)を使う設計へ切り替え済み。本関数自体は既存selftestの対象の
    ため削除しないが、新規の呼び出し元を増やさないこと。

    (以下は元の設計メモ・現在は上記の理由で非推奨)
    intraday_today_series()が返すsentiment(各snapshot実行
    時刻そのままの不揃いな間隔・catchup=10分毎/フル実行=毎時)を、price_ptsと
    同じ「10分刻み(分を10で切り捨て)」バケットへリサンプルする純関数。これにより
    x軸のカテゴリ(HH:MM)が価格チャートの_effective_today_buckets()と厳密に一致し、
    型が同じtype="category"グラフとして縦に並べた時に横軸が視覚的に揃う。

    ①bull_ratio/bear_ratio: 各snapshotは「そのrunまでの累積比率」という状態値
    (price_open/closeのような区間内平均ではない)のため、バケット内では時系列
    最後(最新)の値を採用する(=そのバケット終了時点の状態)。intraday_today_series
    の入力は既にsnapshot_rowsの出現順(時系列昇順)を保つ前提。
    ②post_count: 各snapshotの時点で「直前snapshotからの新規投稿数」という区間
    差分値のため、バケット内では単純合計する(Noneはこの合計では0扱い・ただし
    バケット内が全件Noneならバケット自体もNoneのまま=捏造しない)。
    ③時刻文字列が短い/不正な点はスキップする(fail-soft)。有効な点が無ければ
    空リストを返す。"""
    buckets = {}   # "HH:M0" -> {"bull": last, "bear": last, "post_sum": float, "post_seen": bool}
    for p in sent_pts or []:
        t = (p.get("time") or "").strip()
        if len(t) < 4:
            continue
        try:
            hh, mm = int(t[0:2]), int(t[3:5])
        except (TypeError, ValueError):
            continue
        key = f"{hh:02d}:{(mm // 10) * 10:02d}"
        b = buckets.setdefault(key, {"bull": None, "bear": None, "post_sum": 0.0, "post_seen": False})
        if p.get("bull_ratio") is not None:
            b["bull"] = p["bull_ratio"]
        if p.get("bear_ratio") is not None:
            b["bear"] = p["bear_ratio"]
        pc = p.get("post_count")
        if pc is not None:
            b["post_sum"] += pc
            b["post_seen"] = True

    return [
        {"time": k, "bull_ratio": buckets[k]["bull"], "bear_ratio": buckets[k]["bear"],
         "post_count": buckets[k]["post_sum"] if buckets[k]["post_seen"] else None}
        for k in sorted(buckets)
    ]


def sentiment_last_24h_10min(raw_rows, analyzed_rows, now=None):
    """★2026-08-20追加・同日中に再設計(ユーザー指示「本日のセンチメント推移は、
    過去24時間の10分毎のセンチメントの推移にしましょう」→ユーザー指摘「(収集が
    まとまった時の)突出した投稿量は、投稿時刻でばらけさせるべきでは」)。

    当初はsnapshots.jsonl(=収集/スナップショット実行のたびの巡回時刻)を基準に
    バケット化していたため、収集側にバックログが溜まって一度に大量取得した場合
    (実例2026-08-20: Yahoo!掲示板の未取得分19,801件を1回のrunでまとめて取得)、
    実際にはもっと広い時間帯にわたって投稿されていたはずの投稿が、収集が完了した
    1つの10分バケットへまるごと計上されてしまっていた(バグではなく設計上の限界)。

    raw_comments.jsonl/analyzed.jsonlの各行が持つ**実際の投稿時刻**("ts"。収集
    時刻を表す"fetched_at"/"analyzed_at"とは別フィールド・実データで確認済み)で
    バケット化することで、収集のタイミングに関わらず「実際に投稿された時刻」で
    グラフへ反映されるようにする。

    post_count: raw_rows(生投稿・meaningfulフィルタ前の全件)を各行のtsでバケット化
    した件数(signals.compute_signals()のtrue_volume="当日の生投稿総数(全件)"と
    同じ定義)。
    bull_ratio/bear_ratio: analyzed_rows のうち meaningful=True の行のみを対象に
    各行のtsでバケット化し、そのバケット内のbullish/bearish件数の比率
    (signals.py._meaningful()/_ratios()と同じmeaningfulのみを対象とする定義)。
    meaningful行が1件も無いバケットはNone(比率を捏造しない・fail-soft)。

    ★個別投稿情報の漏洩防止: raw_rows/analyzed_rowsの各行から使うのは
    "ts"(両方)・"meaningful"/"sentiment"(analyzed_rows側)のみ。text/author/id/
    user/votes/rationale等は一切読み取らず戻り値にも含めない(このモジュール全体の
    設計原則=集計値のみを外部へ渡す)。

    戻り値: [{time("M/D HH:MM"), bull_ratio, bear_ratio, post_count}, ...]
    (古い→新しい順・バケットの実時刻でソート=文字列キーの日跨ぎ衝突が起きない)。
    データが無ければ空リスト。
    """
    now = now or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9))
    window_start = now - dt.timedelta(hours=24)

    def _parse_ts(row):
        # raw_comments.jsonl/analyzed.jsonlのtsは"YYYY-MM-DDTHH:MM:SS"(ISO区切り・T)
        ts_str = (row or {}).get("ts") or ""
        try:
            return dt.datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    def _bucket_dt(ts):
        bucket_minute = (ts.minute // 10) * 10
        return ts.replace(minute=bucket_minute, second=0, microsecond=0)

    post_counts = {}
    for r in raw_rows or []:
        ts = _parse_ts(r)
        if ts is None or ts < window_start or ts > now:
            continue
        bkey = _bucket_dt(ts)
        post_counts[bkey] = post_counts.get(bkey, 0) + 1

    sentiment_counts = {}
    for r in analyzed_rows or []:
        if not (r or {}).get("meaningful"):
            continue
        ts = _parse_ts(r)
        if ts is None or ts < window_start or ts > now:
            continue
        bkey = _bucket_dt(ts)
        c = sentiment_counts.setdefault(bkey, {"bull": 0, "bear": 0, "total": 0})
        c["total"] += 1
        s = (r or {}).get("sentiment")
        if s == "bullish":
            c["bull"] += 1
        elif s == "bearish":
            c["bear"] += 1

    result = []
    for bkey in sorted(set(post_counts) | set(sentiment_counts)):
        c = sentiment_counts.get(bkey)
        bull_ratio = round(c["bull"] / c["total"], 3) if c and c["total"] > 0 else None
        bear_ratio = round(c["bear"] / c["total"], 3) if c and c["total"] > 0 else None
        result.append({
            "time": f"{bkey.month}/{bkey.day} {bkey.strftime('%H:%M')}",
            "bull_ratio": bull_ratio,
            "bear_ratio": bear_ratio,
            "post_count": post_counts.get(bkey, 0),
        })
    return result


def sentiment_today_from_last_24h(sent_pts_24h, today=None):
    """★2026-08-21追加(ユーザー指摘「投稿量は、取得時刻ではなく、投稿時刻で
    ならすことにしたはずです」)。「本日のセンチメント推移」チャートの初回実装
    (intraday_today_sentiment_10min())はsnapshots.jsonl(=巡回/取得実行のたびの
    時刻)基準でバケット化しており、これは2026-08-20に一度発見・是正済みだった
    「収集側にバックログが溜まって一度に大量取得すると、実際は広い時間帯に
    わたって投稿されたはずの分が1バケットへまるごと計上される」不具合
    (sentiment_last_24h_10min()のdocstring参照)を、自分が知らずに再導入して
    いた回帰バグだった。実測でも本日12:30に投稿量3,979の跳ね上がりとして再現
    していた(原因はYahoo収集が12:37に19,801件を一括取得したため)。

    正しい直し方は「投稿時刻(ts)でバケット化」であり、それは既に
    sentiment_last_24h_10min()が実装済み(raw_comments.jsonl/analyzed.jsonlの各行
    自身のtsでバケット化・過去24時間ぶん)。本関数はその戻り値(sent_pts_24h・
    "M/D HH:MM"形式)から本日ぶんだけを抜き出し、日付部分を落として"HH:MM"形式
    へ変換する純関数。新たに再集計は行わない(=snapshots.jsonl由来の
    intraday_today_sentiment_10min()は今後この用途では使わない・ただし既存
    selftestの対象になっているため関数自体は削除しない)。

    today(省略時はJST今日)のyyyy-mm-dd文字列から月/日を取り出し、
    sent_pts_24hの"time"先頭の"M/D"(ゼロ埋め無し・sentiment_last_24h_10min()の
    出力形式と同じ構築方法)と一致する行だけを残す。"""
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    y, m, d = today.split("-")
    prefix = f"{int(m)}/{int(d)} "
    out = []
    for p in sent_pts_24h or []:
        t = (p.get("time") or "")
        if not t.startswith(prefix):
            continue
        out.append({**p, "time": t[len(prefix):]})
    return out


def detect_series_outliers(pts, key, window=5, ratio=5.0, min_median=1.0):
    """★2026-08-21追加(ユーザー依頼「改善提案②=異常値の自動検出」・ユーザー
    指摘「データの異常性のチェックはしてないのですか」への恒久対応)。

    投稿量(post_count)や板の買い/売り総計のような「件数・数量」系の時系列
    (pts=[{"time":..., key: value}, ...])から、前後の値と比べて突出した点を
    検出する汎用の純関数。今回の一連の調査(板寄せ混入・収集の一括キャッチ
    アップ)はいずれも「都度ユーザーからの指摘を受けて手動でログを掘る」形に
    なっていたが、既知のパターンをコードで個別に塞ぐ(board_totals_60s_seriesの
    itayose検出等)だけでなく、**未知の異常も自動的に目立たせる**汎用の安全網
    として追加する。

    判定方法: 各点について、前後window点(自分を除く・末端は片側のみ)の
    中央値(median)を基準にし、値がmedianのratio倍を超えていれば外れ値と
    みなす。中央値方式は平均より少数の極端値に引きずられにくい(1点の異常が
    自分自身の判定基準を歪めない)。medianがmin_median未満(値がほぼ0近辺の
    閑散区間)の場合は分母が小さすぎて僅かな変動でも比率が跳ね上がり誤検知
    しやすいため判定を見送る(fail-soft・過検知しない)。

    ★値は一切書き換えない・除外もしない(検出のみ)。捏造しない設計原則どおり、
    データそのものは手を加えず、呼び手(ダッシュボード側)が「⚠️N点の異常値を
    自動検出」等の注記を添えて透明性を保ったまま表示する用途を想定する。

    戻り値: 外れ値と判定された点の"time"のリスト(時系列順)。"""
    values = [p.get(key) for p in (pts or [])]
    n = len(values)
    flagged = []
    for i in range(n):
        v = values[i]
        if v is None:
            continue
        neighbors = [values[j] for j in range(max(0, i - window), min(n, i + window + 1))
                    if j != i and values[j] is not None]
        if len(neighbors) < 3:
            continue
        neighbors_sorted = sorted(neighbors)
        median = neighbors_sorted[len(neighbors_sorted) // 2]
        if median < min_median:
            continue
        if v > ratio * median:
            flagged.append(pts[i].get("time"))
    return flagged


def previous_snapshot_for_ai_commentary(previous_record):
    """★2026-08-19追加(ユーザー依頼「AI考察は前回からの変化に対する考察も入れる」)。
    直前に書き出し済みの公開レコード(load_public_latest()の戻り値、つまり"今回の更新
    より前のlatest.json")から、AI考察プロンプトに渡す軽量な比較用スナップショットを
    抜き出す純関数。集計値のみ(price/bull_ratio/bear_ratio/post_count_today/
    generated_at)を対象とし、ai_commentary本文やintraday_today等の詳細は含めない
    (プロンプトを肥大化させないため・個別投稿は元々previous_recordにも含まれない)。
    previous_record が None/空なら None を返す(=まだ前回データが無い=初回生成扱い、
    呼び手はNoneならプロンプトに比較セクションを含めない)。"""
    if not previous_record:
        return None
    price = previous_record.get("price") or {}
    board = previous_record.get("board") or {}
    return {
        "generated_at": previous_record.get("generated_at"),
        "price_last": price.get("last"),
        "change_pct": price.get("change_pct"),
        "bull_ratio": board.get("bull_ratio"),
        "bear_ratio": board.get("bear_ratio"),
        "post_count_today": board.get("post_count_today"),
    }


def extended_hours_summary(adr_pts_data):
    """★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を分析
    するように。翌日の傾向につながる可能性がある」)。
    price_fetch.fetch_adr_pts_and_save() が保存したnikkei225jp.comフィード
    (load_price(config.ADR_PTS_PATH)の戻り値)から、直近のPTS(夜間取引)・米国ADR
    円換算の最新値サマリーを組み立てる純関数。個別投稿は一切参照しない。

    基準点(boundary)＝フィード内で「tse列(TSE正規セッション現在値)が非Noneの最後の
    行」＝その日のTSE最終気配(実質的な大引け値)。これより後のPTS/ADR観測値だけを
    「延長取引時間帯」とみなし、それぞれの直近値(取引が無ければNone=捏造しない)を
    抜き出す。change_pctはTSE最終値との比較(=「もし翌営業日にこの変化が反映され
    たら」という解釈で使える値)。

    戻り値: {tse_close:{price,time}, pts:{price,change_pct,time}|None,
    adr:{price_yen,price_usd,change_pct,time}|None} または、フィード自体が
    無ければ None(fail-soft)。
    """
    rows = (adr_pts_data or {}).get("rows") or []
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r.get("ts") or 0)
    tse_rows = [r for r in rows if r.get("tse") is not None]
    if not tse_rows:
        return None
    tse_close_row = tse_rows[-1]
    tse_close = tse_close_row.get("tse")
    boundary_ts = tse_close_row.get("ts") or 0
    after = [r for r in rows if (r.get("ts") or 0) > boundary_ts]

    def _fmt_time(ts):
        d = dt.datetime.utcfromtimestamp(ts) + dt.timedelta(hours=9)
        return d.strftime("%m/%d %H:%M")

    def _chg(v):
        if v is None or not tse_close:
            return None
        return round((v - tse_close) / tse_close * 100.0, 2)

    pts_rows = [r for r in after if r.get("pts") is not None]
    pts_summary = None
    if pts_rows:
        last = pts_rows[-1]
        pts_summary = {"price": last["pts"], "change_pct": _chg(last["pts"]),
                       "time": _fmt_time(last["ts"])}

    adr_rows = [r for r in after if r.get("adr_yen") is not None]
    adr_summary = None
    if adr_rows:
        last = adr_rows[-1]
        adr_summary = {"price_yen": last["adr_yen"], "price_usd": last.get("adr_usd"),
                       "change_pct": _chg(last["adr_yen"]), "time": _fmt_time(last["ts"])}

    return {
        "tse_close": {"price": tse_close, "time": _fmt_time(boundary_ts)},
        "pts": pts_summary,
        "adr": adr_summary,
    }


# ============================================================================
# 純関数: 公開レコード組み立て(既存の集計済み結果だけを受け取る=生コメント非依存)
# ============================================================================
def build_public_record(S, price_d, trend_14d, *, symbol=None, company_name=None,
                        generated_at=None, price_sentiment_series=None,
                        ai_commentary=None, regime=None, intraday_today=None,
                        previous=None, extended_hours=None,
                        board_history_14d=None, signal_changes=None,
                        sentiment_last_24h=None, signal_cards_history_14d=None):
    """
    既存の集計結果から公開用レコードを組み立てる純関数。個別投稿情報は一切参照しない
    (引数として生コメントのリストを受け取らない設計=構造的に混入を防ぐ)。

    引数:
      S          - signals.compute_signals() の戻り値(dict)。true_volume/ratios/gauges/
                   posts_per_hour/price/cards(9シグナルカード)等の集計済みフィールド
                   だけを参照する(S['named']['top_authors'] のような個別投稿寄りの
                   フィールドは意図的に一切参照しない)。
      price_d    - price_fetch.load_price() の戻り値(dict|None)。S['price']['last'] が
                   Noneの時のフォールバックにのみ使う(meta.regularMarketPrice)。
      trend_14d  - trend_14d_from_snapshots() 等が返す集計済みの日次リスト。
      price_sentiment_series - price_sentiment_series_from_snapshots() 等が返す
                   集計済みの [{date, price_close, bull_ratio, bear_ratio}, ...]。
                   省略時は空リスト(既存呼び出し元の動作を壊さない)。
      ai_commentary - {"text": ..., "generated_at": ...} の dict、または None。
                   None(既定)なら出力レコードにキー自体を含めない(既存動作を壊さない)。
                   生成は public_insight.generate_public_insight() が担い、このモジュール
                   はできあがった dict を差し込むだけ(ここではLLMを呼ばない)。
      regime     - ★2026-08-19追加(おにや09:00投稿・公開ダッシュボード用)。
                   {"vol_regime": ..., "vol_regime_score": ..., "calibration_status": ...}
                   のdict、または None。export_signal.py(内部トレーディングシグナル・
                   signal_export/latest.json)が既に計算済みの較正状態を読み取り専用で
                   渡す想定(このモジュール自身は再計算しない・個別投稿は一切不参照)。
                   None(既定)なら出力レコードに regime キー自体を含めない。
      intraday_today - ★2026-08-19追加(ユーザー依頼「当日の価格推移とセンチメント推移も
                   入れる」)。intraday_today_series() が返す
                   {"price": [...], "sentiment": [...]} の dict、または None。
                   None(既定)なら出力レコードにキー自体を含めない(既存動作を壊さない)。
      previous   - ★2026-08-19追加(ユーザー依頼「AI考察は前回からの変化に対する考察も
                   入れる」)。previous_snapshot_for_ai_commentary() が返す軽量な比較
                   スナップショット、または None。None(既定)なら出力レコードにキー
                   自体を含めない(既存動作を壊さない・初回生成時など前回データが
                   無い場合の想定挙動)。
      extended_hours - ★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯も
                   それらの値を分析するように」)。extended_hours_summary() が返す
                   {tse_close, pts, adr} のdict、または None。None(既定)なら出力
                   レコードにキー自体を含めない(既存動作を壊さない・フィード取得
                   失敗時等の想定挙動)。
      board_history_14d - ★2026-08-20追加(ユーザー提案「メーターに推移スパークラインを」)。
                   board_score_daily_series() が返す
                   [{date, overheat_score, capitulation_score}, ...] のリスト、または
                   None。None(既定)なら出力レコードにキー自体を含めない。
      signal_changes - ★2026-08-20追加(ユーザー提案「9指標の状態変化が分かるように」)。
                   signal_state_changes() が返す [{name, from, to, compared_date}, ...]
                   のリスト、または None。None(既定)なら出力レコードにキー自体を
                   含めない。出力レコード上のキー名は"signal_state_changes"。
      sentiment_last_24h - ★2026-08-20追加(ユーザー指示「本日のセンチメント推移は、
                   過去24時間の10分毎のセンチメントの推移に」)。
                   sentiment_last_24h_10min() が返す [{time, bull_ratio, bear_ratio,
                   post_count}, ...] のリスト、または None。None(既定)なら出力
                   レコードにキー自体を含めない。
      signal_cards_history_14d - ★2026-08-25追加(ユーザー指摘「公開用ダッシュボード
                   (streamlit版)は直ってないのでは」。当初は画像版のみにシグナル
                   推移スパークラインを実装していたが、streamlit版にも同じ推移を
                   出すため追加)。signal_cards_daily_series() が返す
                   [{date, <指標名>: value, ...}, ...] のリスト、または None。
                   None(既定)なら出力レコードにキー自体を含めない。
    """
    S = S or {}
    ratios = S.get("ratios") or {}
    gauges = S.get("gauges") or {}
    price_info = S.get("price") or {}

    price_last = price_info.get("last")
    price_change = price_info.get("change_pct")
    if price_last is None and price_d:
        meta = (price_d or {}).get("meta") or {}
        price_last = meta.get("regularMarketPrice")

    rec = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol or config.SYMBOL,
        "company_name": company_name or COMPANY_NAME,
        "generated_at": generated_at or dt.datetime.now().isoformat(timespec="seconds"),
        "price": {
            "last": price_last,
            "change_pct": price_change,
        },
        "board": {
            "post_count_today": S.get("true_volume", 0),
            "posts_per_hour": S.get("posts_per_hour"),
            "bull_ratio": ratios.get("bull_ratio"),
            "bear_ratio": ratios.get("bear_ratio"),
            "neutral_ratio": ratios.get("neutral_ratio"),
            "overheat_score": gauges.get("overheat"),
            "capitulation_score": gauges.get("capitulation"),
        },
        # ★2026-08-19追加: 9シグナルカード(灼熱/そう思う票/イナゴ語彙/ネームド集中/
        # 他銘柄混入/暴落煽り/阿鼻叫喚/話題枯れ/投稿サージ)。各カードは
        # {name, value, threshold, state, note} の集計値のみ(signals.build_signal_cards
        # 参照・個別投稿は一切含まない)。公開ダッシュボードのシグナル一覧に使う。
        "signal_cards": list(S.get("cards") or []),
        "trend_14d": list(trend_14d or []),
        "price_sentiment_series": list(price_sentiment_series or []),
        "disclaimer": DISCLAIMER,
    }
    if ai_commentary is not None:
        rec["ai_commentary"] = ai_commentary
    if regime is not None:
        rec["regime"] = {
            "vol_regime": regime.get("vol_regime"),
            "vol_regime_score": regime.get("vol_regime_score"),
            "calibration_status": regime.get("calibration_status"),
        }
    if intraday_today is not None:
        rec["intraday_today"] = {
            "price": list(intraday_today.get("price") or []),
            "sentiment": list(intraday_today.get("sentiment") or []),
        }
    if previous is not None:
        rec["previous"] = {
            "generated_at": previous.get("generated_at"),
            "price_last": previous.get("price_last"),
            "change_pct": previous.get("change_pct"),
            "bull_ratio": previous.get("bull_ratio"),
            "bear_ratio": previous.get("bear_ratio"),
            "post_count_today": previous.get("post_count_today"),
        }
    if extended_hours is not None:
        pts = extended_hours.get("pts")
        adr = extended_hours.get("adr")
        tse_close = extended_hours.get("tse_close") or {}
        rec["extended_hours"] = {
            "tse_close": {"price": tse_close.get("price"), "time": tse_close.get("time")},
            "pts": ({"price": pts.get("price"), "change_pct": pts.get("change_pct"),
                    "time": pts.get("time")} if pts else None),
            "adr": ({"price_yen": adr.get("price_yen"), "price_usd": adr.get("price_usd"),
                    "change_pct": adr.get("change_pct"), "time": adr.get("time")}
                   if adr else None),
        }
    if board_history_14d is not None:
        rec["board_history_14d"] = list(board_history_14d)
    if signal_changes is not None:
        rec["signal_state_changes"] = list(signal_changes)
    if sentiment_last_24h is not None:
        rec["sentiment_last_24h"] = list(sentiment_last_24h)
    if signal_cards_history_14d is not None:
        rec["signal_cards_history_14d"] = list(signal_cards_history_14d)
    return rec


# ============================================================================
# 漏洩検証(最重要): 個別投稿由来のキーが混入していないか再帰的に検証
# ============================================================================
def validate_no_leak(rec):
    """
    レコード(dict/list/スカラーのネスト構造)を再帰的に走査し、個別投稿由来と
    疑われるキー名(text/user/author/id等・大小無視の完全一致)が含まれていないか
    検証する。エラー文字列のリストを返す(空=OK)。

    キー名の完全一致で判定するため、post_count_today のような正規の集計キーを
    誤検知しない。値の中身までは判定しない(このモジュールの出力は数値/日付文字列/
    定型文だけを想定しており、値ベースの自由記述テキスト検査は対象外)。

    唯一の例外: パスが厳密に "$.ai_commentary.text" かつ値が文字列の場合だけ、
    キー名"text"での検出をスキップする(_LEAK_KEY_EXEMPT_EXACT_PATHS参照。
    public_insight.generate_public_insight() が集計値のみから生成する公開用考察文)。
    """
    errs = []

    def _walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).strip().lower()
                full_path = f"{path}.{k}"
                exempt = (full_path in _LEAK_KEY_EXEMPT_EXACT_PATHS
                         and isinstance(v, str))
                if kl in _LEAK_KEY_HINTS and not exempt:
                    errs.append(f"leaked key '{k}' at {full_path}")
                _walk(v, full_path)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")

    _walk(rec, "$")
    return errs


# ============================================================================
# 書き込み(export_signal.py と同型のI/Oパターン・完全に独立したパス/スキーマ)
# ============================================================================
def _atomic_write_json(path, obj):
    """temp へ書いて os.replace(WinError5安全)。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] public_export: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_public_export(S, price_d, trend_14d, **kw):
    """
    build_public_record() でレコードを作り、validate_no_leak() を通らなければ
    書き込みを中止して例外を送出する(fail-closed)。通れば history.jsonl に
    append(退避主義) + latest.json を atomic 置換。戻り値: 書いたレコード。
    """
    config.ensure_public_export_dir()
    rec = build_public_record(S, price_d, trend_14d, **kw)
    errs = validate_no_leak(rec)
    if errs:
        _log(f"ERROR individual-post leak detected, write ABORTED: {errs}")
        raise ValueError(
            f"public_export: individual-post leak detected, write aborted: {errs}")
    _append_jsonl(config.PUBLIC_EXPORT_HISTORY_PATH, rec)      # append-only
    _atomic_write_json(config.PUBLIC_EXPORT_LATEST_PATH, rec)  # atomic
    _log(f"export post_count_today={rec['board']['post_count_today']} "
         f"bear_ratio={rec['board']['bear_ratio']} bull_ratio={rec['board']['bull_ratio']} "
         f"trend_days={len(rec['trend_14d'])} "
         f"pss_days={len(rec.get('price_sentiment_series') or [])} "
         f"has_commentary={'ai_commentary' in rec}")
    return rec


def load_public_latest():
    """消費側/動作確認用: latest.json を読む。無ければ None。"""
    p = config.PUBLIC_EXPORT_LATEST_PATH
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parse_public_json_csv(csv_text):
    """★2026-08-19追加。Google Sheetsの「ウェブに公開」CSV書き出し(json_blobタブ・
    A1セルにlatest.json全体のJSON文字列が入っている)をパースする純関数
    (ネット非依存・load_public_latest_from_url()から分離してselftest対象にする
    =price_fetch.parse_chart_json/_fetchと同じ「純関数とネットワークI/Oの分離」
    パターン)。

    「ウェブに公開」のCSVはRFC4180準拠(セル内の改行/カンマ/引用符は正しく
    エスケープされる)のため、素朴な文字列分割ではなくPython標準csvモジュールで
    パースする(JSON文字列自体にカンマ・引用符が大量に含まれるため必須)。
    A1セル(1行目1列目)の値をそのままjson.loads()する。失敗時はNone(fail-soft)。
    """
    import csv
    import io
    try:
        reader = csv.reader(io.StringIO(csv_text or ""))
        first_row = next(reader, None)
        if not first_row or not first_row[0]:
            return None
        return json.loads(first_row[0])
    except Exception:
        return None


def load_public_latest_from_url(url, timeout=None):
    """★2026-08-19追加(ユーザー依頼: 公開ダッシュボードをStreamlit Community Cloud
    へデプロイするため)。クラウド環境からはローカルPCのdata/public_export/latest.json
    へ直接アクセスできない。橋渡し役として、public_sheets_sync.py(★同日追加の
    json_blobタブ)が毎run latest.jsonの全内容をGoogle Sheetsの1セルへJSON文字列と
    して書き込み、そのシートを「ウェブに公開」したCSV書き出しURL(gid付きの
    `.../export?format=csv&gid=<json_blobタブのgid>` 形式)を渡す想定。
    実際のCSVパースは_parse_public_json_csv()(純関数)に委譲する。

    fail-soft: ネットワーク失敗・空応答・不正なJSON等いずれも例外を投げずNoneを
    返す(呼び手[public_dashboard.py]は「データ蓄積中です」を表示するだけで
    アプリ自体はクラッシュしない)。ローカルのlatest.json同様、読み取り専用
    (このURLへの書き込みは一切行わない)。
    """
    import requests
    try:
        r = requests.get(url, timeout=timeout or 15)
        if r.status_code != 200:
            return None
        return _parse_public_json_csv(r.text)
    except Exception:
        return None


def _parse_visit_counter_response(text):
    """★2026-08-20追加(ユーザー依頼: 公開サイトの閲覧者数を集計して表示)。
    閲覧数カウンター用Google Apps Script Web App(doGet)の応答本文
    (JSON文字列 例 '{"count": 42}')をパースする純関数(ネット非依存・
    record_visit()から分離してselftest対象にする=_parse_public_json_csvと
    同じ「純関数とネットワークI/Oの分離」パターン)。不正/空応答はNone(fail-soft)。
    """
    try:
        data = json.loads(text or "")
        count = data.get("count")
        return int(count) if count is not None else None
    except Exception:
        return None


def record_visit(url, action="hit", timeout=None):
    """★2026-08-20追加(ユーザー依頼: 公開サイトの閲覧者数を集計して表示)。
    Google Sheetsを裏側の永続化先とする閲覧数カウンター(Google Apps Script
    Web App・doGet)へ問い合わせる。新しい第三者サービスへは接続せず、既に
    承認済みのGoogle Sheets連携基盤(public_sheets_sync.py)を流用する設計。
    action="hit" は1加算してから現在値を返す、action="read" は加算せず現在値
    のみ返す(public_dashboard.py側で1ブラウザセッションにつき1回だけ"hit"を
    呼び、以降の自動更新では"read"のみにしてページ再読込回数の水増しを防ぐ)。

    fail-soft: URL未設定・通信失敗・不正応答いずれも例外を投げずNoneを返す
    (呼び手はバッジを表示しないだけでアプリ自体はクラッシュしない)。
    """
    if not url:
        return None
    import requests
    try:
        r = requests.get(url, params={"action": action}, timeout=timeout or 8)
        if r.status_code != 200:
            return None
        return _parse_visit_counter_response(r.text)
    except Exception:
        return None


def load_public_history():
    """history.jsonl を list で。無ければ []。"""
    p = config.PUBLIC_EXPORT_HISTORY_PATH
    rows = []
    if not os.path.exists(p):
        return rows
    # ★2026-08-19修正(おにや22:13投稿・重大障害調査の横展開): torn write対策
    # (バイナリモード+行ごと個別decode)。1行分のバイト破損で以降の行が全滅しない。
    try:
        with open(p, "rb") as f:
            for raw_line in f:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return rows


# ============================================================================
# AI考察(ai_commentary)の1日1回自動生成ゲート(config.PUBLIC_EXPORT_COMMENTARY_DAILY)
# ============================================================================
def _commentary_already_generated_today(latest_record, history_rows, today):
    """純関数(I/O非依存): 今日(today, 'YYYY-MM-DD' JST日付文字列)分の ai_commentary が
    既に生成済みかを判定する。

    優先順位:
      1) latest_record(latest.json 相当)の ai_commentary.generated_at の日付。
      2) 1) に無ければ history_rows(history.jsonl 相当・古い→新しい順を想定)を新しい順に
         走査し、最初に見つかった ai_commentary.generated_at の日付。

    latest.json は毎run(commentaryの有無に関わらず)atomic上書きされるため、commentary
    無しのrunが後から来ると latest.json の ai_commentary は消える(キー自体が無くなる)。
    そのため 1) だけでは「今日は既に生成済み」を見失う ―― history.jsonl は append-only
    で過去のcommentary付きレコードを保持しているので 2) がフォールバックとして機能する。

    どちらにも今日分が見つからなければ False(=未生成・生成対象)。
    """
    ac = (latest_record or {}).get("ai_commentary") or {}
    gen_at = ac.get("generated_at")
    if gen_at:
        return str(gen_at)[:10] == today

    for row in reversed(list(history_rows or [])):
        ac2 = (row or {}).get("ai_commentary") or {}
        gen_at2 = ac2.get("generated_at")
        if gen_at2:
            return str(gen_at2)[:10] == today
    return False


def should_generate_commentary_today(today=None):
    """I/O込み: load_public_latest() / load_public_history() を読み、JST当日分の
    ai_commentary がまだ生成されていなければ True(=このrunで with_commentary=True に
    すべき)を返す。既に今日分が生成済みなら False。

    today を省略した場合は dt.date.today()(このプロジェクトの他モジュール[afterhours_bearish
    等]と同じく、壁時計=JSTローカル実行前提)。呼び手(run_once.py)は
    config.PUBLIC_EXPORT_COMMENTARY_DAILY が True の時だけこの関数を呼ぶ想定。
    """
    today = today or dt.date.today().isoformat()
    latest = load_public_latest()
    history = load_public_history()
    return not _commentary_already_generated_today(latest, history, today)


# ============================================================================
# CLI: 実データ(読み取り専用)から1回分の公開レコードを生成する最小エントリ
# ============================================================================
def _build_from_live_data(with_commentary=False):
    """実データ(read-only)から1回分の公開レコードを組み立てて書き出す。CLI用。
    raw_comments/analyzed/snapshots/price の各ファイルは jsonl_window / price_fetch
    経由で読むだけ(書込み・削除・切り詰め一切なし)。

    with_commentary=True の時だけ、①で validate_no_leak() を通過済みの公開レコードを
    public_insight.generate_public_insight() に渡して ai_commentary を追加生成する
    (有料API・明示フラグ経由のみ=既定は生成しない=課金なし)。生成失敗時は
    generate_public_insight() 自体がfail-soft(None)なので、ai_commentary無しで続行する。
    """
    import jsonl_window
    import signals as sigmod
    import price_fetch

    analyzed = jsonl_window.read_jsonl_recent(config.ANALYZED_PATH,
                                              days=TREND_READ_WINDOW_DAYS)
    raw = jsonl_window.read_jsonl_recent(config.RAW_COMMENTS_PATH,
                                         days=TREND_READ_WINDOW_DAYS)
    price_daily = price_fetch.load_price(config.PRICE_DAILY_PATH)
    price_intraday = price_fetch.load_price(config.PRICE_INTRADAY_PATH)

    S = sigmod.compute_signals(analyzed, raw_rows=raw,
                               price_daily=price_daily, price_intraday=price_intraday)

    # ★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を分析
    # するように」)。price_fetch.fetch_adr_pts_and_save()が別stepで保存済みの
    # フィードを読み取り専用で読む(このモジュール自身はネット非依存を維持)。
    adr_pts_data = price_fetch.load_price(config.ADR_PTS_PATH)

    # ★2026-08-20追加(ユーザー指摘「最新の株価が反映されていない」への対応・ユーザー
    # 指示「取得しているデータで更新しましょう」): 実測で確認済みの障害(Yahoo Finance
    # chart APIが本日分の出来高>0のbarを一切返さなくなる=寄り前特別気配、または
    # API側の一時的なデータ不整合)が起きている間だけ、公開ダッシュボードの
    # ヘッダー現在値を、Yahooとは完全独立なnikkei225jp.comのTSE現在値フィード
    # (adr_pts)へ差し替える。9シグナルの発火判定(gauges/cards)はcompute_signals()
    # 内で既に確定済みの値をそのまま使う(この上書きはS['price']の表示専用フィールド
    # だけを対象とし、他のS要素には一切波及しない=意図的に影響範囲を絞った設計)。
    if not _today_yahoo_has_volume(price_intraday):
        fallback = _latest_tse_price_from_adr_pts(adr_pts_data)
        if fallback:
            prev_close = _previous_close_price(price_daily)
            chg = (round((fallback["price"] - prev_close) / prev_close * 100.0, 2)
                  if prev_close else None)
            S = dict(S)
            S["price"] = {"last": fallback["price"], "change_pct": chg}
            _log(f"price fallback: Yahoo intraday has no volume today, "
                f"using adr_pts tse={fallback['price']} (prev_close={prev_close}, chg={chg})")

    snaps = jsonl_window.read_jsonl_recent(config.SNAPSHOTS_PATH,
                                           days=TREND_READ_WINDOW_DAYS)
    trend = trend_14d_from_snapshots(snaps)
    # ★2026-08-20: price_intradayを渡し、Yahoo日足に一時的に無い日(日付切替直後の
    # null終値等)を自前の日中足集計で補完できるようにする(_price_ohlc_by_dateの
    # docstring参照)。
    pss = price_sentiment_series_from_snapshots(snaps, price_daily,
                                                price_intraday=price_intraday)
    regime = _load_regime_readonly()
    # ★2026-08-20: adr_ptsを渡し、本日のYahoo日中足が使えない間は「本日の推移」も
    # 同じフォールバック(_today_tse_ohlc_from_adr_pts)で埋める(intraday_today_series
    # のdocstring参照)。
    intraday_today = intraday_today_series(snaps, price_intraday, adr_pts=adr_pts_data)
    extended_hours = extended_hours_summary(adr_pts_data)
    # ★2026-08-19追加(ユーザー依頼「AI考察は前回からの変化に対する考察も入れる」)。
    # 今回の書き出しで latest.json が上書きされる"前"の状態を読んでおく(=前回分の
    # 公開レコード)。読み取り専用(load_public_latest())・今回のrec組み立てより前に
    # 呼ぶ必要がある(write_public_export()が実行されるとlatest.jsonは今回の内容に
    # なってしまうため)。
    previous = previous_snapshot_for_ai_commentary(load_public_latest())

    # ★2026-08-20追加(ユーザー提案「灼熱/阿鼻叫喚メーターに推移スパークラインを」
    # 「9指標の状態変化が分かるように」)。history.jsonl(今回の書き出し"前"の状態・
    # まだ今回ぶんは含まれない)から過去日ぶんの日別最終値を読み、今回計算済みの
    # 現在値(S['gauges']/S['cards'])を1点追加して公開レコードへ埋め込む(=cloud側の
    # ダッシュボードは新たなSheets同期を増やさず、既存のjson_blob同期だけで
    # このデータも受け取れる)。
    history_rows = load_public_history()
    today_jst = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    gauges = S.get("gauges") or {}
    board_history_14d = board_score_daily_series(history_rows) + [{
        "date": today_jst, "overheat_score": gauges.get("overheat"),
        "capitulation_score": gauges.get("capitulation"),
    }]
    signal_changes = signal_state_changes(S.get("cards") or [], history_rows)
    # ★2026-08-25追加(ユーザー指摘「公開用ダッシュボード(streamlit版)は直ってないのでは」)。
    # board_history_14dと全く同じ組み立て順序(過去日ぶんはhistory.jsonlから・今日ぶんは
    # 今回計算済みの現在値S['cards']を1点追加)。
    today_signal_entry = {"date": today_jst}
    for _c in (S.get("cards") or []):
        _name = (_c or {}).get("name")
        if _name:
            today_signal_entry[_name] = _c.get("value")
    signal_cards_history_14d = signal_cards_daily_series(history_rows) + [today_signal_entry]

    # ★2026-08-20追加・同日中に再設計(ユーザー指示「本日のセンチメント推移は、過去
    # 24時間の10分毎のセンチメントの推移に」→ユーザー指摘「投稿量は投稿時刻で
    # ばらけさせるべきでは」)。既に読み込み済みのraw/analyzed(各行の実投稿時刻"ts"
    # を持つ)から算出(snapshots.jsonlベースだと収集タイミングに投稿が偏って
    # 見える問題があった。sentiment_last_24h_10min()のdocstring参照)。
    sentiment_last_24h = sentiment_last_24h_10min(raw, analyzed)

    ai_commentary = None
    if with_commentary:
        # まず①(price_sentiment_series 込み)の公開レコードを組み立て、
        # validate_no_leak() を通過したものだけを public_insight へ渡す
        # (=個別投稿を一切受け取れない関数へは、検証済みの集計dictしか渡らない)。
        prelim = build_public_record(S, price_intraday, trend, price_sentiment_series=pss,
                                     regime=regime, intraday_today=intraday_today,
                                     previous=previous, extended_hours=extended_hours,
                                     board_history_14d=board_history_14d,
                                     signal_changes=signal_changes,
                                     sentiment_last_24h=sentiment_last_24h,
                                     signal_cards_history_14d=signal_cards_history_14d)
        errs = validate_no_leak(prelim)
        if errs:
            _log(f"ERROR leak detected before commentary generation, skip: {errs}")
        else:
            import public_insight
            result = public_insight.generate_public_insight(prelim)
            if result:
                ai_commentary = {
                    "text": result.get("text"),
                    "generated_at": result.get("generated_at"),
                }
                _log(f"ai_commentary generated chars={len(result.get('text') or '')}")
            else:
                _log("WARN ai_commentary generation failed (fail-soft None); "
                     "exporting without ai_commentary")

    return write_public_export(S, price_intraday, trend, price_sentiment_series=pss,
                               ai_commentary=ai_commentary, regime=regime,
                               intraday_today=intraday_today, previous=previous,
                               extended_hours=extended_hours,
                               board_history_14d=board_history_14d,
                               signal_changes=signal_changes,
                               sentiment_last_24h=sentiment_last_24h,
                               signal_cards_history_14d=signal_cards_history_14d)


def _load_regime_readonly():
    """★2026-08-19追加(おにや09:00投稿・公開ダッシュボードのボラ・レジーム帯用)。
    export_signal.py(内部トレーディングシグナル)が既に書き出し済みの
    signal_export/latest.json を**読み取り専用**で開き、vol_regime/vol_regime_score/
    calibration_status の3フィールドだけを取り出す(それ以外のフィールド
    [direction_candidate/features/thresholds_crossed等]は公開スコープ外のため
    意図的に無視・出力レコードへ混入させない)。ファイルが無い/壊れている/
    このrunでまだ研究層が走っていない等でも fail-soft で None を返す
    (呼び手はNoneならrec['regime']キー自体を出力しないだけで、他の処理は続行する)。"""
    try:
        if not os.path.exists(config.SIGNAL_LATEST_PATH):
            return None
        with open(config.SIGNAL_LATEST_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {
            "vol_regime": d.get("vol_regime"),
            "vol_regime_score": d.get("vol_regime_score"),
            "calibration_status": d.get("calibration_status"),
        }
    except Exception:
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="public_export.py - 公開用集計値エクスポート(Phase 1・手動実行)")
    parser.add_argument("--selftest", action="store_true",
                        help="selftest.py 全体を実行(このモジュール専用テスト込み)")
    parser.add_argument("--with-commentary", action="store_true",
                        help="public_insight.generate_public_insight() でAI考察を生成し"
                             "ai_commentaryフィールドに含める(有料API課金・明示フラグ時のみ)")
    args = parser.parse_args()

    if args.selftest:
        import selftest
        return selftest.main()

    rec = _build_from_live_data(with_commentary=args.with_commentary)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
