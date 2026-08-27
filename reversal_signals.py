# -*- coding: utf-8 -*-
"""
reversal_signals.py - デイトレの「折り返し極値」自動検出(板総計×RSI6/MACD×センチメント)。

背景(2026-08-27 15:39 おにや自己申告・CROSS_PROJECT_LOG参照): 本日
①09:06-09:07 高値53,450円(RSI6=94・超大口利確売り集中)②10:12-10:15 安値50,590円
(RSI6=22-32)③13:00頃 押し目52,090-52,100円 の3件の「折り返し極値」を実際には
分析できていたにも関わらず、トレPJへのハンドオフ(連携ログ投稿)を都度実行できて
いなかった。本モジュールはこの検出〜ハンドオフの流れを機械化し、抜けを構造的に防ぐ。

【設計(初期案・後で調整可能)】
  高値反転候補(high_reversal) = 板の買い/売り総計比(buy_total/sell_total)が
    本日ローリング極値(直近N分間の最小値)を更新 かつ RSI6(1分足)が過熱
    (>=REVERSAL_RSI_OVERBOUGHT)。売り圧力が急増しRSIも過熱=高値圏での反転候補。
  安値反転候補(low_reversal)  = 同ローリング極値が最大値を更新 かつ RSI6が
    売られ過ぎ(<=REVERSAL_RSI_OVERSOLD)。買い/支持が急増しRSIも売られ過ぎ=
    安値圏での反転候補。
  センチメント(強気/弱気比率)は参考情報として付記するのみ(必須条件にはしない・
  方向の整合性チェック用)。

【データ源(全て read-only)】
  - 板総計: 株取引API_プロト1の board_285A_YYYY-MM-DD.csv を
    public_export.board_totals_60s_series() でそのまま集計(既存の買い/売り総計
    計算式=buy1..10qty+under_buy_qty+market_buy_qty 等を board_totals_bridge.py と
    完全に同じ経路で再利用。式を重複実装しない)。
  - テクニカル(RSI6/MACD): 株取引API_プロト1の ticks_285A_YYYY-MM-DD.csv から
    1分足終値を作りRSI6・MACDヒストグラムを計算(本モジュール新規実装。
    既存コードに類似の指標計算が無いため標準的な計算式=Wilder法RSI・
    12/26/9 EMA MACDで実装)。
  - センチメント: signals.sentiment_ratios() をそのまま使用。

【連携ログへの投稿】
  REVERSAL_LOG_POST_ENABLE(既定OFF)の間は post_to_cross_project_log() が
  dry_run=True 相当で動き、ファイルへは一切書き込まずエントリ文字列を返すのみ。
  '1' に切り替えるまで本番ログは変更されない(制約「いきなり本番ログに書き込まない」
  をコードで担保)。

規律(既存モジュールと同型):
  - 読み取り専用。record_all.py/signals.py本体・board_read.py・既存の凍結台帳
    (forward_sentiment_285A.csv等)には一切書き込まない/変更しない。
  - 全シグナルは研究用・未検証(signals.py冒頭の免責と同型)。
  - datetime.now()は呼び出し側(orchestrator)でのみ扱い、純粋な計算関数は引数で
    時刻/データを受け取る(テスト決定性・board_read.pyと同じ規律)。
"""
import os
import csv
import json
import datetime as dt

import config
import public_export
import signals as sigmod
import jsonl_window


WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _now_jst():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)


def _today_str(now=None):
    now = now or _now_jst()
    return now.strftime("%Y-%m-%d")


def _log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] reversal_signals: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# データ読み込み(read-only・fail-soft。プロト1のファイル・コードには一切書かない)
# ---------------------------------------------------------------------------
def _read_csv_rows(path):
    try:
        if not path or not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def read_today_board_rows(sym=None, date_iso=None, base_dir=None):
    """本日分の板CSV(board_<sym>_<date>.csv)を読む。無ければ[]。"""
    sym = sym or config.SYMBOL
    date_iso = date_iso or _today_str()
    base_dir = base_dir or config.KABU_PROTO1_285A_BOARD_DIR
    path = os.path.join(base_dir, f"board_{sym}_{date_iso}.csv")
    return _read_csv_rows(path)


def read_today_tick_rows(sym=None, date_iso=None, base_dir=None):
    """本日分のティックCSV(ticks_<sym>_<date>.csv)を読む。無ければ[]。"""
    sym = sym or config.SYMBOL
    date_iso = date_iso or _today_str()
    base_dir = base_dir or config.KABU_PROTO1_285A_TICKS_DIR
    path = os.path.join(base_dir, f"ticks_{sym}_{date_iso}.csv")
    return _read_csv_rows(path)


