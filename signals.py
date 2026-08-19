# -*- coding: utf-8 -*-
"""
signals.py - おにや式(逆張りリテールセンチメント)の定量シグナル群。全て純関数。

【重要・免責】全シグナルは研究用・未検証。損益ゲート(10-20営業日OOS)通過まで
"シグナル"と呼ばず投資判断に使わない。文献(Antweiler&Frank 2004)は掲示板の
方向予測力は僅少・投稿量はボラ/出来高を予測と報告 → 本モジュールは方向予測でなく
「過熱/悲観の極値検知器」として設計。LLM追加コストゼロ(regex/カウントのみ)。
データ源の分離: 量・語彙・速度・サージ・他銘柄混入・ネームド集中 = raw_comments 全件
(化け除外)/ 強気弱気の比率・votes集中・ゲージのsentiment成分 = analyzed(AI判定サンプル)。
"""
import re
import math
import datetime as dt

import config
from collect_yahoo import is_mojibake
import mojibake_cache


# ============================================================================
# 語彙(正規表現)
# ============================================================================
CAPITULATION_WORDS = [
    "損切り", "退場", "追証", "助けて", "終わった", "阿鼻叫喚", "狼狽",
    "ナンピン", "塩漬け", "セリクラ", "もうダメ", "もうだめ",
]
EUPHORIA_WORDS = [
    "🚀", "爆益", "億り", "祭り", "ストップ高", "S高", "月まで",
    "買うしかない", "青天井", "最強",
]
AORI_WORDS = [
    "暴落", "逃げろ", "S安", "オワ", "終わり", "養分", "狼狽売り",
]

_CAP_RE = re.compile("|".join(map(re.escape, CAPITULATION_WORDS)))
_EUP_RE = re.compile("|".join(map(re.escape, EUPHORIA_WORDS)))
_AORI_RE = re.compile("|".join(map(re.escape, AORI_WORDS)))

# 他銘柄混入: 4桁コード(年号1900-2100は除外)+主要銘柄名
_CODE_RE = re.compile(r"(?<![0-9])([1-9][0-9]{3})(?![0-9])")
OTHER_NAMES = [
    "サムスン", "マイクロン", "ハイニックス", "エヌビディア", "NVIDIA",
    "ソフトバンク", "レーザーテック", "アドバンテスト", "東エレ", "ディスコ",
    "ルネサス", "ソシオネクスト", "トヨタ", "SOXL", "サンディスク", "SanDisk",
    "メタプラ", "サンバイオ", "三菱重工",
]
_OTHER_NAME_RE = re.compile("|".join(map(re.escape, OTHER_NAMES)), re.I)
SELF_TERMS = ["285A", "キオクシア", "kioxia", "KIOXIA", "Kioxia"]


# ============================================================================
# 基本ヘルパ(純関数)
# ============================================================================
def _meaningful(rows):
    return [r for r in rows if r.get("meaningful")]


def clean_rows(rows):
    """化け(mojibake)を除いた行。生データ(raw)集計の前処理。
    analyze済み行は 'garbled' フラグがあればそれも尊重。
    mojibake判定は id 単位の永続キャッシュ(mojibake_cache)経由で引く
    (同じ id のtextは書込み後不変=一度計算すれば再計算不要・cProfile実測で
    59万件規模の全件再計算が compute_signals() の1/3を占めていた対策)。
    id が無い行はキャッシュに乗らないだけで、従来通りその場で計算する
    (fail-soft・処理は継続)。"""
    rows = rows or []
    cache_hits = mojibake_cache.get_or_compute(rows)
    out = []
    for r in rows:
        if r.get("garbled"):
            continue
        rid = r.get("id")
        if rid is None:
            mojibake = is_mojibake(r.get("text", ""))
        else:
            mojibake = cache_hits.get(str(rid))
            if mojibake is None:
                mojibake = is_mojibake(r.get("text", ""))
        if mojibake:
            continue
        out.append(r)
    return out


