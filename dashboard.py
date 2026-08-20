# -*- coding: utf-8 -*-
"""
dashboard.py - おにや式 掲示板投資法 トレーディング・コックピット(研究/エンタメ・読取専用)

タブ式端末UI: 🎯コックピット / 📈価格×センチメント / 🔬分析 / 🚦シグナル&前向きOOS / 💬コメント。
台帳(analyzed/snapshots/clusters)・価格JSON・signal_export/latest.json を読む。
発注は一切しない。「今すぐ更新」ボタン押下時のみ掲示板/価格を取得(collect-only=LLM非実行)。
データ欠損/較正中でも落ちない。
plotly / streamlit-autorefresh は import失敗時に graceful fallback。
起動: streamlit run dashboard.py
"""
import os
import io
import csv
import json
import logging
import hashlib
import datetime as dt

import streamlit as st

import config
import signals as sig
import signal_engine as eng
import export_signal
import backtest as bt
import jsonl_window

_logger = logging.getLogger("bbs_dashboard")

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except Exception:
    HAS_PLOTLY = False

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False

try:
    from research import run_study
    HAS_STUDY = True
except Exception:
    HAS_STUDY = False

# 柱モジュール・統合モジュールはモジュール単位でimportする(1モジュールの失敗で原因不明のまま
# 関連機能全部が無効化されるのを避ける)。失敗したモジュール名と例外は IMPORT_ERRORS に記録し、
# logging へ出す(fail-soft・呼び出し元は個別のHAS_*フラグで判定)。
IMPORT_ERRORS = {}


def _try_import(modname, label=None):
    """モジュールを個別にimportし、失敗時はNoneを返し理由をIMPORT_ERRORS+loggingに残す。"""
    try:
        return __import__(modname, fromlist=["_"])
    except Exception as e:
        key = label or modname
        IMPORT_ERRORS[key] = repr(e)
        _logger.warning("dashboard: import failed for %s: %r", modname, e)
        return None


# 柱モジュール(insight/alerts/cluster_trend/moomoo_source)
insight = _try_import("insight")
alerts = _try_import("alerts")
cluster_trend = _try_import("cluster_trend")
moomoo_source = _try_import("moomoo_source")
HAS_INSIGHT = insight is not None
HAS_ALERTS = alerts is not None
HAS_CLUSTER_TREND = cluster_trend is not None
HAS_MOOMOO_SOURCE = moomoo_source is not None
HAS_PILLARS = HAS_INSIGHT and HAS_ALERTS and HAS_CLUSTER_TREND and HAS_MOOMOO_SOURCE  # 後方互換の集約フラグ

try:
    from research import evaluate as bbs_eval
    HAS_EVAL = True
except Exception:
    HAS_EVAL = False

# 統合モジュール(L2板 / 熱狂指数 / 米ピア寄り予測 / 収集鮮度 / 信用残)= すべて読取専用・記述用。
board_read = _try_import("board_read")
euphoria = _try_import("euphoria")
peer_lead_read = _try_import("peer_lead_read")
feed_health = _try_import("feed_health")
margin_balance = _try_import("research.margin_balance_285A", label="margin_balance_285A")
HAS_BOARD_READ = board_read is not None
HAS_EUPHORIA = euphoria is not None
HAS_PEER_LEAD_READ = peer_lead_read is not None
HAS_FEED_HEALTH = feed_health is not None
HAS_MARGIN_BALANCE = margin_balance is not None
HAS_INTEG = (HAS_BOARD_READ and HAS_EUPHORIA and HAS_PEER_LEAD_READ
             and HAS_FEED_HEALTH and HAS_MARGIN_BALANCE)  # 後方互換の集約フラグ


# ============================================================================
# パレット / CSS(端末風ライト)
# ============================================================================
COL = {
    "bg": "#F4F6F9", "panel": "#FFFFFF", "border": "#D5DBE5", "muted": "#55606F",
    "text": "#1B2333", "orange": "#C2570B", "red": "#C81E1E", "green": "#15803D",
    "purple": "#6D28D9", "blue": "#1D4ED8", "yellow": "#B45309", "grey": "#8B94A3",
}
# ライト向け: 淡いパステル背景。文字はグローバル font=COL['text'](濃色)で載せAA+を確保。
REGIME_BANDS = [("calm", "#D6F0E0"), ("normal", "#D6E4FA"),
                ("elevated", "#FBE7C6"), ("extreme", "#F7CFCF")]

# ★2026-08-20追加(ユーザー依頼「ボラ・レジーム帯の英語の記載は日本語に」)。
# REGIME_BANDSの英語キー(calm/normal/elevated/extreme)自体は
# signal_engine.classify_regime()等が返す内部データ値と一致させる必要があるため
# そのまま維持し、UI表示のときだけこの辞書で日本語ラベルに変換する
# (=データと表示ロジックを分離・キーの一致判定には影響しない)。
_REGIME_LABEL_JA = {
    "calm": "平穏", "normal": "平常", "elevated": "警戒", "extreme": "急変",
    "calibrating": "較正中",
}

# 価格×センチメントの時間レンジ。表示幅に応じて足を切替(狭い→1分足・中→5分足・広→日足)。
RANGE_OPTIONS = ["1時間", "3時間", "5時間", "12時間", "1日", "3日", "5日", "1ヶ月", "6ヶ月"]
RANGE_DAILY = {"1ヶ月", "6ヶ月"}            # 日足
_RANGE_1M = {"1時間", "3時間", "5時間"}      # 1分足(狭い幅=細かい足)
# 残り(12時間/1日/3日/5日)は5分足
_RANGE_HOURS = {"1時間": 1, "3時間": 3, "5時間": 5, "12時間": 12}
_RANGE_DAYS = {"1日": 1, "3日": 3, "5日": 5, "1ヶ月": 31, "6ヶ月": 183}

# raw_comments/analyzed の直近窓読込(_read_jsonl_recent)に渡す日数。
# signals.compute_signals の trailing z-score窓(config.SIG_ZSCORE_WINDOW=20営業日)に
# 安全マージンを見た60日を基本にしつつ、価格×センチメントの日足レンジ(1ヶ月/6ヶ月)は
# その表示期間+αをカバーしないと重ね描画のセンチメント線が窓の外で欠けてしまうため、
# レンジ日数+14日まで広げる(全履歴機能を削らない=6ヶ月ビューでも欠けさせない)。
_WINDOW_DAYS_BASE = 60


def _window_days_for_range(rng):
    """選択中の時間レンジに応じた直近読込ウィンドウ日数(安全マージン込み)。"""
    days = _RANGE_DAYS.get(rng)
    if rng in RANGE_DAILY and days:
        return max(_WINDOW_DAYS_BASE, days + 14)
    return _WINDOW_DAYS_BASE


def _nice_step(span, target=6):
    """span を約 target 分割する"キリのよい"目盛り幅(1/2/2.5/5×10^k)を返す。span<=0 は None。"""
    import math
    if span is None or span <= 0:
        return None
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _fit_price_yaxis(fig, src, x_candles, win):
    """表示窓(win)内のローソクの高値/安値にY軸(株価)をフィットさせ、キリのよい目盛りにする。
    窓内にバーが無ければ何もしない(autorangeのまま)。"""
    if not (win and src and src.get("bars") and x_candles):
        return
    bars = src["bars"]
    lo = hi = None
    for i, xb in enumerate(x_candles):
        if xb is None or not (win[0] <= xb <= win[1]) or i >= len(bars):
            continue
        b = bars[i]
        low, high = b.get("low"), b.get("high")
        if low is not None:
            lo = low if lo is None else min(lo, low)
        if high is not None:
            hi = high if hi is None else max(hi, high)
    if lo is None or hi is None or hi <= lo:
        return
    pad = (hi - lo) * 0.06
    y0, y1 = lo - pad, hi + pad
    dtick = _nice_step(y1 - y0)
    kwargs = dict(range=[y0, y1], secondary_y=False, row=1, col=1,
                  tickformat=",.0f", ticksuffix="")
    if dtick:
        kwargs["dtick"] = dtick
    fig.update_yaxes(**kwargs)


def _price_source(rng, price_1m, price_i, price_d):
    """レンジ→(価格ソース, 足の分[分/本])。日足はNone。1分足が無ければ5分足へフォールバック。"""
    if rng in _RANGE_1M:
        if price_1m and price_1m.get("bars"):
            return price_1m, 1
        return price_i, 5                      # 1分足未取得時のフォールバック
    if rng in RANGE_DAILY:
        return price_d, None
    return price_i, 5


def _range_window(x_candles, sx, rng, interval_min=5):
    """選択レンジに対応する (xmin, xmax) を返す。時間幅は「足の分」で本数換算(1分足=60本/時・5分足=12本/時)。
    日(intraday)は直近N営業日、日足はdaysで窓取り。価格の最終点を基準。データ無しは None。"""
    anchor = x_candles[-1] if x_candles else (max(sx) if sx else None)
    if anchor is None:
        return None
    if rng in _RANGE_HOURS and x_candles:          # 時間: 直近 N*(60/足分) 本
        per_hour = max(1, int(round(60 / (interval_min or 5))))
        n = _RANGE_HOURS[rng] * per_hour
        return (x_candles[max(0, len(x_candles) - n)], x_candles[-1])
    if rng in ("1日", "3日", "5日") and x_candles:  # 日(intraday): 直近 N 営業日を含める
        keep = set(sorted({t.date() for t in x_candles})[-_RANGE_DAYS[rng]:])
        xs = [t for t in x_candles if t.date() in keep]
        return (min(xs), max(xs)) if xs else (x_candles[0], anchor)
    # 日足レンジ(1ヶ月/6ヶ月)は days で遡る
    days = _RANGE_DAYS.get(rng, 183)
    all_min = min(list(x_candles) + list(sx)) if (x_candles or sx) else anchor
    xmin = anchor - dt.timedelta(days=days)
    return (max(xmin, all_min), anchor)


def inject_css():
    st.markdown(f"""
    <style>
      .stApp {{ background:{COL['bg']}; color:{COL['text']}; }}
      section[data-testid="stSidebar"] {{ background:{COL['panel']}; border-right:1px solid {COL['border']}; }}
      .block-container {{ padding-top:1.1rem; padding-bottom:2rem; max-width:1500px; }}
      div[data-testid="stMetric"] {{ background:{COL['panel']}; border:1px solid {COL['border']};
          border-radius:12px; padding:10px 14px; box-shadow:0 2px 8px rgba(15,23,42,.08); }}
      div[data-testid="stMetricValue"], div[data-testid="stMetricValue"] > div {{
          font-variant-numeric:tabular-nums; font-weight:700; font-size:1.3rem; line-height:1.2;
          white-space:nowrap; overflow:visible !important; text-overflow:clip !important; }}
      div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] p {{ font-size:.78rem; }}
      .stTabs [data-baseweb="tab-list"] {{ gap:4px; }}
      .stTabs [data-baseweb="tab"] {{ background:{COL['panel']}; border:1px solid {COL['border']};
          border-radius:10px 10px 0 0; padding:8px 16px; }}
      .stTabs [aria-selected="true"] {{ background:{COL['panel']}; color:{COL['orange']}; font-weight:700; }}
      .bbs-panel {{ background:{COL['panel']}; border:1px solid {COL['border']}; border-radius:12px;
          padding:12px 15px; margin-bottom:10px; line-height:1.5;
          box-shadow:0 1px 3px rgba(15,23,42,.06), 0 2px 8px rgba(15,23,42,.05); }}
      .bbs-ribbon {{ background:linear-gradient(90deg,{COL['panel']},{COL['bg']});
          border:1px solid {COL['border']}; border-radius:12px; padding:8px 14px; margin-bottom:10px;
          display:flex; gap:14px; align-items:center; flex-wrap:wrap; font-size:.9em; }}
      .chip {{ border-radius:999px; padding:2px 10px; font-size:.82em; font-weight:600; color:#fff;
          white-space:nowrap; }}
      .dot {{ height:10px; width:10px; border-radius:50%; display:inline-block; margin-right:6px; }}
      .calib {{ background:#FBF3E0; color:{COL['text']}; border:1px solid #F0E4C8;
          border-left:4px solid {COL['orange']}; border-radius:8px;
          padding:9px 13px; font-size:.9em; }}
      code, .mono {{ font-variant-numeric:tabular-nums; }}
      @media (max-width: 900px) {{
        .block-container {{ padding-left:.6rem; padding-right:.6rem; padding-top:.7rem; max-width:100%; }}
        div[data-testid="stMetric"] {{ padding:8px 10px; }}
        div[data-testid="stMetricValue"], div[data-testid="stMetricValue"] > div {{ font-size:1.08rem; }}
        .bbs-panel {{ padding:9px 11px; }}
        .bbs-ribbon {{ gap:8px; padding:7px 10px; font-size:.82em; }}
        .stTabs [data-baseweb="tab"] {{ padding:6px 10px; }}
        div[data-testid="stPlotlyChart"], .stPlotlyChart {{ overflow-x:auto; }}
      }}
      @media (max-width: 480px) {{
        .block-container {{ padding-left:.4rem; padding-right:.4rem; }}
        div[data-testid="stMetric"] {{ padding:6px 8px; }}
        div[data-testid="stMetricValue"], div[data-testid="stMetricValue"] > div {{ font-size:.94rem; }}
        div[data-testid="stMetricLabel"], div[data-testid="stMetricLabel"] p {{ font-size:.7rem; }}
        .bbs-panel {{ padding:7px 9px; font-size:.92em; }}
        .bbs-ribbon {{ gap:6px; padding:6px 8px; font-size:.76em; }}
        .chip {{ padding:1px 7px; font-size:.76em; }}
        .stTabs [data-baseweb="tab"] {{ padding:5px 7px; font-size:.85em; }}
      }}
    </style>
    """, unsafe_allow_html=True)