def read_today_analyzed_rows(date_iso=None, now=None):
    """本日分のanalyzed行(analyzed.jsonl)をjsonl_window経由で読む(全件読込を避ける)。"""
    date_iso = date_iso or _today_str(now)
    now = now or _now_jst()
    rows = jsonl_window.read_jsonl_recent(config.ANALYZED_PATH, days=2, now=now, ts_field="ts")
    return [r for r in rows if (r.get("ts") or "")[:10] == date_iso]


# ---------------------------------------------------------------------------
# 板総計の買い/売り比・ローリング極値(純関数)
# ---------------------------------------------------------------------------
def board_ratio_series(board_rows, date_iso, as_of=None):
    """board_totals_60s_series()(既存の買い/売り総計式をそのまま再利用)に
    ratio=buy_total/sell_total を付加した時刻昇順の系列を返す。
    sell_total<=0の点はratio=None(0除算を捏造しない)。
    as_of("HH:MM")指定時はそれ以前の点だけに切り詰める(バックテスト/dry-run用)。
    """
    series = public_export.board_totals_60s_series(board_rows, today=date_iso)
    out = []
    for pt in series:
        if as_of is not None and pt["time"] > as_of:
            continue
        sell_total = pt.get("sell_total") or 0.0
        ratio = (pt["buy_total"] / sell_total) if sell_total > 0 else None
        out.append({"time": pt["time"], "buy_total": pt["buy_total"],
                    "sell_total": pt["sell_total"], "ratio": ratio})
    return out


def rolling_extreme_flags(series, window_n):
    """各点について、直近window_n点(有効なratioのみ・自分自身を含む)の中で
    自分が最小/最大かを判定する。ratio=Noneの点は常にFalse/False。純関数。
    戻り値は series と同じ長さの [{"time","ratio","is_rolling_min","is_rolling_max"}, ...]。
    """
    out = []
    valid_idx = [i for i, p in enumerate(series) if p.get("ratio") is not None]
    for i, p in enumerate(series):
        ratio = p.get("ratio")
        if ratio is None:
            out.append({"time": p["time"], "ratio": None,
                       "is_rolling_min": False, "is_rolling_max": False})
            continue
        # 自分より前(含む)の有効点のうち直近window_n個
        pos = valid_idx.index(i)
        window_positions = valid_idx[max(0, pos - window_n + 1):pos + 1]
        window_vals = [series[j]["ratio"] for j in window_positions]
        out.append({
            "time": p["time"], "ratio": ratio,
            "is_rolling_min": ratio <= min(window_vals),
            "is_rolling_max": ratio >= max(window_vals),
        })
    return out


def rolling_percentile_rank(series, window_n):
    """各点のratioが、直近window_n点(有効値のみ・自分自身を含む)の中で
    百分位順位(0.0=window内最小・1.0=window内最大)のどこに位置するかを返す。

    ★実データ検証(2026-08-27)で判明: rolling_extreme_flags()の「直近window分の
    厳密な新記録」判定は、板圧力のピークが価格の天井/底に対して数分〜20分程度
    先行/後行する現実の値動き(order-flow leadと呼ばれる現象)には厳しすぎ、
    実際の反転時刻ちょうどでは新記録を更新していない(=直前に既にもっと極端な値を
    記録済み)ケースが多かった。本関数は「直近の極端な水準に近いか」を連続値
    (百分位)で返すことで、detect_reversal() 側が許容帯(REVERSAL_NEAR_EXTREME_PCT)
    を使って「ほぼ極値」も拾えるようにする(rolling_extreme_flagsは「厳密な
    新記録」が必要な別用途向けに残す)。ratio=Noneの点はNone。
    """
    out = []
    valid_idx = [i for i, p in enumerate(series) if p.get("ratio") is not None]
    for i, p in enumerate(series):
        ratio = p.get("ratio")
        if ratio is None:
            out.append(None)
            continue
        pos = valid_idx.index(i)
        window_positions = valid_idx[max(0, pos - window_n + 1):pos + 1]
        window_vals = [series[j]["ratio"] for j in window_positions]
        if len(window_vals) <= 1:
            out.append(1.0)   # 唯一の点=最小かつ最大(次数不定なので上限を採用)
            continue
        rank = sum(1 for v in window_vals if v <= ratio) / len(window_vals)
        out.append(round(rank, 4))
    return out