def volume_per_hour(rows, day=None):
    """当日の生投稿の平均速度(posts/hour)。アクティブ時間=バケット数で割る。純関数。"""
    day = day or dt.date.today().isoformat()
    per = {}
    for r in rows:
        ts = r.get("ts") or ""
        if ts[:10] == day and len(ts) >= 13:
            per[ts[11:13]] = per.get(ts[11:13], 0) + 1
    if not per:
        return 0.0
    return round(sum(per.values()) / len(per), 1)


def clip01(x):
    """0-1へクリップ。None/非数は None。"""
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


def resolved_sentiment(row):
    """解決済みセンチメント(純関数・読み取り専用)。

    投稿者の自己申告(Yahoo!掲示板「投稿者の気持ち」= feel フィールド)を
    AI/LLM判定(sentiment フィールド)より優先する:
      - feel が strong/strongest(強気側) -> "bullish"(sentimentに関わらず)
      - feel が weak/weakest(弱気側)     -> "bearish"(sentimentに関わらず)
      - feel が both(様子見)、無し、未知値 -> AI/LLM判定(sentiment)へフォールバック

    row["sentiment"] 自体は一切変更しない(このロジックはあくまで表示/集計用の
    解決値を返すだけで、analyzed.jsonl 等の永続フィールドには触れない)。
    """
    feel = row.get("feel")
    if feel in ("strong", "strongest"):
        return "bullish"
    if feel in ("weak", "weakest"):
        return "bearish"
    return row.get("sentiment")


def sentiment_ratios(rows):
    """meaningful行の bull/bear/neutral 比率。空なら全てNone。
    強気/弱気の判定は resolved_sentiment() 経由(自己申告優先・AI判定フォールバック)。"""
    m = _meaningful(rows)
    n = len(m)
    if n == 0:
        return {"n": 0, "bull_ratio": None, "bear_ratio": None, "neutral_ratio": None}
    b = sum(1 for r in m if resolved_sentiment(r) == "bullish")
    br = sum(1 for r in m if resolved_sentiment(r) == "bearish")
    return {
        "n": n,
        "bull_ratio": round(b / n, 3),
        "bear_ratio": round(br / n, 3),
        "neutral_ratio": round((n - b - br) / n, 3),
    }


def hourly_buckets(rows, day=None):
    """
    投稿ts基準の1時間バケット(当日)。戻り値: {"HH": {total,bullish,bearish,neutral}}
    ts欠損行は無視。純関数。
    """
    day = day or dt.date.today().isoformat()
    out = {}
    for r in rows:
        ts = r.get("ts") or ""
        if ts[:10] != day or len(ts) < 13:
            continue
        hh = ts[11:13]
        b = out.setdefault(hh, {"total": 0, "bullish": 0, "bearish": 0, "neutral": 0})
        b["total"] += 1
        if r.get("meaningful"):
            s = resolved_sentiment(r) or "neutral"
            if s in ("bullish", "bearish", "neutral"):
                b[s] += 1
    return out


def posts_per_hour(rows, day=None):
    """当日の平均投稿速度(posts/hour)。アクティブ時間=バケット数で割る。"""
    buckets = hourly_buckets(rows, day)
    if not buckets:
        return 0.0
    total = sum(b["total"] for b in buckets.values())
    return round(total / len(buckets), 1)


def daily_counts(rows):
    """ts日付別の投稿数 {date: n}(全履歴)。ts欠損は無視。純関数。"""
    out = {}
    for r in rows:
        ts = r.get("ts") or ""
        if len(ts) >= 10:
            d = ts[:10]
            out[d] = out.get(d, 0) + 1
    return out


def trailing_zscore(series, window=None):
    """
    値リストの最後の要素の trailing z-score(直前window個に対して)。
    標本不足(<5)や分散0は None。純関数。
    """
    window = window or config.SIG_ZSCORE_WINDOW
    if not series or len(series) < 2:
        return None
    hist = series[:-1][-window:]
    if len(hist) < 5:
        return None
    mean = sum(hist) / len(hist)
    var = sum((x - mean) ** 2 for x in hist) / len(hist)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round((series[-1] - mean) / sd, 2)