PLOTLY_LAYOUT = dict(
    paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
    font=dict(color="#1B2333"), margin=dict(l=10, r=10, t=28, b=10),
    hovermode="x unified", legend=dict(orientation="h", y=1.12, x=0),
)


def _fig(fig, height=360):
    fig.update_layout(**PLOTLY_LAYOUT, height=height)
    fig.update_xaxes(gridcolor="#E6E9EF", zerolinecolor="#E6E9EF")
    fig.update_yaxes(gridcolor="#E6E9EF", zerolinecolor="#E6E9EF")
    return fig


# ============================================================================
# データ読み込み(読取専用) — st.cache_data はファイルmtimeをキーに含める(ファイル未変更なら
# 再読込しない)。「今すぐ更新」ボタン押下時のみ st.cache_data.clear() で強制再読込する。
# ★2026-08-14 メモリ暴走の実機発見と是正: raw_comments.jsonl等は裏で常時追記され続ける
#   (収集schtaskが数分おきにmtimeを更新)ため、max_entries未設定だとrerun(自動更新)のたびに
#   古いmtimeキーのキャッシュ(数百MB級)が退避されず溜まり続け、実測でプロセスが22GB超まで
#   膨張する不具合が起きた。各cache_data に max_entries を明示(呼び出し元の実ファイル数×2程度)
#   して古いキーを確実に退避させる。
# ★2026-08-14 追加: raw_comments/analyzed/snapshots は巨大(数百MB〜1GB超)なのに全件を毎回
#   丸ごと読んでいたのが遅延の根本原因(実測 数秒〜9秒/ファイル)。jsonl_window(共通ヘルパー)の
#   末尾からの逆読み+日付境界打ち切りで「直近だけ」読む _cached 系を新設し、コックピット/
#   価格×センチメント/シグナル/分析/北極星タブ等の「現在〜直近」用途はこちらを使う。
#   全履歴が要る箇所(北極星の較正日数カウント・snapshots全履歴CSV)は既存の全件読込
#   (_read_jsonl_cached/_read_jsonl)を維持し、実際に必要になった箇所でだけ呼ぶ。
# ============================================================================
def _mtime(path):
    """ファイルの更新時刻(mtime)。無ければNone。st.cache_dataのキャッシュキーに使う。"""
    try:
        return os.path.getmtime(path) if path and os.path.exists(path) else None
    except Exception:
        return None


@st.cache_data(show_spinner=False, max_entries=8)
def _read_jsonl_cached(path, _mtime_key):
    """全件読込(フォールバック/全履歴が必要な機能専用)。実体は jsonl_window(共通)。"""
    return jsonl_window.read_jsonl_full(path)


def _read_jsonl(path):
    return _read_jsonl_cached(path, _mtime(path))


# 呼び出しパターン: {raw_comments, analyzed} × {60日既定, 価格×センチメント6ヶ月ビュー用の
# 拡張ウィンドウ} 程度が同時に生きうる想定 = max_entries=8(ファイル数2×窓2種×mtime世代余裕)。
@st.cache_data(show_spinner=False, max_entries=8)
def _read_jsonl_recent_cached(path, _mtime_key, days):
    """直近days日分だけを末尾から逆読みする高速パス(全件は読まない)。実体はjsonl_window。"""
    return jsonl_window.read_jsonl_recent(path, days=days)


def _read_jsonl_recent(path, days=60):
    return _read_jsonl_recent_cached(path, _mtime(path), days)


# 呼び出しパターン: snapshots.jsonl 1ファイル×tail件数1種(既定1000)+mtime世代余裕=max_entries=4。
@st.cache_data(show_spinner=False, max_entries=4)
def _read_jsonl_tail_cached(path, _mtime_key, n):
    """末尾n行だけを読む高速パス(全件は読まない)。実体はjsonl_window.read_jsonl_tail。"""
    return jsonl_window.read_jsonl_tail(path, n)


def _read_jsonl_tail(path, n=1000):
    return _read_jsonl_tail_cached(path, _mtime(path), n)


def _dedupe_by_id(rows):
    by_id, order = {}, []
    for r in rows:
        rid = r.get("id")
        if rid is None:
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = r
    return [by_id[i] for i in order]


@st.cache_data(show_spinner=False, max_entries=6)
def _load_price_cached(path, _mtime_key):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_price(path):
    return _load_price_cached(path, _mtime(path))


@st.cache_data(show_spinner=False, max_entries=2)
def _load_forward_csv_cached(path, _mtime_key):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _load_forward_csv():
    p = config.FORWARD_OOS_PATH
    return _load_forward_csv_cached(p, _mtime(p))


def _bars_to_dt(bars, gmtoffset):
    off = gmtoffset or 32400
    return [dt.datetime.utcfromtimestamp(b["ts"] + off) for b in bars]


# ---- 統合モジュールの薄い読み取りラッパ(読取専用) --------------------------
# ネット呼び出し(米ピア)は st.cache_data でTTLを付け、毎rerunでの多重取得を防ぐ。
@st.cache_data(ttl=300, show_spinner=False)
def _peer_summary_cached():
    """米半導体ピアの前日比%(Yahoo daily)。5分キャッシュ・失敗はNone。"""
    if not HAS_PEER_LEAD_READ:
        return None
    try:
        return peer_lead_read.peer_summary()
    except Exception:
        return None


@st.cache_data(ttl=5, show_spinner=False)
def _board_summary_cached(date_iso):
    """L2板の要約(ローカル読み)。短TTL=当日板の更新に追随・失敗はNone。"""
    if not HAS_BOARD_READ:
        return None
    try:
        return board_read.summarize("285A", date_iso)
    except Exception:
        return None


def _margin_latest():
    """信用残台帳(margin_balance LEDGER)の最終行を読む。毎回ネット取得しない。無ければNone。"""
    if not HAS_MARGIN_BALANCE:
        return None
    try:
        rows = _load_jsonl_tail(margin_balance.LEDGER, 1)
        return rows[-1] if rows else None
    except Exception:
        return None


def _day_texts(analyzed, raw, day):
    """当日(day=YYYY-MM-DD)の投稿本文リストを抽出(raw優先=全件)。"""
    src = raw if raw else (analyzed or [])
    out = []
    for r in src:
        ts = r.get("ts") or ""
        if day and ts[:10] != day:
            continue
        t = r.get("text")
        if t:
            out.append(t)
    return out


def daily_sentiment_series(analyzed, raw=None):
    per = {}
    for r in analyzed:
        d = (r.get("ts") or "")[:10]
        if len(d) != 10:
            continue
        e = per.setdefault(d, {"raw": 0, "m": 0, "bear": 0})
        if r.get("meaningful"):
            e["m"] += 1
            if r.get("sentiment") == "bearish":
                e["bear"] += 1
    src = raw if raw is not None else analyzed
    for r in src:
        if r.get("garbled") or sig.is_mojibake(r.get("text", "")):
            continue
        d = (r.get("ts") or "")[:10]
        if len(d) != 10:
            continue
        per.setdefault(d, {"raw": 0, "m": 0, "bear": 0})["raw"] += 1
    out = []
    for d in sorted(per):
        e = per[d]
        br = round(e["bear"] / e["m"], 3) if e["m"] >= 5 else None
        out.append({"date": d, "total": e["raw"], "meaningful": e["m"], "bear_ratio": br})
    return out


def _intraday_board_series(raw, analyzed, interval_min):
    """分足ビュー用に、投稿量と弱気率を「足の刻み(interval_min分)」バケットで集計する。
    - 日次系列は各日15:00の1点なので、分足ビューでは rangebreak(15:00→翌9:00を畳む)の
      隙間に隠れて投稿量バーが空に見える。これを解消し、ローソク足に時刻を揃える。
    - 投稿量は raw(全件・非文字化け)から、弱気率は analyzed(meaningful>=3の足のみ)から。
    - x はバケット開始時刻(naive JST=ローソクと同じ時刻系)ゆえ足に揃い、夜間は自動で畳まれる。
    戻り値: (投稿量x, 投稿量y, 弱気率x, 弱気率y%)  ※tz非依存(naiveのまま丸める)。"""
    step = max(1, int(interval_min or 5))

    def _bucket(ts):
        try:
            t = dt.datetime.fromisoformat(ts)
        except Exception:
            return None
        m = (t.hour * 60 + t.minute) // step * step
        return t.replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)

    posts = {}
    for r in raw:
        if r.get("garbled") or sig.is_mojibake(r.get("text", "")):
            continue
        b = _bucket(r.get("ts") or "")
        if b is not None:
            posts[b] = posts.get(b, 0) + 1

    bear = {}  # bucket -> [meaningful, bearish]
    for r in analyzed:
        if not r.get("meaningful"):
            continue
        b = _bucket(r.get("ts") or "")
        if b is None:
            continue
        e = bear.setdefault(b, [0, 0])
        e[0] += 1
        if r.get("sentiment") == "bearish":
            e[1] += 1

    px = sorted(posts)
    py = [posts[b] for b in px]
    bx = sorted(bear)
    by = [round(bear[b][1] / bear[b][0] * 100, 1) if bear[b][0] >= 3 else None for b in bx]
    return px, py, bx, by


# ============================================================================
# 小物
# ============================================================================
STATE_BADGE = {
    "過熱警戒": ("🚀 イナゴ相場警戒", COL["red"]),
    "セリクラ接近": ("🩸 セリクラ接近", COL["orange"]),
    "セリクラ(逆張り買い候補ゾーン)": ("💰 セリクラ!? 逆張り買い候補", COL["purple"]),
    "中立": ("😐 平常運転", COL["green"]),
}
CARD_ICON = {
    "灼熱メーター(過熱)": "🔥", "そう思う大量票": "👍", "イナゴ語彙(euphoria)": "🚀",
    "ネームド集中": "👑", "他銘柄混入率": "🔀", "暴落煽り語彙": "📢",
    "阿鼻叫喚(セリクラ)": "😱", "話題枯れ": "😴", "投稿サージ": "🌊",
}
ST_COL = {"OK": COL["green"], "警戒": COL["yellow"], "発火": COL["red"]}

# config.BBS_LLM_BACKEND("lexicon"/"claude"/"ollama"/"lemonade")の表示名。
LLM_BACKEND_LABELS = {
    "lexicon": "辞書(無料)",
    "claude": "Claude API",
    "ollama": "ローカルLLM(Ollama)",
    "lemonade": "ローカルLLM(Lemonade)",
}


def llm_backend_label():
    """現在の分類エンジン(config.BBS_LLM_BACKEND)の表示名。未知値はそのまま返す(fail-soft)。"""
    backend = getattr(config, "BBS_LLM_BACKEND", "lexicon")
    return LLM_BACKEND_LABELS.get(backend, backend)


def state_badge(state_str):
    base = (state_str or "中立").split("(較正中")[0]
    label, color = STATE_BADGE.get(base, (f"😐 {base}", COL["green"]))
    if "較正中" in (state_str or ""):
        label += " ⏳較正中"
    return label, color


def chip(text, color):
    return f"<span class='chip' style='background:{color}'>{text}</span>"


def _fail_note(label, e, kind="caption", nested=False):
    """例外は生repr直出しをやめ、定型の日本語メッセージ＋折り畳みで詳細を出す(fail-soft)。
    kind: 'error' | 'warning' | 'caption'(既定・軽い失敗向け)。
    nested=True: 呼び出し元が既に st.expander の中(Streamlitはexpanderの入れ子不可のため
    折り畳みは使わず1行の詳細文で代替)。"""
    text = f"⚠️ {label}"
    if kind == "error":
        st.error(text)
    elif kind == "warning":
        st.warning(text)
    else:
        st.caption(text)
    if nested:
        st.caption(f"詳細: `{e!r}`")
    else:
        with st.expander("詳細（エラー内容）", expanded=False):
            st.code(repr(e))