# ---------------------------------------------------------------------------
# ティック→1分足終値・RSI6・MACD(純関数)
# ---------------------------------------------------------------------------
def minute_close_bars(tick_rows, date_iso, as_of=None):
    """ティックCSV行(time="YYYY-MM-DD HH:MM:SS.mmm", price)から、当日分の
    1分足終値(そのバケット内で最後に観測された値)を時刻昇順で返す。
    [{"time":"HH:MM","close":float}]。価格/時刻いずれか不正な行は無視。
    as_of("HH:MM")指定時はそれ以前のバケットだけに切り詰める。
    """
    buckets = {}  # "HH:MM" -> last price seen (in-order)
    for r in tick_rows or []:
        t = (r.get("time") or "").strip()
        if len(t) < 16 or not t.startswith(date_iso):
            continue
        try:
            price = float(r.get("price"))
        except (TypeError, ValueError):
            continue
        key = t[11:16]
        if as_of is not None and key > as_of:
            continue
        buckets[key] = price
    return [{"time": k, "close": buckets[k]} for k in sorted(buckets)]


def _rsi_from_avg(avg_gain, avg_loss):
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def rsi_series(closes, period=6):
    """Wilder法RSI(標準的な定義)。closes=時系列昇順の終値リスト。
    先頭period個は履歴不足でNone。純関数。"""
    n = len(closes)
    out = [None] * n
    if n <= period or period <= 0:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = diff if diff > 0 else 0.0
        losses[i] = -diff if diff < 0 else 0.0
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out[period] = _rsi_from_avg(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_avg(avg_gain, avg_loss)
    return out


def ema_series(values, period):
    """単純EMA(先頭値をシードにする簡易版・十分な履歴が無くても値を返す
    =内部監視用途の簡便実装。厳密なウォームアップ規約は採らない)。純関数。"""
    n = len(values)
    out = [None] * n
    if n == 0 or period <= 0:
        return out
    k = 2.0 / (period + 1.0)
    ema = values[0]
    out[0] = ema
    for i in range(1, n):
        ema = values[i] * k + ema * (1.0 - k)
        out[i] = ema
    return out


def macd_histogram_series(closes, fast=12, slow=26, signal=9):
    """MACDヒストグラム(EMA fast - EMA slow のシグナル線からの乖離)。純関数。"""
    if not closes:
        return []
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [ef - es for ef, es in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal)
    return [round(m - s, 2) for m, s in zip(macd_line, signal_line)]


# ---------------------------------------------------------------------------
# 検出(オーケストレーション・引数は台帳データのみ=純関数)
# ---------------------------------------------------------------------------
def detect_reversal(board_rows, tick_rows, analyzed_rows, date_iso, as_of=None, params=None):
    """折り返し極値候補を1件検出する(直近時点のみ判定・過去の再現には使わない)。
    条件を満たさなければ None。純関数(I/Oなし・時刻は呼び手が as_of で渡す)。

    params(省略時はconfig既定値): window_min, rsi_period, overbought_th, oversold_th, near_pct。

    判定方針(実データ検証2026-08-27で確定・詳細はconfig.REVERSAL_NEAR_EXTREME_PCTの
    コメント参照):
      高値反転候補(high_reversal) = 板総計比(買い/売り)が直近window_min分のうち
        上位near_pct以内(=買い優勢が過熱=チェイス買いの枯渇) かつ RSI6が過熱
        (>=overbought_th)。実例: 09:07高値時点でratioはその時点までの日中最大値
        (買い圧力の山)、その直後に「超大口の利確売り集中」で反転した。
      安値反転候補(low_reversal)  = 同ratioが下位near_pct以内(=売り優勢が過熱=
        投げ売りの枯渇) かつ RSI6が売られ過ぎ(<=oversold_th)。実例: 10:12-10:14
        RSI6=25.4→22.4→20.0(ユーザー実測「RSI6=22-32」とほぼ一致)。
    """
    p = dict(params or {})
    window_min = p.get("window_min", config.REVERSAL_ROLLING_WINDOW_MIN)
    rsi_period = p.get("rsi_period", config.REVERSAL_RSI_PERIOD)
    overbought_th = p.get("overbought_th", config.REVERSAL_RSI_OVERBOUGHT)
    oversold_th = p.get("oversold_th", config.REVERSAL_RSI_OVERSOLD)
    near_pct = p.get("near_pct", config.REVERSAL_NEAR_EXTREME_PCT)

    ratio_series = board_ratio_series(board_rows, date_iso, as_of=as_of)
    if not ratio_series:
        return None
    ranks = rolling_percentile_rank(ratio_series, window_min)
    latest_rank = ranks[-1]
    if latest_rank is None:
        return None
    near_max = latest_rank >= (1.0 - near_pct)
    near_min = latest_rank <= near_pct
    if not (near_max or near_min):
        return None

    bars = minute_close_bars(tick_rows, date_iso, as_of=as_of)
    closes = [b["close"] for b in bars]
    rsis = rsi_series(closes, period=rsi_period)
    macds = macd_histogram_series(closes)
    latest_rsi = rsis[-1] if rsis else None
    latest_macd = macds[-1] if macds else None
    latest_price = closes[-1] if closes else None
    latest_time = bars[-1]["time"] if bars else ratio_series[-1]["time"]

    if near_max and latest_rsi is not None and latest_rsi >= overbought_th:
        rtype = "high_reversal"
    elif near_min and latest_rsi is not None and latest_rsi <= oversold_th:
        rtype = "low_reversal"
    else:
        return None

    today_analyzed = [r for r in (analyzed_rows or []) if (r.get("ts") or "")[:10] == date_iso]
    ratios = sigmod.sentiment_ratios(today_analyzed)

    return {
        "type": rtype,
        "date": date_iso,
        "time": latest_time,
        "price": latest_price,
        "buy_total": latest_series_field(ratio_series, "buy_total"),
        "sell_total": latest_series_field(ratio_series, "sell_total"),
        "ratio": ratio_series[-1]["ratio"],
        "rsi6": latest_rsi,
        "macd_hist": latest_macd,
        "sentiment": ratios,
        "window_min": window_min,
    }


def latest_series_field(series, field):
    """series の最終点の field 値。空なら None(小道具・純関数)。"""
    return series[-1].get(field) if series else None


# ---------------------------------------------------------------------------
# 重複抑制(状態ファイル・読み書きはこのモジュールの専用ファイルのみ)
# ---------------------------------------------------------------------------
def _load_state(path=None):
    path = path or config.REVERSAL_STATE_PATH
    try:
        if not os.path.isfile(path):
            return {"detections": []}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("detections"), list):
                return data
            return {"detections": []}
    except (OSError, ValueError, UnicodeDecodeError):
        return {"detections": []}