# ============================================================================
# 語彙カウンタ(純関数)
# ============================================================================
def lexicon_counts(texts):
    """
    テキスト群の語彙該当(該当語を1つ以上含む投稿数)と件/100投稿の指数。
    戻り値: {capitulation:{hits,index}, euphoria:{...}, aori:{...}, n}
    """
    n = len(texts)
    cap = sum(1 for t in texts if t and _CAP_RE.search(t))
    eup = sum(1 for t in texts if t and _EUP_RE.search(t))
    aori = sum(1 for t in texts if t and _AORI_RE.search(t))

    def idx(h):
        return round(h / n * 100.0, 1) if n else 0.0

    return {
        "n": n,
        "capitulation": {"hits": cap, "index": idx(cap)},
        "euphoria": {"hits": eup, "index": idx(eup)},
        "aori": {"hits": aori, "index": idx(aori)},
    }


def other_symbol_ratio(texts):
    """
    285A/キオクシア以外の銘柄(4桁コード or 主要銘柄名)への言及を含む投稿比率。
    年号(1900-2100)の4桁は除外。純関数。
    """
    n = len(texts)
    if n == 0:
        return {"n": 0, "hits": 0, "ratio": 0.0}
    hits = 0
    for t in texts:
        if not t:
            continue
        found = False
        for m in _CODE_RE.finditer(t):
            code = int(m.group(1))
            if 1900 <= code <= 2100:
                continue  # 年号らしき数字
            found = True
            break
        if not found and _OTHER_NAME_RE.search(t):
            found = True
        if found:
            hits += 1
    return {"n": n, "hits": hits, "ratio": round(hits / n, 3)}


# ============================================================================
# votes / author 集中(純関数)
# ============================================================================
def votes_concentration(rows):
    """
    「そう思う」集中。bullishコメ総votes_yes / bearishコメ総votes_yes(比率)、
    bullish単一コメの最大votes_yes、bearish側votesシェア。meaningfulのみ。
    強気/弱気の判定は resolved_sentiment() 経由(自己申告優先・AI判定フォールバック)。
    """
    m = _meaningful(rows)
    bull_votes = sum(int(r.get("votes_yes") or 0) for r in m if resolved_sentiment(r) == "bullish")
    bear_votes = sum(int(r.get("votes_yes") or 0) for r in m if resolved_sentiment(r) == "bearish")
    max_bull = max((int(r.get("votes_yes") or 0) for r in m if resolved_sentiment(r) == "bullish"),
                   default=0)
    ratio = round(bull_votes / bear_votes, 2) if bear_votes > 0 else (
        float(bull_votes) if bull_votes else None)
    total = bull_votes + bear_votes
    bear_share = round(bear_votes / total, 3) if total else None
    return {
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
        "bull_bear_ratio": ratio,
        "max_bullish_votes": max_bull,
        "bear_votes_share": bear_share,
    }


def named_concentration(rows, min_n=None):
    """
    投稿者別件数の上位5シェア(仕込み/操作ゲート用)。n<min_n(既定30)は None(非表示)。
    投稿者名は author を優先し、無ければ user を使う。yahoo HTMLフォールバックや一部の
    海外ソースは author(ハッシュID)を持たず user のみ持つため、author だけで集計すると
    生行の約2割を名前集計から取りこぼす(BVPの操作ゲート計数が過小になる)。
    """
    min_n = min_n or config.SIG_NAMED_MIN_N
    counts = {}
    for r in rows:
        a = r.get("author") or r.get("user")
        if a:
            counts[a] = counts.get(a, 0) + 1
    n = sum(counts.values())
    if n < min_n:
        return {"n": n, "top5_share": None, "top_authors": []}
    top = sorted(counts.items(), key=lambda x: -x[1])[:5]
    share = round(sum(c for _, c in top) / n, 3)
    return {"n": n,
            "top5_share": share,
            "top_authors": [{"author": str(a)[:12], "count": c} for a, c in top]}