def _rgba(hex_color, alpha):
    """#RRGGBB -> 'rgba(r,g,b,a)'(plotlyは8桁hexを受け付けないため)。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def zone_state(value, warn, fire):
    """what-if閾値に対する現在値の状態(OK/警戒/発火)。純ロジック(UIから閾値注入)。"""
    if value is None:
        return "OK"
    if value >= fire:
        return "発火"
    if value >= warn:
        return "警戒"
    return "OK"


def spark(values, color, dates=None, unit=""):
    """小さなトレンド線(スパークライン)。軸は省くがホバーで日付+値を出す(説明はUI側caption)。"""
    xs, ys = [], []
    for i, v in enumerate(values):
        if v is None:
            continue
        xs.append(dates[i] if (dates and i < len(dates)) else i)
        ys.append(v)
    if not HAS_PLOTLY or len(ys) < 2:
        return None
    f = go.Figure(go.Scatter(
        x=xs, y=ys, mode="lines", line=dict(color=color, width=2),
        fill="tozeroy", fillcolor=_rgba(color, 0.13),
        hovertemplate="%{x}<br>%{y:,.1f}" + unit + "<extra></extra>"))
    f.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    height=48, margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
                    xaxis=dict(visible=False), yaxis=dict(visible=False))
    return f


@st.cache_data(show_spinner=False, max_entries=4)
def _compute_signals_cached(_an_f, _raw_f, _price_d, _price_i, view_day, srcs_key,
                            _an_mtime, _raw_mtime, _price_d_mtime, _price_i_mtime):
    """sig.compute_signals()のキャッシュ層。キーは各元台帳ファイルのmtime+view_day+ソース選択
    (未変更なら再計算しない)。引数名の先頭アンダースコアはStreamlitの規約で「ハッシュ対象外」を
    意味する＝巨大なリスト(raw_f/an_f等)を毎回ハッシュ/コピーしてメモリを食い潰さないため。
    ソース選択の差異は明示的に srcs_key(タプル)で別キー化するので取りこぼさない。"""
    return sig.compute_signals(_an_f, raw_rows=_raw_f, price_daily=_price_d,
                               price_intraday=_price_i, day=view_day)


# ============================================================================
# main
# ============================================================================
def main():
    st.set_page_config(page_title="おにや式 掲示板投資法 285A",
                       page_icon="🔥", layout="wide",
                       initial_sidebar_state="expanded")
    inject_css()

    # サイドバーを先に評価する(sidebar_controlsはraw/analyzedを参照しない)。
    # 選択された時間レンジが分かってから台帳の直近読込ウィンドウ日数を決められるようにするため。
    ctrl = sidebar_controls()

    win_days = _window_days_for_range(ctrl["range"])
    # raw_comments/analyzed は直近win_days日だけを末尾から逆読みする高速パス(全件は読まない)。
    # snapshotsは直近1000行(≒3-5日分)だけ末尾から読む(全履歴が要る箇所は各タブ側で個別に
    # 全件読込を呼ぶ=毎レンダリングの重い経路には混ぜない)。clusters.jsonlは元々軽量(<1MB台)
    # のため既存の全件読込のまま維持する。
    analyzed = _dedupe_by_id(_read_jsonl_recent(config.ANALYZED_PATH, days=win_days))
    raw = _dedupe_by_id(_read_jsonl_recent(config.RAW_COMMENTS_PATH, days=win_days))
    snaps = _read_jsonl_tail(config.SNAPSHOTS_PATH, n=1000)
    clusters = _read_jsonl(config.CLUSTERS_PATH)
    price_d = _load_price(config.PRICE_DAILY_PATH)
    price_i = _load_price(config.PRICE_INTRADAY_PATH)
    price_1m = _load_price(getattr(config, "PRICE_1M_PATH", ""))
    latest_export = export_signal.load_latest()
    fwd_rows = _load_forward_csv()

    srcs = ctrl["sources"]
    raw_f = [r for r in raw if (r.get("source") or "yahoo") in srcs]
    an_f = [r for r in analyzed if (r.get("source") or "yahoo") in srcs]

    view_day = _latest_data_day(raw_f) or dt.date.today().isoformat()
    S = None
    try:
        S = _compute_signals_cached(
            an_f, raw_f, price_d, price_i, view_day, tuple(sorted(srcs)),
            _mtime(config.ANALYZED_PATH), _mtime(config.RAW_COMMENTS_PATH),
            _mtime(config.PRICE_DAILY_PATH), _mtime(config.PRICE_INTRADAY_PATH))
    except Exception as e:
        _fail_note("シグナル計算に失敗しました。", e, kind="error")

    status_ribbon(S, latest_export, snaps, ctrl)
    render_alerts(snaps, latest_export)

    meaningful_n = len(raw_f)
    # 板は「実際の取引日」= 今日の日付でファイル名解決する(view_day=最新コメント日は
    # 早朝に前日へずれ得るため、寄り前板の当日表示が狂わないよう today を使う)。
    board_day = dt.date.today().isoformat()
    # ★2026-08-14: st.tabs(on_change="rerun") + .open で「開いているタブだけ」中身を計算する
    #   (Streamlit 1.59で利用可能・既定のon_change="ignore"だと全タブが毎回計算される)。
    #   タブに固有ウィジェットが無いため@st.fragmentは(仕組み上)ここでは実効性が薄く、
    #   こちらの方が「タブ切替時の全体再計算を抑える」という狙いに直接効く。
    tabs = st.tabs([
        "🎯 コックピット", "📈 価格×センチメント", "🔬 分析",
        "🚦 シグナル&前向きOOS", "🏦 moomoo", "🕵️ 北極星(研究)", "🧠 AI考察",
        f"💬 コメント ({meaningful_n})", "🪧 板"], on_change="rerun")
    t1, t2, t3, t4, t5, t6, t7, t8, t9 = tabs
    with t1:
        if t1.open:
            tab_cockpit(S, latest_export, analyzed, raw_f, ctrl)
    with t2:
        if t2.open:
            tab_price(S, analyzed, raw_f, price_d, price_i, ctrl, price_1m=price_1m)
    with t3:
        if t3.open:
            tab_analysis(raw_f, an_f, price_d, price_i, snaps, view_day)
    with t4:
        if t4.open:
            tab_signals(S, latest_export, fwd_rows, ctrl)
    with t5:
        if t5.open:
            tab_moomoo()
    with t6:
        if t6.open:
            tab_northstar(an_f, raw_f, view_day)
    with t7:
        if t7.open:
            tab_insight(S, latest_export, an_f, snaps)
    with t8:
        if t8.open:
            tab_comments(raw_f, an_f, ctrl)
    with t9:
        if t9.open:
            tab_board(board_day)


def _latest_data_day(rows):
    days = [(r.get("ts") or "")[:10] for r in rows if len(r.get("ts") or "") >= 10]
    return max(days) if days else None


# ============================================================================
# サイドバー(操作パネル) — 設定はローカル(非Dropbox)に自動保存し、
#   フルリロード/再起動でも復元する(session_stateだけだとF5で既定に戻るため)。
# ============================================================================
def _ui_settings_path():
    """UI設定の保存先(%LOCALAPPDATA%\\bbs_sentiment)。Dropbox外=同期チャーン/os.replace問題を避ける。"""
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
            or os.path.expanduser("~"))
    d = os.path.join(base, "bbs_sentiment")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "ui_settings.json")


def _load_ui_settings():
    try:
        p = _ui_settings_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _save_ui_settings(d):
    """ローカルディスクへ atomic 保存(fail-soft)。LOCALAPPDATA はローカルゆえ os.replace 安全。"""
    try:
        p = _ui_settings_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


_UI_DEFAULTS_FACTORY = lambda: {
    "ui_range": "6ヶ月", "src_yahoo": True, "src_5ch": True, "src_stk": True,
    "ui_auto": False, "ui_interval": 60,
    "ui_th_overheat": config.SIG_OVERHEAT_TH,
    "ui_th_capit": config.SIG_CAPITULATION_FIRE,
    "ui_th_votes": config.SIG_ONIYA_VOTES_MAX,
    "ui_density": "詳細",
}


def sidebar_controls():
    # 注意: rawを引数に取らない(この関数はraw/analyzedを一切参照しない)。main()側は
    # サイドバー選択(時間レンジ)を先に確定させてから台帳の直近読込ウィンドウ日数を決める。
    sb = st.sidebar
    sb.markdown("### ⚙️ 操作パネル")

    defaults = _UI_DEFAULTS_FACTORY()
    saved = _load_ui_settings()
    # 「既定に戻す」押下の1ショット: ウィジェット生成前に session_state を掃除(=API制約回避)。
    if st.session_state.pop("_ui_reset", False):
        for k in defaults:
            st.session_state.pop(k, None)
        saved = {}
    # フルリロード時は session_state が空 → 保存値(無ければ既定)で seed。
    #   rerun 時は既存のユーザ値が残るため setdefault は no-op(ライブ変更を保持)。
    for k, v in defaults.items():
        st.session_state.setdefault(k, saved.get(k, v))

    symbol = sb.selectbox("銘柄", ["285A (キオクシア)"], index=0,
                          help="将来の複数銘柄対応を想定")
    # 旧レンジ値(当日/5日 等)が保存に残っていても落ちないよう、無効値は既定へ戻す。
    if st.session_state.get("ui_range") not in RANGE_OPTIONS:
        st.session_state["ui_range"] = "6ヶ月"
    rng = sb.selectbox("時間レンジ", RANGE_OPTIONS, key="ui_range",
                       help="1時間〜5日は5分足、1ヶ月/6ヶ月は日足。チャートの表示窓を切替")
    sb.markdown("**ソース**")
    c1, c2, c3 = sb.columns(3)
    use_y = c1.checkbox("Yahoo", key="src_yahoo")
    use_5 = c2.checkbox("5ch", key="src_5ch")
    use_st = c3.checkbox("StkTwt", key="src_stk")
    sources = set()
    if use_y:
        sources.add("yahoo")
    if use_5:
        sources.add("5ch")
    if use_st:
        sources.update(["stocktwits", "reddit"])
    if not sources:
        sources = {"yahoo"}

    sb.divider()
    sb.markdown("**🔄 自動更新**(台帳の再読込のみ・API非接続)")
    auto = sb.toggle("自動更新", key="ui_auto")
    interval = sb.select_slider("間隔(秒)", [30, 60, 120], key="ui_interval")
    if auto:
        if HAS_AUTOREFRESH:
            st_autorefresh(interval=interval * 1000, key="bbs_auto")
        else:
            sb.caption("streamlit-autorefresh未導入 → 手動更新を使用")
    if sb.button("🔄 今すぐ更新（最新を取得）"):
        with st.spinner("最新コメント/価格を取得中…（数十秒・LLM分析なし）"):
            try:
                import run_once
                run_once.main(collect_only=True)   # collect+price+snapshot のみ(LLM非実行)
            except Exception as e:
                _fail_note("最新データの取得に一部失敗しました（既存データを表示します）。",
                          e, kind="warning")
        st.cache_data.clear()   # 能動的な最新化: mtimeキャッシュを破棄して必ず再読込する
        st.rerun()
    sb.caption("↑ 掲示板/価格を今すぐ取得して再表示（AI判定=弱気率は毎時サイクルで更新）")

    sb.divider()
    sb.markdown("**🎚️ what-if しきい値**(動かすとカードの色/状態がライブ変化)")
    th_overheat = sb.slider("灼熱(過熱)発火", 40, 100, step=5, key="ui_th_overheat")
    th_capit = sb.slider("阿鼻叫喚(セリクラ)発火", 40, 100, step=5, key="ui_th_capit")
    th_votes = sb.slider("そう思う票 発火", 30, 300, step=10, key="ui_th_votes")

    sb.divider()
    density = sb.radio("表示密度", ["詳細", "コンパクト"], horizontal=True, key="ui_density")
    if sb.button("↩ 設定を既定に戻す"):
        _save_ui_settings(_UI_DEFAULTS_FACTORY())
        st.session_state["_ui_reset"] = True
        st.rerun()
    sb.caption("発注なし・別台帳・研究用/未検証・更新ボタンで最新取得(設定はローカルに自動保存)")

    # 現在の設定を永続化(変更時のみ書く=無駄な書込みを避ける)。
    cur = {k: st.session_state.get(k) for k in defaults}
    if cur != saved:
        _save_ui_settings(cur)

    return {"symbol": symbol, "range": rng, "sources": sources, "auto": auto,
            "interval": interval, "th_overheat": th_overheat, "th_capit": th_capit,
            "th_votes": th_votes, "density": density}


# ============================================================================
# ステータスリボン
# ============================================================================
def status_ribbon(S, latest_export, snaps, ctrl):
    price = (S or {}).get("price", {}) if S else {}
    last_ts = (latest_export or {}).get("run_ts") or (
        snaps[-1].get("timestamp") if snaps else "-")
    live = "🟢 LIVE" if ctrl["auto"] else "⚪ 手動"
    state = (S or {}).get("gauges", {}).get("state", "-") if S else "-"
    label, color = state_badge(state)
    px = price.get("last")
    chg = price.get("change_pct")
    px_txt = f"{px:,.0f}円 ({chg:+.2f}%)" if px is not None else "現値 -"
    nxt = _next_schtask()

    parts = [
        f"<span class='dot' style='background:{COL['green'] if ctrl['auto'] else COL['muted']}'></span>{live}",
        f"最終更新 <code>{last_ts}</code>",
        f"次回schtask ~<code>{nxt}</code>",
        f"<b>285A</b> {px_txt}",
        chip(label, color),
        chip(f"分類エンジン: {llm_backend_label()}", COL["blue"]),
    ]
    st.markdown(f"<div class='bbs-ribbon'>{' &nbsp;|&nbsp; '.join(parts)}</div>",
                unsafe_allow_html=True)
    d1, d2, _ = st.columns([1, 1, 4])
    if latest_export:
        d1.download_button("⬇ latest.json",
                           json.dumps(latest_export, ensure_ascii=False, indent=2),
                           file_name="bbs_latest.json", mime="application/json")
    with d2:
        _snapshots_csv_download_widget()


def _next_schtask():
    now = dt.datetime.now()
    h = now.hour
    if h < 9:
        return "09:00"
    if h >= 21:
        return "翌09:00"
    return f"{h+1:02d}:00"


def _snaps_to_csv(snaps):
    keys = ["timestamp", "date", "day_cumulative", "day_meaningful"]
    sigkeys = ["true_volume", "bear_ratio", "overheat", "capitulation", "state",
               "posts_per_hour", "price_close"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(keys + sigkeys)
    for s in snaps:
        sg = s.get("signals") or {}
        w.writerow([s.get(k, "") for k in keys] + [sg.get(k, "") for k in sigkeys])
    return buf.getvalue()


def _snapshots_csv_download_widget():
    """snapshots.csv(全履歴)のダウンロード。snapshots.jsonlは1GB超あり、CSV化には全件読込
    (_read_jsonl、mtimeキャッシュ)が要る。毎レンダリングでは呼ばず、「準備」ボタン押下時
    だけ全件読込+CSV化する(全履歴機能は削らないが、重い経路には混ぜない)。"""
    if st.button("📦 全履歴CSVを準備", help="snapshots.jsonl全件を読み込みCSV化します(件数次第で数秒)"):
        snaps_full = _read_jsonl(config.SNAPSHOTS_PATH)
        st.session_state["_snaps_csv_data"] = _snaps_to_csv(snaps_full) if snaps_full else ""
    data = st.session_state.get("_snaps_csv_data")
    if data:
        st.download_button("⬇ snapshots.csv", data,
                           file_name="bbs_snapshots.csv", mime="text/csv")


# ============================================================================
# 検知バナー(柱3: 異常/転換検知・演出/研究用・売買シグナルではない)
# ============================================================================
def render_alerts(snaps, latest_export):
    """alerts.detect_alerts の結果を横断バナーで表示(較正中は断定抑制・fail-soft)。"""
    if not HAS_ALERTS or not snaps:
        return
    try:
        al = alerts.detect_alerts(snaps[-1], history=snaps, latest_export=latest_export)
    except Exception:
        return
    if not al:
        return
    sev_col = {"fire": COL["red"], "warn": COL["yellow"], "info": COL["blue"]}
    sev_icon = {"fire": "🔴", "warn": "🟡", "info": "🔵"}
    chips = []
    for a in al[:6]:
        c = sev_col.get(a.get("severity"), COL["muted"])
        chips.append(chip(f"{sev_icon.get(a.get('severity'), '')} {a.get('message', '')}", c))
    st.markdown(
        f"<div class='bbs-ribbon'><b>🔔 検知</b>&nbsp; {' '.join(chips)}"
        f"<span style='color:{COL['muted']};font-size:.82em'>"
        f"&nbsp; 演出/研究用・未検証・売買シグナルではない</span></div>",
        unsafe_allow_html=True)


# ============================================================================
# タブ1: コックピット
# ============================================================================
def tab_cockpit(S, latest_export, analyzed, raw, ctrl):
    if not S:
        st.info("データ蓄積中。run_once.py 実行で表示されます。")
        return
    g = S["gauges"]
    dss = daily_sentiment_series(analyzed, raw=raw)

    top = st.columns([1.1, 1.1, 1.6])
    with top[0]:
        _gauge(g["overheat"], "🔥 灼熱メーター(過熱)", ctrl["th_overheat"],
               [COL["green"], COL["yellow"], COL["orange"]])
    with top[1]:
        # ★2026-08-21修正(ユーザー指摘・public_dashboard.py側と同じ是正): 灼熱側は
        # "メーター"を含むのに対しこちらは抜けていた表記不統一を是正。
        _gauge(g["capitulation"], "😱 阿鼻叫喚メーター(セリクラ)", ctrl["th_capit"],
               [COL["green"], COL["orange"], COL["red"]])
    with top[2]:
        regime_band(latest_export)
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    # 外部(米ピア)・需給(信用残)・鮮度(収集)のリボン(全read-only・記述用)。
    # コンパクト密度では補助情報として折り畳む(KPI本体/シグナルカードは常に表示のまま)。
    if ctrl["density"] == "コンパクト":
        with st.expander("📡 外部・需給・鮮度（タップで表示）", expanded=False):
            _ext_supply_freshness_ribbon()
    else:
        _ext_supply_freshness_ribbon()

    st.markdown("#### 🎭 おにや式・逆張り目線（上げ / 下げ / 中立）")
    _direction_view(S)
    _kpi_row(S, dss)

    st.markdown("#### 🎯 シグナル(what-ifしきい値でライブ更新)")
    _whatif_summary(S, ctrl)
    _whatif_chips(S, ctrl)
    st.caption("※エンタメ/研究用・未検証。掲示板は方向よりボラを予測(Antweiler&Frank 2004)。"
               "発注なし・損益ゲート通過まで\"シグナル\"と呼ばない。")
    _featured_comments(analyzed, ctrl)
    _glossary()


def _gauge(value, title, fire_th, colors):
    value = value or 0
    if HAS_PLOTLY:
        f = go.Figure(go.Indicator(
            mode="gauge+number", value=value,
            number={"suffix": " /100", "font": {"size": 21, "color": COL["text"]}},
            title={"text": title, "font": {"size": 14, "color": COL["text"]}},
            gauge={"axis": {"range": [0, 100], "tickcolor": COL["muted"]},
                   "bar": {"color": colors[-1]},
                   "bgcolor": COL["bg"], "borderwidth": 0,
                   "steps": [{"range": [0, min(50, fire_th)], "color": _rgba(colors[0], 0.5)},
                             {"range": [min(50, fire_th), fire_th], "color": _rgba(colors[1], 0.62)},
                             {"range": [fire_th, 100], "color": _rgba(colors[2], 0.78)}],
                   "threshold": {"line": {"color": COL["text"], "width": 3}, "value": fire_th}}))
        # ★2026-08-20修正(ユーザー指摘「100の目盛りが見切れている」): 右マージン18pxでは
        # "100"の目盛りラベル(幅約18px)を収めきれず、実測でプロット右端から約8px
        # はみ出していた(ブラウザのgetBoundingClientRectで直接確認)。左右マージンを
        # 30pxに広げ、ゲージ本体を少し縮めることでラベルの収まる余白を確保する
        # (左右対称を維持=見た目のバランスを崩さない)。
        # ★2026-08-21修正(ユーザー指摘「上の字(タイトル)が見切れている」): ローカル
        # ブラウザ実測(デスクトップ幅・モバイル幅とも)ではmargin.t=40内にタイトルが
        # 収まっていたが、環境(フォント・OS)によってはタイトル行の実高さがこれより
        # 大きくなり得るため、上記と同じ設計思想(実測で足りなかった側へ余裕を持たせる)
        # で48pxへ拡大しさらなる余白バッファを確保する。
        f.update_layout(paper_bgcolor=COL["panel"], height=210,
                        margin=dict(l=30, r=30, t=48, b=6), font=dict(color=COL["text"]))
        # ★2026-08-21追加(ユーザー報告「タイトルが『阿鼻叫喚』になっている」=灼熱/阿鼻叫喚
        # の2つのゲージのタイトルが混同されたように見える不具合報告への対応)。従来は
        # st.plotly_chart()にkeyを指定しておらず、Streamlitの自動キー割当てに依存していた。
        # このゲージは1画面内で2回(灼熱/阿鼻叫喚)呼ばれる上、st_autorefresh(60秒毎)で
        # 頻繁に全体rerunされる構成のため、tilteから導出した安定・一意なkeyを明示して
        # ウィジェット同一性の取り違えを構造的に防ぐ(titleのハッシュ値=絵文字/日本語の
        # 文字コード差異に依存しない安全な短縮ID)。
        gauge_key = "gauge_" + hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
        st.plotly_chart(f, width="stretch", key=gauge_key)
    else:
        st.markdown(f"**{title}: {value}/100** (発火>{fire_th})")
        st.progress(min(1.0, value / 100))


def regime_band(latest_export):
    st.markdown("**ボラ・レジーム帯**")
    vrs = (latest_export or {}).get("vol_regime_score")
    regime = (latest_export or {}).get("vol_regime", "calibrating")
    if HAS_PLOTLY:
        f = go.Figure()
        for name, color in REGIME_BANDS:
            # ★2026-08-20: バー上のラベル(text)・凡例名(name)は日本語表示に変換
            # (英語キー自体はデータ照合用にそのまま=_REGIME_LABEL_JA参照)。
            label = _REGIME_LABEL_JA.get(name, name)
            f.add_trace(go.Bar(x=[25], y=["regime"], orientation="h", marker_color=color,
                               name=label, text=label, textposition="inside",
                               insidetextanchor="middle", hoverinfo="name"))
        if vrs is not None:
            f.add_vline(x=vrs * 100, line=dict(color=COL["text"], width=3))
        f.update_layout(barmode="stack", paper_bgcolor=COL["panel"],
                        plot_bgcolor=COL["panel"], height=90, showlegend=False,
                        margin=dict(l=6, r=6, t=6, b=6),
                        xaxis=dict(range=[0, 100], visible=False),
                        yaxis=dict(visible=False), font=dict(color=COL["text"]))
        st.plotly_chart(f, width="stretch")
    if vrs is None:
        st.markdown("<div class='calib'>レジーム: <b>較正中</b> "
                    "(BVP未確立・dense session蓄積待ち)</div>", unsafe_allow_html=True)
    else:
        st.caption(f"現在レジーム: {_REGIME_LABEL_JA.get(regime, regime)}  (BVP={vrs})")


def _direction_view(S):
    """おにや式・逆張り目線(エンタメ/未検証・投資助言でない)。既存の非routable candidate を表示。
    fade_down=総悲観の底→上昇目線(逆張り買い) / fade_up=過熱の天井→下降目線(逆張り売り) / none=中立。
    極値(過熱>TH or 阿鼻叫喚>FIRE)でのみ向きが出る(なければ中立=正直)。"""
    feats = {"in_low_zone": (S.get("price") or {}).get("in_low_zone"),
             "named_top5_share": (S.get("named") or {}).get("top5_share")}
    try:
        dc = eng.direction_candidate(S.get("gauges", {}), feats)
    except Exception:
        dc = {"side": "none", "strength": None, "rationale": "-"}
    view = {
        "fade_down": ("🔼 上昇目線", "逆張り買い（総悲観の底をフェード）", COL["green"]),
        "fade_up": ("🔽 下降目線", "逆張り売り（過熱の天井をフェード）", COL["red"]),
        "none": ("➖ 中立", "極値でない＝逆張りの妙味なし", COL["muted"]),
    }
    label, sub, color = view.get(dc.get("side", "none"), view["none"])
    stg = dc.get("strength")
    stg_txt = f"（強度 {stg:.0%}）" if stg is not None else ""
    st.markdown(
        f"<div class='bbs-panel' style='border-left:6px solid {color}'>"
        f"<span style='font-size:1.25rem;font-weight:800;color:{color}'>{label}</span>"
        f"&nbsp;<span style='color:{COL['text']}'>{sub}{stg_txt}</span><br>"
        f"<span style='font-size:.85em;color:{COL['muted']}'>根拠: {dc.get('rationale', '')}</span>"
        f"</div>", unsafe_allow_html=True)
    st.caption("⚠️ おにや式の“逆張り目線”＝エンタメ/研究用・**未検証**（損益ゲート未通過）で"
               "投資助言ではありません。掲示板は本来「方向」より「ボラ/出来高」を予測（Antweiler&Frank 2004）。"
               "過熱→天井をフェード（下）、総悲観の底→フェード（上）の発想で、極値のときだけ向きが出ます。")


def _kpi_row(S, dss):
    price = S.get("price", {})
    k = st.columns(4)
    with k[0]:
        px = price.get("last")
        st.metric("現値", f"{px:,.0f}" if px is not None else "-",
                  f"{price.get('change_pct', 0) or 0:+.2f}%",
                  help="現在株価。5分足の直近値と、前日終値からの変化率(%)。")
    with k[1]:
        st.metric("投稿量(全件)", f"{S.get('true_volume', 0):,}",
                  help="AI判定前の生投稿の総数。おにや式で重視する『量(勢い)』の主指標。")
        d14 = dss[-14:]
        _mini(spark([d["total"] for d in d14], COL["blue"],
                    dates=[d["date"] for d in d14], unit="件"))
        st.caption("📈 直近14日の投稿量トレンド(件/日)")
    with k[2]:
        br = S["ratios"].get("bear_ratio")
        st.metric("弱気率(AI)", f"{br:.0%}" if br is not None else "-",
                  help="AIが弱気(bearish)と判定した投稿の割合。判定できた投稿のうちの比率"
                       f"(AI判定サンプル n={S.get('analyzed_today', 0)})。")
        d14 = dss[-14:]
        _mini(spark([(d["bear_ratio"] or 0) * 100 for d in d14], COL["red"],
                    dates=[d["date"] for d in d14], unit="%"))
        st.caption("📉 直近14日の弱気率トレンド(%/日)")
    with k[3]:
        st.metric("投稿速度", f"{S.get('posts_per_hour', 0):.0f}/h",
                  help="1時間あたりの書き込み数。生投稿全件ベースの平均速度(勢いの体感指標)。")


def _mini(fig):
    if fig is not None:
        st.plotly_chart(fig, width="stretch",
                        config={"displayModeBar": False})


def _whatif_items(S, ctrl):
    """what-if 6指標の (name, value, state, note)。state は既存 zone_state のみ使用(新規判定なし)。"""
    g = S["gauges"]
    votes = S["votes"].get("max_bullish_votes", 0)
    lex = S["lexicon"]
    other = S["other_symbols"].get("ratio", 0)
    return [
        ("🔥 灼熱", g["overheat"], zone_state(g["overheat"], 50, ctrl["th_overheat"]),
         f"{g['overheat']:.0f} (発火>{ctrl['th_overheat']})"),
        ("😱 阿鼻叫喚", g["capitulation"],
         zone_state(g["capitulation"], config.SIG_CAPITULATION_WARN, ctrl["th_capit"]),
         f"{g['capitulation']:.0f} (発火>{ctrl['th_capit']})"),
        ("👍 そう思う票", votes, zone_state(votes, ctrl["th_votes"] * 0.6, ctrl["th_votes"]),
         f"最大{votes}票 (発火>={ctrl['th_votes']})"),
        ("🚀 イナゴ語彙", lex["euphoria"]["index"],
         zone_state(lex["euphoria"]["index"], 10, 20), f"{lex['euphoria']['index']}/100"),
        ("📢 暴落煽り", lex["aori"]["index"],
         zone_state(lex["aori"]["index"], 15, 30), f"{lex['aori']['index']}/100"),
        ("🔀 他銘柄混入", other * 100, zone_state(other * 100, 15, 30), f"{other:.0%}"),
    ]


def _whatif_summary(S, ctrl):
    """発火/警戒/OK の集計を1行チップで表示(_whatif_items の state を再集計するだけ)。"""
    states = [it[2] for it in _whatif_items(S, ctrl)]
    n_fire, n_warn, n_ok = states.count("発火"), states.count("警戒"), states.count("OK")
    st.markdown(
        f"<div class='bbs-ribbon' style='margin-bottom:6px'>"
        f"<b>集計</b>&nbsp; {chip(f'🔴 発火 {n_fire}', COL['red'])} "
        f"{chip(f'🟡 警戒 {n_warn}', COL['yellow'])} "
        f"{chip(f'🟢 OK {n_ok}', COL['green'])}"
        f"<span style='color:{COL['muted']};font-size:.88em'>&nbsp;/ 全{len(states)}指標</span></div>",
        unsafe_allow_html=True)


def _whatif_chips(S, ctrl):
    items = _whatif_items(S, ctrl)
    cols = st.columns(3)
    for i, (name, _v, state, note) in enumerate(items):
        c = ST_COL[state]
        with cols[i % 3]:
            st.markdown(
                f"<div class='bbs-panel' style='border-left:4px solid {c}'>"
                f"<b>{name}</b> {chip(state, c)}<br>"
                f"<span style='color:{COL['muted']};font-size:.88em'>{note}</span></div>",
                unsafe_allow_html=True)


def _comment_card(r):
    """tab_comments と同一体裁の1コメントカードHTML。"""
    emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(r.get("sentiment"), "")
    return (f"<div class='bbs-panel'>{emoji} <b>{r.get('sentiment')}</b> "
            f"<span style='color:{COL['muted']};font-size:.88em'>{r.get('ts', '')} / "
            f"👍{r.get('votes_yes', 0)} / {r.get('source', '')} / "
            f"{r.get('cluster_label') or '-'}</span><br>{_esc(r.get('text', ''))}</div>")


def _featured_comments(analyzed, ctrl):
    """コックピットに注目コメントを数件抜粋(発見性・読取専用)。
    選抜: 最多票bullish/最多票bearish/最新セリクラ語彙/最新ts。density=コンパクトで3件。"""
    st.markdown("#### 💬 注目コメント")
    meaningful = [r for r in analyzed if r.get("meaningful")]
    if not meaningful:
        st.caption("コメント蓄積中")
        return
    picks, seen = [], set()

    def _add(r):
        if r is None:
            return
        rid = r.get("id")
        key = rid if rid is not None else id(r)
        if key not in seen:
            seen.add(key)
            picks.append(r)

    def _votes(r):
        return r.get("votes_yes") or 0

    def _ts(r):
        return r.get("ts") or ""

    bulls = [r for r in meaningful if r.get("sentiment") == "bullish"]
    bears = [r for r in meaningful if r.get("sentiment") == "bearish"]
    if bulls:
        _add(max(bulls, key=_votes))               # (a) 最多票bullish
    if bears:
        _add(max(bears, key=_votes))               # (b) 最多票bearish
    caps = [r for r in meaningful if sig._CAP_RE.search(r.get("text", "") or "")]
    if caps:
        _add(max(caps, key=_ts))                    # (c) 最新セリクラ語彙
    _add(max(meaningful, key=_ts))                  # (d) 当日ts最新

    n = 3 if ctrl["density"] == "コンパクト" else 4
    for r in picks[:n]:
        st.markdown(_comment_card(r), unsafe_allow_html=True)
    st.caption("すべてのコメントは「💬 コメント」タブへ")


# ============================================================================
# コックピット・リボン(外部/需給/鮮度)＋用語集 — すべて読取専用・記述用
# ============================================================================
def _ext_supply_freshness_ribbon():
    """米ピア(外部)・信用残(需給)・収集鮮度 の3パネルを1行で。全read-only・記述用。"""
    cols = st.columns(3)
    with cols[0]:
        _freshness_panel()
    with cols[1]:
        _peer_lead_panel()
    with cols[2]:
        _margin_panel()


def _freshness_panel():
    """収集鮮度: 市場時間中にソースが stale なら赤で警告(feed_health)。"""
    st.markdown("**📡 収集鮮度**")
    if not HAS_FEED_HEALTH:
        st.caption("feed_health 未ロード")
        return
    now = dt.datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    # 平日 09:00-15:00 を市場時間とみなす(簡易・祝日非考慮)
    market_open = (now.weekday() < 5) and (9 <= now.hour < 15)
    try:
        h = feed_health.health(config.DATA_DIR, now_iso, market_open=market_open)
    except Exception as e:
        _fail_note("収集鮮度の取得に失敗しました。", e)
        return
    overall = h.get("overall")
    lbl, col = {"STALE": ("🔴 STALE", COL["red"]),
                "WARN": ("🟡 WARN", COL["yellow"])}.get(overall, ("🟢 OK", COL["green"]))
    st.markdown(chip(lbl, col), unsafe_allow_html=True)
    if overall == "STALE":
        stale = "・".join(h.get("stale_sources", []))
        st.caption(f"⚠️ {stale} が {h.get('stale_min')}分以上更新なし")
    st.caption(feed_health.summary_line(h))


def _peer_lead_panel():
    """米半導体ピアの前日比%横並び＋implied_open_lead(寄りブレ目安)。ネットはcache済。"""
    st.markdown("**🌎 米ピア×寄り予測**")
    if not HAS_PEER_LEAD_READ:
        st.caption("peer_lead_read 未ロード")
        return
    summ = _peer_summary_cached()
    if not summ:
        st.caption("取得不可(ネット/欠測)")
        return
    parts = []
    for nm in ("SanDisk", "Micron", "SOX", "TSMC", "NVIDIA"):
        d = summ.get(nm) or {}
        pct = d.get("pct")
        if pct is None:
            parts.append(f"{nm} —")
        else:
            c = COL["green"] if pct >= 0 else COL["red"]
            parts.append(f"{nm} <b style='color:{c}'>{pct:+.1f}%</b>")
    st.markdown("<span style='font-size:.86em'>" + " / ".join(parts) + "</span>",
                unsafe_allow_html=True)
    try:
        lead = peer_lead_read.implied_open_lead(summ)
    except Exception:
        lead = None
    if lead and lead.get("usable"):
        arrow = {"UP": "🔼", "DOWN": "🔽", "FLAT": "➖"}.get(lead.get("direction"), "➖")
        sc = lead.get("score")
        st.caption(f"寄りの上下ブレ目安: {arrow} {lead.get('direction')}/{lead.get('size')}"
                   + (f"(SNDK/MU重み和 {sc:+.2f})" if sc is not None else ""))
    else:
        st.caption("寄り予測: 本命ピア(SanDisk/Micron)欠測=算出不能")


def _margin_panel():
    """信用買残(需給): 台帳最終行を読み、基準日・買残・倍率・前週比・セリクラ判定を1パネル。"""
    st.markdown("**🏦 信用買残(需給)**")
    if not HAS_MARGIN_BALANCE:
        st.caption("margin_balance 未ロード")
        return
    a = _margin_latest()
    if not a or not a.get("buy"):
        st.caption("未取得(次回公表待ち)")
        return
    st.markdown(f"基準日 <code>{a.get('base_date', '-')}</code>", unsafe_allow_html=True)
    wow = a.get("buy_wow_pct")
    wow_txt = f" / 前週比 {wow:+.1f}%" if wow is not None else ""
    st.caption(f"信用買残 {a.get('buy'):,}株 / 信用倍率 {a.get('ratio')}倍{wow_txt}")
    if a.get("climax_read"):
        st.caption("🩸 " + a["climax_read"])


def _glossary():
    """用語集(平易な日本語)。読取専用の説明のみ。"""
    with st.expander("📖 用語集（クリックで開く）"):
        st.markdown(
            "- **overheat(過熱度)**: 掲示板が『買い煽り・強気の陶酔』でどれだけ過熱しているかの0-100スコア。高いほど天井警戒。\n"
            "- **capitulation(投げ売り検知)**: 下落局面で小口が『もうダメだ』と投げ売る阿鼻叫喚の度合い。大底のマーカー候補(0-100)。\n"
            "- **euphoria(熱狂)**: 過熱度の言い換え。『20万いく』『全力信用買い』等の高値目標連呼・買い煽り・弱気派の降参を検出。天井の指標。\n"
            "- **VWAP**: 出来高加重平均価格。その日の売買代金ベースの平均約定価格。株価がこれを上/下どちらで推移するかを見る。\n"
            "- **OVER / UNDER**: 板の表示範囲の外側にある売り注文の合計(OVER)と買い注文の合計(UNDER)。UNDER÷OVERが大きいほど買い圧力。\n"
            "- **特別気配**: 買い(売り)注文が偏りすぎて即約定できないとき、値幅を刻んで気配値だけ表示する状態。約定待ち。\n"
            "- **信用買残**: 信用取引で買われたまま決済されていない株数(週次公表)。多い/倍率が高いほど将来の売り圧力(しこり)。\n"
            "- **セリクラ(セリング・クライマックス)**: 総悲観の投げ売りが一気に出て下げが加速し、底を打つ現象。逆張り買いの候補ゾーン。\n"
            "- **超大口 / 小口**: 1回の約定株数が大きい注文(機関・大口)と小さい注文(個人・小口)。『小口主導ほど掲示板センチメントが効く』が研究核。\n"
            "- **near_buy_share(近接の買い厚み比)**: 最良気配付近(上位数段)の買い注文が買+売に占める割合。>50%で買い支え優勢。\n"
            "\n※すべて研究/エンタメ・記述用。発注や投資助言ではありません。")


# ============================================================================
# タブ9: 🪧 板(L2オーダーブック・読取専用・記述用)
# ============================================================================
def _wall_txt(wall):
    """max_(buy/sell)_wall (px, qty) を『価格@株数』文字列に。無ければ '-'。"""
    if not wall:
        return "-"
    px, qty = wall
    if px is None:
        return "-"
    return f"{px:,.0f}@{qty:,.0f}"


def tab_board(date_iso):
    st.markdown("#### 🪧 板（L2オーダーブック・読取専用）")
    st.caption("kabu の record_all が記録した L2 板を読取専用で要約(発注なし・戦略非接触・記述用)。"
               f"対象日={date_iso}。板記録がOFF/寄り前/休場ならデータは出ません。")
    if not HAS_BOARD_READ:
        st.info("board_read モジュール未ロード。")
        return
    s = _board_summary_cached(date_iso)
    if not s or (not s.get("has_session") and not s.get("premarket")):
        st.info("板データなし(寄り前で未記録／休場、または板記録OFF)。")
        return
    if s.get("has_session"):
        _board_session_panel(s)
    else:
        _board_premarket_panel(s)


def _board_session_panel(s):
    """寄付済み: 現値/スプレッド/近接買い厚み比/大口の壁/当日高安。"""
    sess = s.get("session") or {}
    st.markdown("##### 📈 寄付済み（セッション板）")
    c = st.columns(4)
    mid = sess.get("mid")
    c[0].metric("現値(mid)", f"{mid:,.0f}" if mid is not None else "-",
                help="最良買い気配と最良売り気配の中値(mid price)。")
    spr = sess.get("spread")
    c[1].metric("スプレッド", f"{spr:,.1f}" if spr is not None else "-",
                help="最良売り−最良買いの値幅。狭いほど流動性が高い(約定コストが低い)。")
    nbs = sess.get("near_buy_share")
    c[2].metric("近接の買い厚み比", f"{nbs:.0%}" if nbs is not None else "-",
                help="最良付近(上位3段)の買い注文が買+売に占める割合。>50%で買い支え優勢。")
    rbs = s.get("session_rolling_buy_share")
    c[3].metric("買い厚み比(移動平均)", f"{rbs:.0%}" if rbs is not None else "-",
                help="直近240行の近接買い厚み比の平均。その日の基調(瞬間値より安定)。")
    c2 = st.columns(3)
    c2[0].metric("大口の売り蓋", _wall_txt(sess.get("max_sell_wall")),
                 help="最も数量の大きい売り注文の段(価格@株数)。上値の重し。")
    c2[1].metric("大口の買い支え", _wall_txt(sess.get("max_buy_wall")),
                 help="最も数量の大きい買い注文の段(価格@株数)。下値の支え。")
    hi, lo = s.get("day_high"), s.get("day_low")
    hilo = f"{hi:,.0f} / {lo:,.0f}" if (hi is not None and lo is not None) else "-"
    c2[2].metric("当日高安(mid)", hilo, help="セッション板 mid の当日高値/安値。")
    nb = sess.get("near_buy_qty")
    ns = sess.get("near_sell_qty")
    st.caption(f"板時刻 {sess.get('time', '-')} / 近接買い {nb:,.0f}株・近接売り {ns:,.0f}株"
               if (nb is not None and ns is not None) else f"板時刻 {sess.get('time', '-')}")
    st.caption("read-only・発注なし・記述用。板の厚みは瞬間で変動するため1点で断じない。")


def _board_premarket_panel(s):
    """未寄付(特別気配): 気配値/OVER・UNDER比/成行買い偏り。"""
    pm = s.get("premarket") or {}
    st.markdown("##### 🕗 特別気配・未寄付（寄り前板）")
    c = st.columns(4)
    kh = pm.get("kehai_price")
    khpct = pm.get("kehai_change_pct")
    c[0].metric("寄前気配値", f"{kh:,.0f}" if kh is not None else "-",
                f"{khpct:+.2f}%" if khpct is not None else None,
                help="板寄せの予想約定値段(実際に上下する気配値=Buy1/Sell1)。前日終値比%。"
                     "※CalcPriceは未約定だと前日終値のまま動かないので使わない。")
    our = pm.get("over_under_ratio")
    c[1].metric("OVER/UNDER比", f"{our:.2f}" if our is not None else "-",
                help="表示範囲外の買い合計÷売り合計(UnderBuy÷OverSell)。>1で買い優勢。")
    mbi = pm.get("market_buy_imbalance")
    c[2].metric("成行 買い/売り比", f"{mbi:.2f}" if mbi is not None else "-",
                help="成行買い÷成行売り。>1で寄りは買い優勢の圧力。")
    c[3].metric("特別気配", "はい" if pm.get("is_special_quote") else "いいえ",
                help="注文が偏り即約定できず、気配値だけ表示している状態か(約定待ち)。")
    over = pm.get("over_sell_qty")
    under = pm.get("under_buy_qty")
    prev = pm.get("prev_close")
    st.caption(f"気配時刻 {pm.get('ts', '-')} / OVER(範囲外の売り){over}・UNDER(範囲外の買い){under}"
               f" / 前日終値 {prev}")
    st.caption("OVER/UNDER=最良気配の外側にある売り/買い注文の累計(圧力の目安・単純比較でない)。"
               "read-only・発注なし・記述用。")


# ============================================================================
# タブ2: 価格×センチメント
# ============================================================================
def tab_price(S, analyzed, raw, price_d, price_i, ctrl, price_1m=None):
    rng = ctrl["range"]
    src, interval_min = _price_source(rng, price_1m, price_i, price_d)
    intraday = interval_min is not None
    _fmt = {1: "1分足", 5: "5分足", None: "日足"}.get(interval_min, "日足")
    st.markdown(f"#### 📈 価格 × センチメント ({rng} ・ {_fmt})")
    dss = daily_sentiment_series(analyzed, raw=raw)
    if not HAS_PLOTLY:
        _price_fallback(price_d, dss)
        _hourly_bucket_chart(S)
        return
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28],
                        vertical_spacing=0.05, specs=[[{"secondary_y": True}], [{}]])
    x_candles = []
    if src and src.get("bars"):
        bars = src["bars"]
        x_candles = _bars_to_dt(bars, (src.get("meta") or {}).get("gmtoffset"))
        fig.add_trace(go.Candlestick(
            x=x_candles, open=[b["open"] for b in bars], high=[b["high"] for b in bars],
            low=[b["low"] for b in bars], close=[b["close"] for b in bars],
            name="285A", increasing_line_color=COL["green"],
            decreasing_line_color=COL["red"]), row=1, col=1)
    else:
        st.caption("価格データ未取得。センチメントのみ表示。")
    sx = []
    if intraday:
        # 分足ビューは足の刻みで投稿量・弱気率を集計しローソクへ時刻を揃える
        # (日次1点=15:00だと rangebreak の隙間に隠れ、投稿量バーが空に見えるため)。
        px, py, brx, bry = _intraday_board_series(raw, analyzed, interval_min)
        sx = px  # 時間レンジ窓の算出に使う
        if brx:
            fig.add_trace(go.Scatter(
                x=brx, y=bry, name="弱気率%(足別)", mode="lines",
                line=dict(color=COL["red"], width=1.5), connectgaps=False),
                row=1, col=1, secondary_y=True)
        if px:
            fig.add_trace(go.Bar(x=px, y=py, name="投稿量(足別)",
                                 marker_color=COL["blue"]), row=2, col=1)
    elif dss:
        sx = [dt.datetime.fromisoformat(d["date"] + "T15:00:00") for d in dss]
        fig.add_trace(go.Scatter(
            x=sx, y=[(d["bear_ratio"] or 0) * 100 if d["bear_ratio"] is not None else None for d in dss],
            name="弱気率%", mode="lines+markers", line=dict(color=COL["red"], width=2)),
            row=1, col=1, secondary_y=True)
        fig.add_trace(go.Bar(x=sx, y=[d["total"] for d in dss], name="投稿量/日(全件)",
                             marker_color=COL["blue"]), row=2, col=1)
    fig.update_yaxes(title_text="株価(円)", secondary_y=False, row=1, col=1)
    fig.update_yaxes(title_text="弱気率%", range=[0, 100], secondary_y=True, row=1, col=1)
    fig.update_yaxes(title_text="投稿量(件)", row=2, col=1)
    # サイドバー「時間レンジ」を x 軸の窓へ実際に適用する(=1D/5D/1ヶ月/6ヶ月で表示が変わる)。
    # ※以前の plotly プリセットボタン(1D等)は日足の自動パディングと噛み合わず動かない上に
    #   サイドバーと重複していたため撤去し、時間レンジの操作はサイドバーに一本化した。
    win = _range_window([t for t in x_candles if t is not None],
                        [t for t in sx if t is not None], rng, interval_min or 5)
    if win:
        # shared_xaxes では上段は下段に matches するため row 指定だと無視される。
        # 全 x 軸へ range を適用し master に効かせる(=時間レンジが実際に反映される)。
        fig.update_xaxes(range=[win[0], win[1]])
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    if intraday:
        # 5分足ビューは夜間(15:00→翌9:00)と週末の空白を圧縮して連続表示にする
        # (日足ビュー=1ヶ月/6ヶ月には適用しない)。
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]),
                                      dict(bounds=[15, 9], pattern="hour")])
    # 縦軸(株価)を表示窓内の高値/安値へフィット＋キリのよい目盛りに(ローソクが潰れないように)。
    _fit_price_yaxis(fig, src, x_candles, win)
    st.plotly_chart(_fig(fig, 470), width="stretch")
    st.caption("時間レンジは左の操作パネルで切替。表示幅に応じて足を自動調整"
               "（1〜5時間=1分足／12時間〜5日=5分足／1ヶ月・6ヶ月=日足）。"
               "分足は夜間/週末を詰めて表示。チャート上はドラッグでズーム、ダブルクリックで戻せます。")

    st.markdown("#### ⏱️ 当日1時間バケット(量と率の分離)")
    _hourly_bucket_chart(S)


def _hourly_bucket_chart(S):
    if not S or not S.get("hourly"):
        st.caption("本日の投稿がまだありません。")
        return
    hb = S["hourly"]
    hours = sorted(hb.keys())
    if not HAS_PLOTLY:
        import pandas as pd
        st.bar_chart(pd.DataFrame({h: hb[h] for h in hours}).T[["bullish", "bearish", "neutral"]])
        return
    f = make_subplots(specs=[[{"secondary_y": True}]])
    f.add_trace(go.Bar(x=hours, y=[hb[h]["bullish"] for h in hours], name="強気", marker_color=COL["green"]))
    f.add_trace(go.Bar(x=hours, y=[hb[h]["bearish"] for h in hours], name="弱気", marker_color=COL["red"]))
    f.add_trace(go.Bar(x=hours, y=[hb[h]["neutral"] for h in hours], name="中立", marker_color=COL["muted"]))
    line = []
    for h in hours:
        m = sum(hb[h][k] for k in ("bullish", "bearish", "neutral"))
        line.append(hb[h]["bearish"] / m * 100 if m else None)
    f.add_trace(go.Scatter(x=hours, y=line, name="弱気率%", line=dict(color=COL["red"], width=2)),
                secondary_y=True)
    f.update_layout(barmode="stack")
    f.update_yaxes(title_text="投稿数(件)", secondary_y=False)
    f.update_yaxes(title_text="弱気率%", range=[0, 100], secondary_y=True)
    f.update_xaxes(title_text="時刻(時)")
    st.plotly_chart(_fig(f, 300), width="stretch")


def _price_fallback(price_d, dss):
    import pandas as pd
    if price_d and price_d.get("bars"):
        bars = price_d["bars"]
        st.line_chart(pd.DataFrame({"close": [b["close"] for b in bars]},
                                   index=_bars_to_dt(bars, 32400)))
    if dss:
        st.bar_chart(pd.DataFrame(dss).set_index("date")["total"])


# ============================================================================
# タブ3: 分析
# ============================================================================
def tab_analysis(raw, analyzed, price_d, price_i, snaps, view_day):
    st.markdown("#### 🔬 分析(研究用・未検証)")

    st.markdown("##### ① リード/ラグ(投稿速度 × 5分足)")
    if HAS_STUDY:
        try:
            it = run_study.intraday_lead_lag(raw, price_i, view_day)
        except Exception as e:
            it = {"n_vol": 0, "note": f"study失敗 {e!r}"}
        diag = it.get("diagnostics", {})
        if diag:
            st.markdown(
                f"<div class='calib'>取引時間内投稿 {diag.get('trading_hour_posts', 0)} 件 / "
                f"時間外 {diag.get('offhour_posts', 0)} 件 "
                f"(引け後集中度 {diag.get('post_close_concentration')})</div>",
                unsafe_allow_html=True)
        if it.get("n_vol", 0) >= 4 and HAS_PLOTLY:
            c = it["vol_ccf"]
            f = go.Figure()
            f.add_trace(go.Bar(x=c["lags"], y=[r or 0 for r in c["r"]],
                               marker_color=COL["orange"], name="CCF r"))
            if c.get("band"):
                for b in (c["band"], -c["band"]):
                    f.add_hline(y=b, line=dict(color=COL["muted"], dash="dot"))
            f.update_layout(xaxis_title="lag k (投稿→価格は k>0)", yaxis_title="Pearson r")
            st.plotly_chart(_fig(f, 300), width="stretch")
        for line in it.get("readout", []):
            st.write("・" + line)
    else:
        st.caption("research モジュール未ロード。")

    _cluster_trend_section(analyzed, price_d)

    st.markdown("##### ② データ健全性 & 特徴信頼")
    _data_health(raw, analyzed, snaps, view_day)

    # 較正日数カウント(_dense_count_estimate)は全期間のユニーク日付が要るため、直近tail
    # (snaps)ではなく全件読込を使う。ここは分析タブが実際に開かれた時だけ実行され
    # (main()のst.tabs on_change="rerun"+.openで非アクティブタブはスキップされる)、
    # かつ_read_jsonlはmtimeキャッシュ済みなので「毎レンダリングの重い経路」にはならない。
    snaps_full = _read_jsonl(config.SNAPSHOTS_PATH)
    n_calib = _dense_count_estimate(snaps_full)
    with st.expander(f"較正中パネル ({n_calib}/{config.SIG_MIN_CALIB_DAYS})", expanded=False):
        st.caption("下記は dense session 蓄積待ち(点推定なし)。標本が揃い次第この折り畳みから前面化する。")
        cc = st.columns(2)
        with cc[0]:
            st.markdown("##### ③ イベントスタディ(過熱/セリクラ→将来リターン)")
            _calibrating_box("cross-day cohort は dense session 蓄積待ち。"
                             "n<5 のうちは平均線を出さず個別のみ(点推定なし)。", snaps_full)
        with cc[1]:
            st.markdown("##### ④ ボラ/出来高 予測 vs 実現(防御可能なエッジ)")
            _calibrating_box("投稿量→翌日ボラ/出来高の予測 vs 実現。ML pred_realized_vol 差替予定。"
                             "方向指標はこのパネルに出さない。", snaps_full)


def _calibrating_box(text, snaps):
    n = _dense_count_estimate(snaps)
    st.markdown(f"<div class='calib'>⏳ calibrating (n={n}/{config.SIG_MIN_CALIB_DAYS})<br>"
                f"<span style='font-size:.9em'>{text}</span></div>", unsafe_allow_html=True)


def _dense_count_estimate(snaps):
    return len({s.get("date") for s in snaps if (s.get("signals") or {}).get("true_volume")})


def _data_health(raw, analyzed, snaps, view_day):
    dense = eng.dense_session_dates(raw, "9999-99-99")
    per = {}
    for r in raw:
        if r.get("garbled") or sig.is_mojibake(r.get("text", "")):
            continue
        d = (r.get("ts") or "")[:10]
        if len(d) == 10:
            per[d] = per.get(d, 0) + 1
    cols = st.columns(3)
    cols[0].metric("dense session数", len(dense),
                   help=f"投稿>={config.SIG_MIN_DAY_COVERAGE} かつ取引時間バケット>={config.SIG_MIN_HOUR_BUCKETS}")
    cols[1].metric("z基準確立まで",
                   f"あと{max(0, config.SIG_DENSE_MIN_CALIB - len(dense))}日",
                   help="cross-day z 抑制解除の残り")
    ratio = (len(analyzed) / len(raw)) if raw else 0
    cols[2].metric("analyzed/raw", f"{ratio:.0%}", help="AI判定サンプル比")
    # ts が日時として読めない行は日次集計から静かに落ちる（5chの「NG」「Over 1000」等）。
    # 挙動は変えず件数だけ見せる＝沈黙をやめる（2026-07-28 ユーザー判断・案C）。
    try:
        import ts_health as _tsh
        _h = _tsh.ts_health(analyzed)
        if _h.get("dropped"):
            st.caption(_tsh.format_health(_h, prefix="ts健全性"))
    except Exception:
        pass
    if per and HAS_PLOTLY:
        days = sorted(per)[-30:]
        f = go.Figure(go.Bar(x=days, y=[per[d] for d in days], name="投稿数/日",
                             marker_color=[COL["green"] if d in dense else COL["border"] for d in days]))
        f.add_hline(y=config.SIG_MIN_DAY_COVERAGE, line=dict(color=COL["muted"], dash="dot"),
                    annotation_text=f"最低カバレッジ {config.SIG_MIN_DAY_COVERAGE}件",
                    annotation_position="top left",
                    annotation_font=dict(color=COL["muted"], size=11))
        f.update_layout(yaxis_title="投稿数/日(件)", xaxis_title="日付(緑=dense / 灰=非dense)")
        st.plotly_chart(_fig(f, 240), width="stretch")
    fx = (snaps[-1].get("feel_vs_llm") if snaps else None) or {}
    if fx.get("agree_rate") is not None:
        st.caption(f"feel↔AI 一致率 {fx['agree_rate']:.0%} "
                   f"(一致{fx.get('agree', 0)}/不一致{fx.get('disagree', 0)})")
    _feel_vs_llm_matrix(fx)


def _feel_vs_llm_matrix(fx):
    """snapshot.feel_vs_llm()のmatrix(投稿者feelLabel×LLM判定の混同行列)を小さな表で表示。
    読取専用・記述用。標本が疎(<20)なら注記を出す(fx=snaps[-1]['feel_vs_llm'])。"""
    matrix = (fx or {}).get("matrix") or {}
    if not matrix:
        return
    total_n = (fx.get("agree") or 0) + (fx.get("disagree") or 0)
    st.markdown("###### feel↔AI 混同行列(投稿者自己申告 × LLM判定)")
    if total_n < 20:
        st.caption(f"⚠️ サンプル不足(n={total_n})。参考値として見る。")
    try:
        import pandas as _pd
        llm_cols = sorted({k for row in matrix.values() for k in row})
        df = _pd.DataFrame(matrix).T.reindex(columns=llm_cols).fillna(0).astype(int)
        df.index.name = "feel(自己申告)"
        st.dataframe(df, use_container_width=True)
    except Exception as e:
        _fail_note("混同行列の表示に失敗しました。", e)


# ============================================================================
# タブ4: シグナル & 前向きOOS
# ============================================================================
def tab_signals(S, latest_export, fwd_rows, ctrl):
    st.markdown("#### 🚦 シグナル & 前向きOOS")

    e = latest_export or {}
    cols = st.columns(4)
    cols[0].metric("台帳", "signal別台帳", help="data/signal_export/ history.jsonl + latest.json")
    cols[1].metric("較正", e.get("calibration_status", "-"),
                   help=f"dense session n={e.get('calib_days', 0)}/{config.SIG_MIN_CALIB_DAYS}")
    oos_n = sum(1 for r in fwd_rows if r.get("is_oos") == "True" and r.get("forward_return_1d"))
    cols[2].metric("累計OOS(成熟)", f"{oos_n}/{config.SIG_MIN_CALIB_DAYS}",
                   help="真OOS(HARNESS_START_DATEより後)で成熟した凍結行")
    ready = oos_n >= config.SIG_MIN_CALIB_DAYS
    cols[3].markdown(chip("🟢 READY" if ready else "🟡 CALIBRATING",
                          COL["green"] if ready else COL["yellow"]), unsafe_allow_html=True)
    st.progress(min(1.0, oos_n / config.SIG_MIN_CALIB_DAYS))
    st.markdown("<div class='calib'>発注なし・別台帳・損益ゲート(baseline vs candidate Δpnl>0 on "
                "真OOS 10-20日 + PBO + 人承認)通過まで\"シグナル\"と呼ばない。</div>",
                unsafe_allow_html=True)

    st.markdown("##### ⑤ バックテスト / エッジ要約(PBOガード)")
    try:
        grid = bt.run_default_grid(fwd_rows)
        for name, res in grid.items():
            st.write(f"・**{name}**: {res['verdict']} (OOS n={res['n']}, "
                     f"Δpnl={res['delta_cum_pnl']})")
    except Exception as e2:
        _fail_note("バックテストの算出に失敗しました。", e2)
    st.caption(f"現状: 全ルール REJECT(標本不足/エッジ未確認)。相関/CCF/AUCは screening のみ。"
               f"Δpnlは**往復コスト補正後net**(実測spread≈{config.BACKTEST_ROUNDTRIP_COST:.2%}/回転・"
               "全プロト共通のコスト補正=番犬systemic)。")

    _forward_oos_eval_section()

    if S:
        st.markdown("##### 🎯 9シグナル(現在値・what-if反映)")
        _nine_cards(S, ctrl)

    if e:
        with st.expander("エクスポートJSON(latest)"):
            st.json({k: e.get(k) for k in ("schema_version", "asof_date", "cutoff",
                     "vol_regime", "vol_regime_score", "range_day_score",
                     "direction_candidate", "confidence", "calib_days",
                     "signal_spec_hash")})


def _forward_oos_eval_section():
    """柱2: evaluate.py の自動forward-OOS評価を整合表示(読取専用・現状は"評価不能"が正)。"""
    if not HAS_EVAL:
        return
    st.markdown("##### 🔬 自動forward-OOS評価(柱2・evaluate.py)")
    try:
        ev = bbs_eval.evaluate_forward_oos()
    except Exception as e:
        _fail_note("前向きOOS評価の算出に失敗しました。", e)
        return
    status = ev.get("status")
    n_oos = ev.get("n_oos", 0)
    min_oos = ev.get("min_oos", config.SIG_MIN_CALIB_DAYS)
    if status == "judged":
        cands = ev.get("candidates", {})
        cols = st.columns(max(1, len(cands)))
        for i, (nm, c) in enumerate(cands.items()):
            d = c.get("delta_cum_pnl")
            cols[i].metric(f"Δpnl {nm}", "-" if d is None else f"{d:+.4f}",
                           help="baseline比の累積損益差(真OOS・起点日明示)")
        dsr = ev.get("deflated_sharpe", {}) or {}
        st.markdown(
            f"<div class='calib'>候補判定(人承認前・シグナルと呼ばない)｜"
            f"PBO={ev.get('pbo')} ／ DSR({dsr.get('best_candidate')})={dsr.get('dsr')} "
            f"(試行数 {dsr.get('n_trials')})<br>"
            f"<span style='font-size:.9em'>{ev.get('note', '')}</span></div>",
            unsafe_allow_html=True)
    else:
        st.markdown(
            f"<div class='calib'>⏳ <b>{status or '評価不能'}</b> "
            f"(真OOS成熟 n={n_oos}/{min_oos})<br>"
            f"<span style='font-size:.9em'>{ev.get('note', '')}</span></div>",
            unsafe_allow_html=True)
    try:
        va = bbs_eval.volume_sentiment_association()
        st.caption(f"出来高×センチメント連関(screening・損益ゲートではない): "
                   f"{va.get('status')} n={va.get('n', 0)}")
    except Exception:
        pass
    rs = ev.get("retail_share_today")
    st.caption(f"小口比率 retail_share(moomoo): "
               f"{'未接続(None)' if rs is None else rs} ／ "
               "「小口主導ほどセンチメントが効く」仮説の入力(moomoo権限確定後に実データ)")


def _load_jsonl_tail(path, n=40):
    """別台帳(jsonl)の末尾n行を dict list で返す。無ければ空。read専用・落ちない。"""
    if not os.path.exists(path):
        return []
    out = []
    # ★2026-08-19修正(おにや22:13投稿・重大障害調査の横展開): torn write対策
    # (バイナリモード+行ごと個別decode)。1行分のバイト破損で以降の行が全滅しない。
    try:
        with open(path, "rb") as f:
            for raw_line in f:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        return []
    return out[-n:]


def tab_northstar(analyzed=None, raw=None, day=None):
    """独立タブ: 北極星の研究記述(retail_chase / 掲示板×株価連動 / 熱狂指数)。
    ★全て『未検証・記述専用・昇格でない』=別台帳の可視化のみ。シグナル/発注に一切使わない。"""
    _RES = os.path.join(config.BASE_DIR, "research", "_ledger")
    st.markdown("#### 🕵️ 北極星: 小口chase/capitulation & 掲示板×株価連動 (研究)")
    st.warning("**未検証・記述専用・昇格でない**。ここは研究段階の別台帳を可視化するだけで、"
               "シグナル/売買判断には一切使わない。実測結論=**掲示板の単独の方向エッジは無い**"
               "(6軸総当り 2026-07-12)。chase/capit は短期モメンタム整合で"
               "『逆張り天井/底マーカー』としては機能しない。分類器の後段の条件特徴候補に留まる。")

    _euphoria_panel(analyzed, raw, day)

    # --- retail_chase(rc2) 日次サマリ ---
    st.markdown("##### 🐽 小口 chase / capitulation (rc2・10分バケット)")
    st.caption("chase = 上昇中に小口強気がベース比でスパイク(distribute/偽物マーカー候補)。"
               "capit = 下落中に小口が投げ(本物の底マーカー候補)。値は max(dev,0)×max(±R,0)×1e4。"
               "**n_valid<15 は評価不能(記述のみ)。分類器の後段条件特徴で、単独の方向予測には使わない。**")
    drows = [r for r in _load_jsonl_tail(os.path.join(_RES, "retail_chase_285A.jsonl"), 200)
             if r.get("spec") == "rc2" and (r.get("n_valid") or 0) > 0]
    # date後勝ちで最新値に集約
    by_date = {}
    for r in drows:
        by_date[r["date"]] = r
    drows = sorted(by_date.values(), key=lambda r: r["date"])[-15:]
    if drows:
        import pandas as _pd
        df = _pd.DataFrame([{
            "日付": r["date"], "有効n": r.get("n_valid"),
            "max_chase": r.get("max_chase"), "時刻(chase)": r.get("max_chase_time"),
            "max_capit": r.get("max_capit"), "時刻(capit)": r.get("max_capit_time"),
        } for r in drows])
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"最新 {drows[-1]['date']}: chase最大={drows[-1].get('max_chase')}"
                   f"@{drows[-1].get('max_chase_time')} / "
                   f"capit最大={drows[-1].get('max_capit')}@{drows[-1].get('max_capit_time')}。"
                   "※1日の値で断じない(10-20営業日で分布を見る)。")
    else:
        st.info("retail_chase(rc2) の有効日がまだ台帳にありません(calibration中/価格重複待ち)。")

    # --- intraday_linkage 記述相関 ---
    st.markdown("##### 🔗 掲示板×株価 記述連動 (intraday_linkage)")
    st.caption("10分バケットの Spearman。投稿量→次vol / 価格→次投稿(掲示板の遅行) / 強気-弱気→ret。"
               "**記述で昇格でない。単独の方向シグナルにしない。**")
    lrows = _load_jsonl_tail(os.path.join(_RES, "intraday_linkage_285A.jsonl"), 5)
    if lrows:
        last = lrows[-1]
        c1, c2, c3 = st.columns(3)
        c1.metric("投稿量→次vol(弱リード)", _fmt_rho(last.get("ll_posts_lead_absret")))
        c2.metric("価格→次投稿(掲示板の遅行)", _fmt_rho(last.get("ll_price_lead_posts")))
        c3.metric("強気-弱気→同時ret(逆張り)", _fmt_rho(last.get("c_bullbear_ret")))
        st.caption(f"n={last.get('n_intervals')}区間 / {last.get('days')} / {last.get('status')}。"
                   "※n小・自己相関大=方向の目安のみ。真判定は forward-OOS(n≥20)。")
    else:
        st.info("intraday_linkage の台帳がまだありません。")


def _fmt_rho(v):
    try:
        return f"{float(v):+.3f}"
    except (TypeError, ValueError):
        return "—"


def _euphoria_panel(analyzed, raw, day):
    """熱狂(euphoria)指数=『天井の過熱』。小口capitulation(大底の投げ)の対。
    当日の投稿本文を euphoria_score へ渡し score/label/カテゴリ別hits/top_examples を表示。
    read-only・記述用(regex・LLM不使用)。"""
    st.markdown("##### 🚀 熱狂(euphoria)指数 — 天井の過熱（capitulationの対）")
    st.caption("既存の『小口 capitulation＝大底の投げ売り検知』の対＝『天井の熱狂』。"
               "「◯万いく」等の高値目標連呼／買い煽り・陶酔／弱気派の降参をregexで検出(LLM不使用)。"
               "**未検証・記述用**・投資助言でない。")
    if not HAS_EUPHORIA:
        st.caption("euphoria モジュール未ロード。")
        return
    texts = _day_texts(analyzed, raw, day)
    if not texts:
        st.info("当日のコメントがまだありません(蓄積中)。")
        return
    try:
        e = euphoria.euphoria_score(texts)
        lbl = euphoria.label(e["score"])
        examples = euphoria.top_examples(texts, k=5)
    except Exception as ex:
        _fail_note("熱狂指数の算出に失敗しました。", ex)
        return
    sc = e["score"]
    col = COL["red"] if sc >= 60 else (COL["yellow"] if sc >= 40 else COL["green"])
    cols = st.columns([1, 2])
    with cols[0]:
        st.metric("熱狂スコア", f"{sc:.0f}/100",
                  help="0-100。高いほど天井過熱。買い煽り/陶酔を重めに合成した記述指標。")
        st.markdown(chip(lbl, col), unsafe_allow_html=True)
        st.caption(f"対象 n={e['n']}件 / 熱狂ヒット率 {e['ratio']:.0%}")
    with cols[1]:
        h = e.get("hits", {})
        st.caption(f"高値目標連呼 {h.get('price_target_mania', 0)}件 / "
                   f"買い煽り・陶酔 {h.get('buy_mania', 0)}件 / "
                   f"弱気の降参 {h.get('bear_capitulation', 0)}件")
        if examples:
            for cat_ja, exc in examples:
                st.markdown(f"<div class='bbs-panel'><b>{cat_ja}</b>：{_esc(exc)}</div>",
                            unsafe_allow_html=True)
        else:
            st.caption("熱狂語彙のヒットなし(平穏)。")
    st.caption("※1日の値で断じない(10-20営業日で分布)。掲示板は本来『方向』より『ボラ/出来高』を予測。")


def tab_moomoo():
    """独立タブ: moomoo read-only リテール指標(quote専用・発注なし)。"""
    st.markdown("#### 🏦 moomoo リテール指標(read-only・発注なし)")
    st.caption("moomoo OpenD(quote専用)から 小口比率・売買圧力・板圧力・資金分布(大口/小口) を"
               "取得して表示する。研究核「小口主導ほどセンチメントが効く」の入力。"
               "**発注は一切しない**(取引APIは import もしない=tripwire)。")
    if not HAS_MOOMOO_SOURCE:
        st.info("moomoo_source モジュール未ロード。")
        return
    _moomoo_panel()
    with st.expander("接続状態 / 設定"):
        try:
            s = moomoo_source.read_only_summary()
            st.json({k: s.get(k) for k in ("enabled", "sdk_present", "opend_host",
                     "opend_port", "small_lot_shares", "tick_count", "book_last")})
        except Exception as e:
            _fail_note("moomoo接続状態の取得に失敗しました。", e, nested=True)
        st.caption("有効化は env `BBS_MOOMOO_ENABLE=1`(既定OFF)。導入手順=`_handoff\\"
                   "moomoo_skill_インストール指南書_2026-07-09.md`。JP相場は現在メンテ停止中。")


def _moomoo_panel():
    """moomoo read-only(quote専用・発注なし)のリテール指標を表示。
    OFF既定/OpenD未接続/JP相場メンテ中はグレースフルにフォールバック(落ちない)。"""
    if not HAS_MOOMOO_SOURCE:
        return
    st.markdown("##### 📊 指標")
    try:
        s = moomoo_source.read_only_summary()
    except Exception as e:
        _fail_note("moomoo指標の算出に失敗しました。", e)
        return
    n_tick = s.get("tick_count", 0)
    has_data = bool(n_tick or s.get("book_pressure") or s.get("capital_distribution"))
    if not has_data:
        reason = []
        if not s.get("enabled"):
            reason.append("BBS_MOOMOO_ENABLE=0(既定OFF)")
        if not s.get("sdk_present"):
            reason.append("moomoo SDK未検出")
        reason.append("JP相場はプラットフォーム側メンテで停止中(復旧待ち)")
        st.markdown(
            f"<div class='calib'>moomoo未接続/データなし ｜ {' / '.join(reason)}<br>"
            f"<span style='font-size:.9em'>OpenD({s.get('opend_host')}:{s.get('opend_port')})起動＋JP相場権限が"
            f"有効化されると、小口比率・売買圧力・板圧力・資金分布(大口/小口)がここに出ます。発注はしません。</span></div>",
            unsafe_allow_html=True)
        return
    cols = st.columns(3)
    rs = s.get("retail_share")
    cols[0].metric("小口比率(retail)", f"{rs:.0%}" if rs is not None else "-",
                   help=f"約定{s.get('small_lot_shares')}株未満の出来高割合(当日tick n={n_tick})")
    bs = s.get("buy_sell") or {}
    br = bs.get("buy_ratio")
    cols[1].metric("売買圧力(buy比)", f"{br:.0%}" if br is not None else "-")
    bp = s.get("book_pressure") or {}
    bdr = bp.get("bid_ratio")
    cols[2].metric("板圧力(bid比)", f"{bdr:.0%}" if bdr is not None else "-")
    cap = s.get("capital_distribution")
    if cap and cap.get("small"):
        st.caption(
            f"資金分布(net): 小口 {cap['small']['net']:+,.0f} ／ 大口 {cap.get('big', {}).get('net', 0):+,.0f} "
            f"／ 超大口 {cap.get('super', {}).get('net', 0):+,.0f} ｜ 小口純share {cap.get('retail_net_share')}")
    st.caption("read-only・発注なし・別台帳。「小口主導ほどセンチメントが効く」仮説(研究核)の入力。")


def _nine_cards(S, ctrl):
    cards = S.get("cards", [])
    cols = st.columns(3)
    for i, cd in enumerate(cards):
        name = cd["name"]
        state = cd["state"]
        if name == "灼熱メーター(過熱)":
            state = zone_state(cd["value"], 50, ctrl["th_overheat"])
        elif name == "阿鼻叫喚(セリクラ)":
            state = zone_state(cd["value"], config.SIG_CAPITULATION_WARN, ctrl["th_capit"])
        elif name == "そう思う大量票":
            state = zone_state(cd["value"], ctrl["th_votes"] * 0.6, ctrl["th_votes"])
        c = ST_COL.get(state, COL["green"])
        icon = CARD_ICON.get(name, "")
        with cols[i % 3]:
            st.markdown(
                f"<div class='bbs-panel' style='border-left:4px solid {c}'>"
                f"<b>{icon} {name}</b> {chip(state, c)}<br>"
                f"<span style='color:{COL['muted']};font-size:.88em'>{cd['note']} "
                f"| 閾値 {cd['threshold']}</span></div>", unsafe_allow_html=True)


# ============================================================================
# タブ5: コメント ドリルダウン
# ============================================================================
def _resolve_comment_sentiment(raw, analyzed, window=8000):
    """
    表示用コメント集合を【生コメント(raw)ベース】で構築(=新着が即出る)。
    強弱は analyzed(id join)優先→無ければ無料 lexicon(analyze.classify_lexicon)で即補完。
    話題(cluster_label)は analyzed のみ付与。負荷対策で新しい方から window 件に限定。
    """
    an_by_id = {r.get("id"): r for r in analyzed if r.get("id") is not None}
    try:
        import analyze as _an
        _clf = _an.classify_lexicon
    except Exception:
        _clf = None
    recent = raw[-window:] if len(raw) > window else raw
    out = []
    for r in recent:
        a = an_by_id.get(r.get("id"))
        if a and a.get("sentiment"):
            sent, cl = a.get("sentiment"), a.get("cluster_label")
        else:
            sent = _clf(r.get("text") or "", r.get("feel")) if _clf else None
            cl = None
        d = dict(r)
        d["sentiment"] = sent
        d["cluster_label"] = cl
        out.append(d)
    return out


def tab_comments(raw, analyzed, ctrl):
    st.markdown("#### 💬 コメント ドリルダウン")
    st.caption("**生コメント全件ベース(新着が即反映)**。強弱は解析済み優先→無ければ無料lexiconで即分類。"
               "話題クラスタは解析済みのみ付与。負荷対策で直近8,000件を対象。")
    base = _resolve_comment_sentiment(raw, analyzed)
    filt = st.selectbox("シグナル別フィルタ", [
        "(すべて)", "😱 セリクラ語彙", "🚀 イナゴ語彙", "📢 煽り語彙",
        "👍 そう思う多い順", "🔴 弱気のみ", "🟢 強気のみ"])
    rows = base
    if filt == "😱 セリクラ語彙":
        rows = [r for r in base if sig._CAP_RE.search(r.get("text", ""))]
    elif filt == "🚀 イナゴ語彙":
        rows = [r for r in base if sig._EUP_RE.search(r.get("text", ""))]
    elif filt == "📢 煽り語彙":
        rows = [r for r in base if sig._AORI_RE.search(r.get("text", ""))]
    elif filt == "👍 そう思う多い順":
        rows = sorted(base, key=lambda r: -(r.get("votes_yes") or 0))
    elif filt == "🔴 弱気のみ":
        rows = [r for r in base if r.get("sentiment") == "bearish"]
    elif filt == "🟢 強気のみ":
        rows = [r for r in base if r.get("sentiment") == "bullish"]

    kw = st.text_input("キーワード検索", help="本文の部分一致(空欄で無効)")
    if kw.strip():
        kwl = kw.strip().lower()
        rows = [r for r in rows if kwl in (r.get("text", "") or "").lower()]
    labels = sorted({r.get("cluster_label") for r in base if r.get("cluster_label")})
    if labels:
        picked = st.multiselect("話題(クラスタ・解析済みのみ)", labels)
        if picked:
            rows = [r for r in rows if r.get("cluster_label") in picked]
    st.download_button("⬇ 抽出コメント.csv", data=_comments_to_csv(rows),
                       file_name="抽出コメント.csv", mime="text/csv")

    limit = 30 if ctrl["density"] == "コンパクト" else 60
    shown = 0
    for r in (rows if filt == "👍 そう思う多い順" else list(reversed(rows))):
        st.markdown(_comment_card(r), unsafe_allow_html=True)
        shown += 1
        if shown >= limit:
            st.caption(f"({limit}件まで表示)")
            break
    if shown == 0:
        st.caption("該当コメントなし。")


def _comments_to_csv(rows):
    """抽出中コメントをCSV文字列に(列= ts, source, sentiment, votes_yes, cluster_label, text)。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "source", "sentiment", "votes_yes", "cluster_label", "text"])
    for r in rows:
        w.writerow([r.get("ts", ""), r.get("source", ""), r.get("sentiment", ""),
                    r.get("votes_yes", 0), r.get("cluster_label", ""), r.get("text", "")])
    return buf.getvalue()