def _save_state(state, path=None):
    path = path or config.REVERSAL_STATE_PATH
    try:
        config.ensure_data_dir()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _hhmm_to_minutes(hhmm):
    try:
        hh, mm = hhmm.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def is_duplicate(detection, state, dedup_window_min=None):
    """同種(type)・同日の直近検出が dedup_window_min 分以内にあれば True。純関数。"""
    if detection is None:
        return False
    dedup_window_min = (config.REVERSAL_DEDUP_WINDOW_MIN
                        if dedup_window_min is None else dedup_window_min)
    cur_min = _hhmm_to_minutes(detection.get("time"))
    if cur_min is None:
        return False
    for d in (state or {}).get("detections", []):
        if d.get("type") != detection.get("type") or d.get("date") != detection.get("date"):
            continue
        prev_min = _hhmm_to_minutes(d.get("time"))
        if prev_min is None:
            continue
        if abs(cur_min - prev_min) <= dedup_window_min:
            return True
    return False


def record_detection(state, detection, max_keep=200):
    """検出をstateへ追記して返す(呼び手が_save_stateで永続化する)。純関数寄り(コピーを返す)。"""
    state = {"detections": list((state or {}).get("detections", []))}
    state["detections"].append({
        "type": detection["type"], "date": detection["date"],
        "time": detection["time"], "price": detection.get("price"),
    })
    if len(state["detections"]) > max_keep:
        state["detections"] = state["detections"][-max_keep:]
    return state


# ---------------------------------------------------------------------------
# 連携ログ フォーマット/投稿
# ---------------------------------------------------------------------------
def _weekday_ja(date_iso):
    y, m, d = (int(x) for x in date_iso.split("-"))
    return WEEKDAY_JA[dt.date(y, m, d).weekday()]


def _time_of_day_ja(hh):
    if hh < 6:
        return "深夜"
    if hh < 11:
        return "朝"
    if hh < 15:
        return "昼"
    if hh < 18:
        return "夕方"
    return "夜"


_TYPE_LABEL_JA = {
    "high_reversal": "高値反転候補",
    "low_reversal": "安値反転候補",
}