# ============================================================================
# 価格ヘルパ(純関数)
# ============================================================================
def low_zone(daily_bars, current_price, days=20, pct=None):
    """
    現値が直近days日安値の±pct%圏か。daily_bars=[{close,low,...}](古→新)。
    データ不足/価格無しは None。純関数。
    """
    pct = config.SIG_LOW_ZONE_PCT if pct is None else pct
    if not daily_bars or current_price is None:
        return None
    lows = [b.get("low") or b.get("close") for b in daily_bars[-days:]
            if (b.get("low") or b.get("close")) is not None]
    if not lows:
        return None
    lo = min(lows)
    return abs(current_price - lo) / lo * 100.0 <= pct


# ============================================================================
# おにや複合ゲージ(純関数)
# ============================================================================
def _z_to01(z, fallback=None):
    """z-score -> 0-1(z=0->0, z>=3->1)。zがNoneならfallback(絶対値正規化)。"""
    if z is not None:
        return clip01(z / 3.0)
    return fallback


def oniya_gauges(*, ratios, lex_today, votes, posts_z, cap_z, eup_z,
                 in_low_zone, calib_days, price_change_pct=None):
    """
    過熱スコア/阿鼻叫喚スコア(0-100)と状態判定。全入力は他の純関数の出力。
    履歴不足(calib_days < SIG_MIN_CALIB_DAYS)は絶対閾値フォールバックで暫定判定し
    calibrating=True を返す。

    ★2026-08-19 おにやキャリブレーション(15:43投稿): 阿鼻叫喚の安値圏(in_low_zone)
    成分は「当日騰落率ベースの連続値c_shock」へ置換。実測比較(大変動日10日)で
    旧c_low(0/1のAND要件)は0/10発火だったのに対しc_shockは9/10(90%)発火に改善。
    in_low_zoneは発火判定から外れ表示専用(build_signal_cardsの注記)として残る。
    """
    calibrating = (calib_days or 0) < config.SIG_MIN_CALIB_DAYS

    # --- 過熱(0-1成分; Noneは除外) ---
    eup_abs = clip01((lex_today.get("euphoria", {}).get("index") or 0) / 20.0)
    c_eup = _z_to01(eup_z, fallback=eup_abs)
    bull = ratios.get("bull_ratio")
    c_bull = clip01((bull - 0.5) / 0.35) if bull is not None else None
    r = votes.get("bull_bear_ratio")
    c_ratio = clip01((r - 1.0) / 4.0) if isinstance(r, (int, float)) else None
    c_maxv = clip01(votes.get("max_bullish_votes", 0) / config.SIG_ONIYA_VOTES_MAX)
    c_votes = max(x for x in [c_ratio or 0.0, c_maxv or 0.0]) if (c_ratio is not None or c_maxv is not None) else None
    c_surge = _z_to01(posts_z, fallback=0.0)
    heat_parts = [x for x in (c_eup, c_bull, c_votes, c_surge) if x is not None]
    overheat = round(sum(heat_parts) / len(heat_parts) * 100.0, 1) if heat_parts else 0.0

    # --- 阿鼻叫喚(0-1成分) ---
    cap_abs = clip01((lex_today.get("capitulation", {}).get("index") or 0) / 20.0)
    c_cap = _z_to01(cap_z, fallback=cap_abs)
    bear = ratios.get("bear_ratio")
    c_bear = clip01((bear - 0.5) / 0.35) if bear is not None else None
    bs = votes.get("bear_votes_share")
    c_bvotes = clip01((bs - 0.5) / 0.4) if bs is not None else None
    if price_change_pct is None:
        c_shock = None
    else:
        span = config.SIG_CAP_SHOCK_FULL_PCT - config.SIG_CAP_SHOCK_MIN_PCT
        c_shock = clip01((-price_change_pct - config.SIG_CAP_SHOCK_MIN_PCT) / span)
    cap_parts = [x for x in (c_cap, c_bear, c_bvotes, c_shock) if x is not None]
    capitulation = round(sum(cap_parts) / len(cap_parts) * 100.0, 1) if cap_parts else 0.0

    # --- 状態判定(★安値圏ANDは撤廃・c_shockが阿鼻叫喚スコアに直接反映される) ---
    if capitulation > config.SIG_CAPITULATION_FIRE:
        state = "セリクラ(逆張り買い候補ゾーン)"
    elif overheat > config.SIG_OVERHEAT_TH:
        state = "過熱警戒"
    elif capitulation > config.SIG_CAPITULATION_WARN:
        state = "セリクラ接近"
    else:
        state = "中立"
    if calibrating:
        state += f"(較正中 n={calib_days or 0}日)"

    return {
        "overheat": overheat,
        "capitulation": capitulation,
        "state": state,
        "calibrating": calibrating,
        "calib_days": calib_days or 0,
        "components": {
            "heat": {"euphoria": c_eup, "bull_extreme": c_bull,
                     "votes_conc": c_votes, "post_surge": c_surge},
            "cap": {"capitulation_lex": c_cap, "bear_extreme": c_bear,
                    "bear_votes": c_bvotes, "shock": c_shock},
        },
    }