def _esc(s):
    esc = (s or "").replace("<", "&lt;").replace(">", "&gt;")
    return esc[:400] + "…" if len(esc) > 400 else esc


def _esc_para(s):
    """段落用エスケープ(改行を<br>で保つ・切り詰めなし)。"""
    return (s or "").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


# ============================================================================
# 柱4: 話題トレンド(クラスタ盛衰 + 価格×センチメント連関・記述/screening)
# ============================================================================
def _cluster_trend_section(analyzed, price_d):
    """cluster_trend.cluster_trends / price_sentiment_features を記述表示(fail-soft)。"""
    if not HAS_CLUSTER_TREND:
        return
    st.markdown("##### 🔥 話題トレンド(クラスタ盛衰・記述)")
    try:
        ct = cluster_trend.cluster_trends(analyzed)
    except Exception as e:
        _fail_note("話題トレンドの算出に失敗しました。", e)
        return
    topics = ct.get("topics", {})
    if not topics:
        st.caption("クラスタ蓄積中(ラベル付きコメントがまだ少ない)。")
    else:
        ranked = sorted(topics.items(),
                        key=lambda kv: (-(kv[1].get("momentum") or 0),
                                        -kv[1].get("total", 0)))
        cols = st.columns(3)
        for i, (name, t) in enumerate(ranked[:6]):
            mom = t.get("momentum")
            m = mom or 0
            arrow = "▲" if m > 0 else ("▼" if m < 0 else "―")
            col = COL["red"] if m > 0 else (COL["blue"] if m < 0 else COL["muted"])
            mom_txt = "-" if mom is None else f"{mom:+d}" if isinstance(mom, int) else str(mom)
            span = t.get("first_seen") or "-"
            span_to = t.get("last_seen") or "-"
            with cols[i % 3]:
                st.markdown(
                    f"<div class='bbs-panel' style='border-left:4px solid {col}'>"
                    f"<b>{_esc(name)}</b> {chip(f'{arrow} 勢い {mom_txt}', col)}<br>"
                    f"<span style='color:{COL['muted']};font-size:.85em'>"
                    f"通算 {t.get('total', 0)}件 / {span}〜{span_to}</span></div>",
                    unsafe_allow_html=True)
    try:
        psf = cluster_trend.price_sentiment_features(analyzed, price_d)
    except Exception:
        psf = None
    if psf:
        st.caption(f"価格×センチメント連関(共通日 n={psf.get('n_days', 0)}): "
                   f"{psf.get('note', '')}")