def format_log_entry(detection, now=None):
    """CROSS_PROJECT_LOG.mdの既存箇条書き形式に合わせたエントリ文字列を1件分作る。
    「## ログ（新しい順）」直後に挿入する想定(post_to_cross_project_log参照)。
    """
    now = now or _now_jst()
    hh, mm = now.hour, now.minute
    date_iso = detection["date"]
    weekday = _weekday_ja(date_iso)
    tod = _time_of_day_ja(hh)
    label = _TYPE_LABEL_JA.get(detection["type"], detection["type"])

    price = detection.get("price")
    price_str = f"{price:,.0f}円" if price is not None else "価格不明"
    ratio = detection.get("ratio")
    ratio_str = f"{ratio:.2f}" if ratio is not None else "N/A"
    rsi = detection.get("rsi6")
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
    macd = detection.get("macd_hist")
    macd_str = f"{macd:+.1f}" if macd is not None else "N/A"
    sent = detection.get("sentiment") or {}
    bull = sent.get("bull_ratio")
    bear = sent.get("bear_ratio")
    sent_str = (f"強気{bull:.0%}/弱気{bear:.0%}(n={sent.get('n', 0)})"
               if bull is not None and bear is not None else "標本不足/参考値なし")

    pressure_desc = ("買い優勢の過熱(チェイス買いの枯渇)" if detection["type"] == "high_reversal"
                    else "売り優勢の過熱(投げ売りの枯渇)")
    title = f"【自動検出】{detection['time']}時点 285A {label}({price_str})"
    body = (
        f"板総計(買い/売り比)が直近{detection.get('window_min')}分のローリング極値圏"
        f"({pressure_desc}・比率={ratio_str}・買い総計={detection.get('buy_total')}・"
        f"売り総計={detection.get('sell_total')})、"
        f"RSI6={rsi_str}・MACDヒスト={macd_str}。センチメント参考値={sent_str}。"
        f"本検出は板総計×テクニカルの機械的な閾値判定のみ(研究用・未検証)。"
        f"要対応=**トレPJ**＝直近の板・値動きと合わせて折り返しの妥当性をご確認ください"
        f"／**ユーザー**＝自動検出のためダブルチェック推奨。"
    )
    return (f"- **[{date_iso}({hh:02d}:{mm:02d} JST・実測・{weekday}・{tod}) "
           f"おにや→トレPJ/ユーザー・{title}】** {body}")


def post_to_cross_project_log(entry_text, log_path=None, dry_run=True):
    """連携ログの「## ログ（新しい順）」直後にentry_textを挿入する。
    dry_run=True(既定)の間はファイルへ一切触れずentry_textをそのまま返すだけ
    (制約「いきなり本番ログに書き込まない」を担保)。
    実書込み時は見出し直後(既存の最新エントリの直前)に1件挿入する。
    見出しが見つからない/読み込み失敗時は書き込まずNoneを返す(fail-soft)。
    """
    if dry_run:
        return entry_text

    log_path = log_path or config.CROSS_PROJECT_LOG_PATH
    heading = "## ログ（新しい順）"
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return None

    idx = content.find(heading)
    if idx == -1:
        return None
    insert_at = idx + len(heading)
    # 見出し直後は "\n\n- **[既存の最新エントリ]**..." の形式(空行1つ+箇条書き)。
    # ここへ "\n\n<entry_text>" を追加するだけで、続く content[insert_at:] 側の
    # 先頭 "\n\n" がそのまま新エントリと既存最新エントリの間の空行1つになる
    # (自分で末尾に改行を足すと空行が二重になるため付けない)。
    new_content = content[:insert_at] + "\n\n" + entry_text + content[insert_at:]
    try:
        tmp = log_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp, log_path)
        return entry_text
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 実行サイクル(orchestration・main()から呼ぶ想定)
# ---------------------------------------------------------------------------
def run_detection_cycle(now=None, dry_run=None, sym=None):
    """1サイクル分の検出〜(条件次第で)連携ログ投稿を行う。fail-softで例外を投げない。
    戻り値: {"detection": dict|None, "duplicate": bool, "posted": bool, "entry_text": str|None}
    """
    now = now or _now_jst()
    dry_run = (not config.REVERSAL_LOG_POST_ENABLE) if dry_run is None else dry_run
    date_iso = _today_str(now)
    sym = sym or config.SYMBOL

    result = {"detection": None, "duplicate": False, "posted": False, "entry_text": None}
    try:
        board_rows = read_today_board_rows(sym=sym, date_iso=date_iso)
        tick_rows = read_today_tick_rows(sym=sym, date_iso=date_iso)
        analyzed_rows = read_today_analyzed_rows(date_iso=date_iso, now=now)
        detection = detect_reversal(board_rows, tick_rows, analyzed_rows, date_iso)
        result["detection"] = detection
        if detection is None:
            return result

        state = _load_state()
        if is_duplicate(detection, state):
            result["duplicate"] = True
            return result

        entry_text = format_log_entry(detection, now=now)
        result["entry_text"] = entry_text

        posted_text = post_to_cross_project_log(entry_text, dry_run=dry_run)
        if not dry_run and posted_text is not None:
            result["posted"] = True
            new_state = record_detection(state, detection)
            _save_state(new_state)
        elif dry_run:
            # dry-runでも重複抑制の学習はしない(本番投稿していないため状態を汚さない)。
            pass
        return result
    except Exception as e:
        _log(f"ERROR run_detection_cycle failed (fail-soft): {e!r}")
        return result