# ============================================================================
# 統合(オーケストレーション・引数は台帳データのみ=純関数)
# ============================================================================
def compute_signals(analyzed_rows, raw_rows=None, price_daily=None,
                    price_intraday=None, day=None):
    """
    当日の全シグナル一式。ネット非依存の純関数。

    データ源の分離(おにや式の要):
      - 【量・語彙・速度・サージ・話題枯れ・他銘柄混入・ネームド集中】= raw_rows
        (生データ全件・化け除外)。regex/カウントのみでLLM不要 → 全件で真値。
      - 【強気/弱気の比率・votes集中・ゲージのsentiment成分】= analyzed_rows
        (AI判定済みのサンプル)。比率は代表標本でも妥当。
    raw_rows=None なら analyzed_rows を量の源に流用(後方互換)。
    戻り値dictは snapshot 保存/ダッシュボード表示にそのまま使う。
    """
    day = day or dt.date.today().isoformat()

    # ---- 生データ(全件・化け除外) = 量/語彙の源 ----
    raw = clean_rows(raw_rows if raw_rows is not None else analyzed_rows)
    raw_today = [r for r in raw if (r.get("ts") or "")[:10] == day]
    raw_texts_today = [r.get("text", "") for r in raw_today]
    true_volume = len(raw_today)

    # ---- analyzed(AI判定済みサンプル) = 比率/votesの源 ----
    today = [r for r in analyzed_rows if (r.get("ts") or "")[:10] == day]

    ratios = sentiment_ratios(today)                 # 強弱比 <- analyzed
    votes = votes_concentration(today)               # そう思う集中 <- analyzed
    lex_today = lexicon_counts(raw_texts_today)      # 語彙 <- raw全件
    named = named_concentration(raw_today)           # ネームド集中 <- raw全件
    other = other_symbol_ratio(raw_texts_today)      # 他銘柄混入 <- raw全件
    buckets = hourly_buckets(today, day)             # 積み上げの強弱中割 <- analyzed
    raw_hourly = raw_hourly_volume(raw, day)         # 量の主指標 <- raw
    pph = volume_per_hour(raw, day)                  # 投稿速度 <- raw全件

    # ---- 日次系列(投稿数・語彙指数)から trailing z ----
    # 生データ全件。疎な日(カバレッジ<SIG_MIN_DAY_COVERAGE)は基準日から除外。
    dcounts = daily_counts(raw)
    cov_days = sorted(d for d, n in dcounts.items()
                      if n >= config.SIG_MIN_DAY_COVERAGE and d <= day)
    calib_days = len(cov_days)
    counts_series = [dcounts[d] for d in cov_days]
    posts_z = trailing_zscore(counts_series)

    # 日別語彙指数系列(生データ・カバレッジ日のみ)
    cap_series, eup_series = [], []
    for d in cov_days:
        dtexts = [r.get("text", "") for r in raw if (r.get("ts") or "")[:10] == d]
        lx = lexicon_counts(dtexts)
        cap_series.append(lx["capitulation"]["index"])
        eup_series.append(lx["euphoria"]["index"])
    cap_z = trailing_zscore(cap_series)
    eup_z = trailing_zscore(eup_series)

    # ---- 価格 ----
    price = prev = chg = None
    in_low = None
    if price_intraday:
        from price_fetch import latest_price_change
        price, prev, chg = latest_price_change(price_intraday)
        # ★2026-08-19 おにや15:43投稿(データ品質バグ): price_intradayは非取引日
        # (週末等)は更新されず直近取引日の値のまま据え置かれる。そのままchgを使うと
        # 非取引日が連続して「前日終値比%」が凍結表示され続ける(例: 07-18〜20が
        # 07-17の-16.10%を保持)。価格データの最終バーの日付(JST)がasof_date(day)と
        # 一致しない場合はchgのみNoneにする(price/prevは参考表示として残す)。
        bars = price_intraday.get("bars") or []
        if bars and bars[-1].get("ts") is not None:
            last_date = (dt.datetime.utcfromtimestamp(bars[-1]["ts"])
                         + dt.timedelta(hours=9)).date().isoformat()
            if last_date != day:
                chg = None
    if price_daily and price is not None:
        in_low = low_zone(price_daily.get("bars") or [], price)

    gauges = oniya_gauges(ratios=ratios, lex_today=lex_today, votes=votes,
                          posts_z=posts_z, cap_z=cap_z, eup_z=eup_z,
                          in_low_zone=in_low, calib_days=calib_days,
                          price_change_pct=chg)

    cards = build_signal_cards(ratios=ratios, lex=lex_today, votes=votes,
                               named=named, other=other, posts_z=posts_z,
                               gauges=gauges, pph=pph, in_low_zone=in_low)

    return {
        "day": day,
        "true_volume": true_volume,          # 当日の生投稿総数(全件)
        "analyzed_today": len(today),        # うちAI判定済み(サンプル)
        "ratios": ratios,
        "lexicon": lex_today,
        "votes": votes,
        "named": named,
        "other_symbols": other,
        "posts_per_hour": pph,
        "posts_z": posts_z,
        "cap_z": cap_z,
        "eup_z": eup_z,
        "hourly": buckets,
        "raw_hourly": raw_hourly,
        "price": {"last": price, "prev_close": prev, "change_pct": chg,
                  "in_low_zone": in_low},
        "gauges": gauges,
        "cards": cards,
        "calib_days": calib_days,
    }