# ============================================================================
# 柱1: AI相場考察タブ(引け後の振り返り・LLM課金はこのボタンのみ・予測助言なし)
# ============================================================================
def tab_insight(S, latest_export, analyzed, snaps):
    st.markdown("#### 🧠 AI相場考察(引け後の振り返り)")
    backend = getattr(config, "BBS_LLM_BACKEND", "lexicon")
    backend_label = llm_backend_label()
    if backend != "lexicon":
        st.caption(f"当日の掲示板センチメントをLLM({backend_label})が散文で振り返る。"
                   "予測・売買助言はしない(振り返りのみ)。"
                   "「生成」を押した時だけLLMを呼ぶ(課金経路=claudeのみ都度課金・ローカルLLMは無料)。")
        btn_label = f"🧠 考察を生成({backend_label})"
        spin_label = f"生成中({backend_label})…"
    else:
        st.caption("当日の掲示板センチメントを機械的に要約(辞書ベース・API課金ゼロ)。"
                   "予測・売買助言はしない(集計値の書き出しのみ)。"
                   "LLM散文が要る場合は環境変数 BBS_LLM_BACKEND=claude/ollama/lemonade で切替。")
        btn_label = "📝 自動要約を生成(辞書ベース・無料)"
        spin_label = "要約中(辞書ベース)…"
    if not HAS_INSIGHT:
        st.info("insight モジュール未ロード。")
        return
    if st.button(btn_label):
        with st.spinner(spin_label):
            try:
                try:
                    rs = moomoo_source.retail_share()
                except Exception:
                    rs = None
                # generate_insight は BBS_USE_LLM=False なら内部でテンプレ要約(API非呼出)へ分岐。
                res = insight.generate_insight(
                    snapshot=snaps[-1] if snaps else None,
                    latest_export=latest_export, analyzed=analyzed,
                    price=(S or {}).get("price"), retail_share=rs)
                if res.get("advice_flagged"):
                    st.warning("助言的表現を検知→注意バナー付きで記録しました。")
                st.markdown(f"<div class='bbs-panel'>{_esc_para(res.get('text', ''))}</div>",
                            unsafe_allow_html=True)
            except Exception as e:
                _fail_note("AI考察の生成に失敗しました。", e, kind="error")

    st.markdown("##### 📜 過去の考察(別台帳 insights.jsonl)")
    try:
        past = insight.load_insights()
    except Exception:
        past = []
    if not past:
        st.caption("まだ生成されていません。上のボタンで生成できます。")
        return
    for rec in reversed(past[-10:]):
        flag = " ⚠️助言検知" if rec.get("advice_flagged") else ""
        st.markdown(
            f"<div class='bbs-panel'><span style='color:{COL['muted']};font-size:.85em'>"
            f"{rec.get('ts', '')} / {rec.get('model', '')} / {rec.get('chars', 0)}字{flag}"
            f"</span><br>{_esc_para(rec.get('text', ''))}</div>",
            unsafe_allow_html=True)


if __name__ == "__main__":
    main()