# ---------------------------------------------------------------------------
# 自己テスト(ネット非依存・合成データ)
# ---------------------------------------------------------------------------
def _run_selftests():
    """純関数のロジックテスト。失敗数を返す(selftest.pyのpillarパターンと同型)。"""
    fails = []

    def ck(name, cond):
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # ---- rolling_extreme_flags ----
    series = [
        {"time": "09:00", "ratio": 1.0},
        {"time": "09:01", "ratio": 1.5},
        {"time": "09:02", "ratio": 0.5},
        {"time": "09:03", "ratio": 2.0},
        {"time": "09:04", "ratio": None},
        {"time": "09:05", "ratio": 0.2},
    ]
    flagged = rolling_extreme_flags(series, window_n=3)
    ck("rolling: index0(唯一値)はmin=maxとも自分自身", flagged[0]["is_rolling_min"] and flagged[0]["is_rolling_max"])
    ck("rolling: index2(0.5)はwindow[1.0,1.5,0.5]内でmin", flagged[2]["is_rolling_min"])
    ck("rolling: index3(2.0)はwindow[1.5,0.5,2.0]内でmax", flagged[3]["is_rolling_max"])
    ck("rolling: ratio=Noneの点はmin/max共にFalse", not flagged[4]["is_rolling_min"] and not flagged[4]["is_rolling_max"])
    ck("rolling: index5(0.2)は有効点だけのwindow[0.5,2.0,0.2]でmin(None点はスキップ)",
       flagged[5]["is_rolling_min"])

    # ---- board_ratio_series (境界値: sell_total=0はratio None) ----
    header = ("time,mid,spread,"
             + ",".join(f"buy{i}px,buy{i}qty" for i in range(1, 11)) + ","
             + ",".join(f"sell{i}px,sell{i}qty" for i in range(1, 11))
             + ",exchange_ts,over_sell_qty,under_buy_qty,market_sell_qty,market_buy_qty")

    def _mkrow(t, buy1qty, sell1qty, over=0, under=0, mbuy=0, msell=0):
        cells = [t, "100", "1.0"]
        cells += [str(100 - 1), str(buy1qty)] + ["0", "0"] * 9
        cells += [str(100 + 1), str(sell1qty)] + ["0", "0"] * 9
        cells += ["2026-08-27T09:00:00", str(over), str(under), str(msell), str(mbuy)]
        return dict(zip(header.split(","), cells))

    rows = [
        _mkrow("2026-08-27 09:00:01.000", 100, 100),   # buy_total=100 sell_total=100 -> ratio=1.0
        _mkrow("2026-08-27 09:01:01.000", 100, 0),      # sell_total=0 -> ratio None
    ]
    bs = board_ratio_series(rows, "2026-08-27")
    ck("board_ratio_series: 2バケット", len(bs) == 2)
    ck("board_ratio_series: ratio=1.0", abs(bs[0]["ratio"] - 1.0) < 1e-9)
    ck("board_ratio_series: sell_total=0はratio None(0除算を捏造しない)", bs[1]["ratio"] is None)

    bs_asof = board_ratio_series(rows, "2026-08-27", as_of="09:00")
    ck("board_ratio_series: as_ofで切り詰め", len(bs_asof) == 1 and bs_asof[0]["time"] == "09:00")

    # ---- minute_close_bars ----
    ticks = [
        {"time": "2026-08-27 09:00:10.000", "price": "100"},
        {"time": "2026-08-27 09:00:40.000", "price": "105"},
        {"time": "2026-08-27 09:01:05.000", "price": "110"},
        {"time": "2026-08-26 15:00:00.000", "price": "999"},   # 別日=除外
        {"time": "2026-08-27 09:01:20.000", "price": "bad"},   # 不正値=無視
    ]
    bars = minute_close_bars(ticks, "2026-08-27")
    ck("minute_close_bars: 2バケット(別日除外)", len(bars) == 2)
    ck("minute_close_bars: 09:00バケットの終値はバケット内最後(105)", bars[0]["close"] == 105.0)
    ck("minute_close_bars: 09:01バケットは不正値行を無視して110", bars[1]["close"] == 110.0)

    bars_asof = minute_close_bars(ticks, "2026-08-27", as_of="09:00")
    ck("minute_close_bars: as_ofで09:01を除外", len(bars_asof) == 1)

    # ---- rsi_series: 境界(単調上昇=RSI100/単調下落=RSI0) ----
    up = [100.0, 101, 102, 103, 104, 105, 106, 107]
    rsis_up = rsi_series(up, period=6)
    ck("rsi: 履歴不足(<=period)はNone", all(v is None for v in rsis_up[:6]))
    ck("rsi: 単調上昇はRSI=100(下落が皆無)", rsis_up[6] == 100.0)

    down = [107.0, 106, 105, 104, 103, 102, 101, 100]
    rsis_down = rsi_series(down, period=6)
    ck("rsi: 単調下落はRSI=0(上昇が皆無)", rsis_down[6] == 0.0)

    flat = [100.0] * 8
    rsis_flat = rsi_series(flat, period=6)
    ck("rsi: 変化なしはRSI=50(中立)", rsis_flat[6] == 50.0)

    # ---- macd_histogram_series: 単調上昇はヒスト>=0が続く傾向(符号のテスト) ----
    trend_up = [100.0 + i for i in range(40)]
    macds = macd_histogram_series(trend_up)
    ck("macd: 長さが終値と一致", len(macds) == len(trend_up))
    ck("macd: 十分なトレンド後はヒストが定義される(None無し)", all(m is not None for m in macds))

    # ---- detect_reversal: 合成シナリオ(高値反転=ratio近傍上位+RSI過熱) ----
    # 板: 序盤は低ratio(0.1)が続き、最終分だけratioが跳ね上がる(買い優勢が過熱=
    # window内の上位near_pct=近傍最大)。実データ検証(09:07高値)で確認した
    # 「天井では板ratioがそれまでの最大値圏」というパターンを模す。
    board_rows2 = []
    t0 = dt.datetime(2026, 8, 27, 9, 0, 0)
    for m in range(6):
        tt = (t0 + dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.000")
        if m < 5:
            board_rows2.append(_mkrow(tt, 50, 500))    # ratio=0.1
        else:
            board_rows2.append(_mkrow(tt, 300, 100))   # ratio=3.0(近傍最大)
    # ティック: 単調上昇(RSI過熱=買われ過ぎを作る)
    tick_rows2 = []
    price = 100.0
    for m in range(8):
        tt = (t0 + dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.000")
        tick_rows2.append({"time": tt, "price": str(price)})
        price += 5
    det = detect_reversal(board_rows2, tick_rows2, [], "2026-08-27",
                          params={"window_min": 30, "rsi_period": 6,
                                  "overbought_th": 70, "oversold_th": 25, "near_pct": 0.2})
    ck("detect_reversal: 高値反転候補を検出", det is not None and det["type"] == "high_reversal")

    # 閾値未達(RSIが過熱していない)なら検出しない
    tick_flat = [{"time": (t0 + dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.000"),
                 "price": "100"} for m in range(8)]
    det_none = detect_reversal(board_rows2, tick_flat, [], "2026-08-27",
                               params={"window_min": 30, "rsi_period": 6,
                                       "overbought_th": 70, "oversold_th": 25, "near_pct": 0.2})
    ck("detect_reversal: RSI条件を満たさなければNone", det_none is None)

    # ---- detect_reversal: 合成シナリオ(安値反転=ratio近傍下位+RSI売られ過ぎ) ----
    board_rows3 = []
    for m in range(6):
        tt = (t0 + dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.000")
        if m < 5:
            board_rows3.append(_mkrow(tt, 300, 100))   # ratio=3.0
        else:
            board_rows3.append(_mkrow(tt, 50, 500))    # ratio=0.1(近傍最小)
    tick_rows3 = []
    price = 200.0
    for m in range(8):
        tt = (t0 + dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S.000")
        tick_rows3.append({"time": tt, "price": str(price)})
        price -= 5
    det_low = detect_reversal(board_rows3, tick_rows3, [], "2026-08-27",
                              params={"window_min": 30, "rsi_period": 6,
                                      "overbought_th": 70, "oversold_th": 25, "near_pct": 0.2})
    ck("detect_reversal: 安値反転候補を検出", det_low is not None and det_low["type"] == "low_reversal")

    # ---- is_duplicate: 重複抑制(30分以内は抑制・超えれば抑制しない) ----
    st = {"detections": [{"type": "high_reversal", "date": "2026-08-27", "time": "09:07", "price": 53450}]}
    d1 = {"type": "high_reversal", "date": "2026-08-27", "time": "09:20"}
    d2 = {"type": "high_reversal", "date": "2026-08-27", "time": "09:50"}
    d3 = {"type": "low_reversal", "date": "2026-08-27", "time": "09:10"}
    ck("is_duplicate: 30分以内・同種は抑制", is_duplicate(d1, st, dedup_window_min=30))
    ck("is_duplicate: 30分超・同種は抑制しない", not is_duplicate(d2, st, dedup_window_min=30))
    ck("is_duplicate: 種類が違えば抑制しない", not is_duplicate(d3, st, dedup_window_min=30))
    ck("is_duplicate: 検出Noneはfalse", not is_duplicate(None, st))

    # ---- record_detection: 追記・上限保持 ----
    st2 = record_detection({"detections": []}, {"type": "low_reversal", "date": "2026-08-27",
                                                "time": "10:13", "price": 50590})
    ck("record_detection: 1件追記", len(st2["detections"]) == 1)
    st3 = record_detection(st2, {"type": "high_reversal", "date": "2026-08-27",
                                 "time": "13:00", "price": 52090})
    ck("record_detection: 元のstateを破壊しない(コピー)", len(st2["detections"]) == 1)
    ck("record_detection: 2件目追記後は2件", len(st3["detections"]) == 2)

    # ---- format_log_entry: 既存フォーマットの必須要素を含む ----
    det_fmt = {"type": "high_reversal", "date": "2026-08-27", "time": "09:07", "price": 53450.0,
              "buy_total": 1000.0, "sell_total": 2000.0, "ratio": 0.5, "rsi6": 94.0,
              "macd_hist": -1.2, "sentiment": {"bull_ratio": 0.3, "bear_ratio": 0.5, "n": 40},
              "window_min": 30}
    entry = format_log_entry(det_fmt, now=dt.datetime(2026, 8, 27, 9, 8))
    ck("format_log_entry: 冒頭が箇条書き+太字の日付ブロック", entry.startswith("- **[2026-08-27(09:08 JST・実測・木・朝)"))
    ck("format_log_entry: 発信元→宛先がおにや→トレPJ/ユーザー", "おにや→トレPJ/ユーザー" in entry)
    ck("format_log_entry: 価格を含む", "53,450円" in entry)
    ck("format_log_entry: RSIを含む", "RSI6=94.0" in entry)

    # ---- post_to_cross_project_log: dry_run=Trueはファイル未変更でentry_textを返すだけ ----
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="reversal_selftest_")
    log_path = os.path.join(tmpdir, "CROSS_PROJECT_LOG.md")
    original = "# heading\n\n## ログ（新しい順）\n\n- **[old entry]** old body.\n"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(original)

    r_dry = post_to_cross_project_log("- **[new entry]** new body.", log_path=log_path, dry_run=True)
    with open(log_path, "r", encoding="utf-8") as f:
        after_dry = f.read()
    ck("post_to_cross_project_log: dry_runはファイル未変更", after_dry == original)
    ck("post_to_cross_project_log: dry_runでもentry_textを返す", r_dry == "- **[new entry]** new body.")

    r_real = post_to_cross_project_log("- **[new entry]** new body.", log_path=log_path, dry_run=False)
    with open(log_path, "r", encoding="utf-8") as f:
        after_real = f.read()
    ck("post_to_cross_project_log: 本番投稿は見出し直後に挿入", "## ログ（新しい順）\n\n- **[new entry]** new body.\n\n- **[old entry]**" in after_real)
    ck("post_to_cross_project_log: 既存の古いエントリは保持される", "- **[old entry]** old body." in after_real)
    ck("post_to_cross_project_log: 実書込みはentry_textを返す", r_real == "- **[new entry]** new body.")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- post_to_cross_project_log: 見出しが無ければ書かずNone ----
    log_path2 = os.path.join(tempfile.mkdtemp(prefix="reversal_selftest2_"), "log.md")
    with open(log_path2, "w", encoding="utf-8") as f:
        f.write("no heading here\n")
    r_missing = post_to_cross_project_log("- entry", log_path=log_path2, dry_run=False)
    ck("post_to_cross_project_log: 見出し無しはNone(fail-soft)", r_missing is None)

    print("PASS" if not fails else f"FAIL: {len(fails)}")
    for name in fails:
        print("  - " + name)
    return len(fails)


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(1 if _run_selftests() else 0)

    if "--dry-run" in sys.argv:
        date_arg = None
        as_of_arg = None
        if "--date" in sys.argv:
            date_arg = sys.argv[sys.argv.index("--date") + 1]
        if "--as-of" in sys.argv:
            as_of_arg = sys.argv[sys.argv.index("--as-of") + 1]
        date_iso = date_arg or _today_str()
        board_rows = read_today_board_rows(date_iso=date_iso)
        tick_rows = read_today_tick_rows(date_iso=date_iso)
        analyzed_rows = read_today_analyzed_rows(date_iso=date_iso)
        det = detect_reversal(board_rows, tick_rows, analyzed_rows, date_iso, as_of=as_of_arg)
        if det is None:
            print(f"no reversal candidate detected (date={date_iso} as_of={as_of_arg})")
        else:
            print(json.dumps(det, ensure_ascii=False, indent=2))
            print("---- log entry (dry-run, not written) ----")
            print(format_log_entry(det))
        sys.exit(0)

    print(__doc__)