def raw_hourly_volume(rows, day=None):
    """当日の生投稿の1時間バケット件数 {"HH": count}。量の主指標。純関数。"""
    day = day or dt.date.today().isoformat()
    out = {}
    for r in rows:
        ts = r.get("ts") or ""
        if ts[:10] == day and len(ts) >= 13:
            out[ts[11:13]] = out.get(ts[11:13], 0) + 1
    return out


def build_signal_cards(*, ratios, lex, votes, named, other, posts_z, gauges,
                       pph, in_low_zone):
    """9シグナルのカード(現在値/閾値/状態/1行根拠)。純関数。"""
    def card(name, value, threshold, state, note):
        return {"name": name, "value": value, "threshold": threshold,
                "state": state, "note": note}

    cards = []
    # 1 灼熱メーター過熱
    oh = gauges["overheat"]
    st = ("発火" if oh > config.SIG_OVERHEAT_TH
          else ("警戒" if oh > config.SIG_OVERHEAT_WARN else "OK"))
    cards.append(card("灼熱メーター(過熱)", oh, f">{config.SIG_OVERHEAT_TH}", st,
                      f"過熱スコア{oh} (閾値{config.SIG_OVERHEAT_TH})"))
    # 2 そう思う大量票
    mv = votes.get("max_bullish_votes", 0)
    st = ("発火" if mv >= config.SIG_ONIYA_VOTES_MAX
          else ("警戒" if mv >= config.SIG_ONIYA_VOTES_WARN else "OK"))
    cards.append(card("そう思う大量票", mv, f">={config.SIG_ONIYA_VOTES_MAX}", st,
                      f"そう思う最大{mv}票 ({config.SIG_ONIYA_VOTES_MAX}超=過熱)"))
    # 3 🚀イナゴ語彙
    ei = lex["euphoria"]["index"]
    st = ("発火" if ei >= config.SIG_EUPHORIA_FIRE
          else ("警戒" if ei >= config.SIG_EUPHORIA_WARN else "OK"))
    cards.append(card("イナゴ語彙(euphoria)", ei, f">={config.SIG_EUPHORIA_FIRE}/100投稿", st,
                      f"euphoria指数{ei}/100投稿"))
    # 4 ネームド集中
    ts5 = named.get("top5_share")
    if ts5 is None:
        cards.append(card("ネームド集中", None, f">={config.SIG_NAMED_FIRE}",
                          "OK", f"標本不足(n={named.get('n',0)}<{config.SIG_NAMED_MIN_N})=非表示"))
    else:
        st = ("発火" if ts5 >= config.SIG_NAMED_FIRE
              else ("警戒" if ts5 >= config.SIG_NAMED_WARN else "OK"))
        cards.append(card("ネームド集中", ts5, f">={config.SIG_NAMED_FIRE}", st,
                          f"上位5人シェア{ts5:.0%}"))
    # 5 他銘柄混入
    orr = other.get("ratio", 0.0)
    st = ("発火" if orr >= config.SIG_OTHER_FIRE
          else ("警戒" if orr >= config.SIG_OTHER_WARN else "OK"))
    cards.append(card("他銘柄混入率", orr, f">={config.SIG_OTHER_FIRE}", st,
                      f"他銘柄言及{orr:.0%} (関心離散のサイン)"))
    # 6 暴落煽り
    ai = lex["aori"]["index"]
    st = ("発火" if ai >= config.SIG_AORI_FIRE
          else ("警戒" if ai >= config.SIG_AORI_WARN else "OK"))
    cards.append(card("暴落煽り語彙", ai, f">={config.SIG_AORI_FIRE}/100投稿", st,
                      f"煽り指数{ai}/100投稿"))
    # 7 阿鼻叫喚セリクラ
    # ★2026-08-19 おにやキャリブレーション: 安値圏(in_low_zone)とのAND要件を撤廃。
    # capitulationスコア自体に当日騰落率ベースのc_shockが直接反映されるため、
    # in_low_zoneは注記表示のみに使う(発火判定には使わない)。
    cp = gauges["capitulation"]
    st = ("発火" if cp > config.SIG_CAPITULATION_FIRE
          else ("警戒" if cp > config.SIG_CAPITULATION_WARN else "OK"))
    cards.append(card("阿鼻叫喚(セリクラ)", cp,
                      f">{config.SIG_CAPITULATION_FIRE}", st,
                      f"阿鼻叫喚スコア{cp}"
                      + (" 安値圏" if in_low_zone else " 安値圏外" if in_low_zone is not None else " 価格無し")))
    # 8 話題枯れ
    if posts_z is None:
        cards.append(card("話題枯れ", None, "z<-1", "OK", "履歴不足(較正中)"))
    else:
        st = "発火" if posts_z < -1.0 else ("警戒" if posts_z < -0.5 else "OK")
        cards.append(card("話題枯れ", posts_z, "z<-1", st,
                          f"投稿数z={posts_z} (負=閑散)"))
    # 9 投稿サージ
    if posts_z is None:
        cards.append(card("投稿サージ", None, "z>2", "OK", "履歴不足(較正中)"))
    else:
        st = "発火" if posts_z > 2.0 else ("警戒" if posts_z > 1.0 else "OK")
        cards.append(card("投稿サージ", posts_z, "z>2", st,
                          f"投稿数z={posts_z} (量→ボラ予測が文献の主結果)"))
    return cards
