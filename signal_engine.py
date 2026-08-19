# -*- coding: utf-8 -*-
"""
signal_engine.py - BVP(BoardVolPressure)+ vol/range スコア + regime + contrarian候補
+ 較正/confidence + export レコード組立。全て純関数(入力=台帳データのみ・ネット非依存)。

【重要・規律】研究用・未検証。方向は前向きOOS限定で断定しない(direction_candidate.status
は常に "未検証")。cross-day z は dense_session(取引時間バケット>=SIG_MIN_HOUR_BUCKETS かつ
投稿>=SIG_MIN_DAY_COVERAGE)が SIG_DENSE_MIN_CALIB 未満のうちは抑制(=None・calibrating)。
=> posts_z=69 のようなアーティファクトを構造的に根絶する。

リーク厳禁: robust_z/pct_rank の窓は採点対象セッションを除外。翌セッションを予測する設計で、
同日カウントから同日realized volを作らない。
"""
import re
import math
import hashlib
import datetime as dt

import config
from collect_yahoo import is_mojibake

# gauges.state 末尾の較正カウント "(較正中 n=NN日)" を検出(半角/全角括弧の両方に寛容)。
# signals.py 側 calib_days は「非dense(SIG_MIN_DAY_COVERAGEのみ)」= dense計数と二重基準になるため、
# export/凍結行では dense_count に揃える(honest-n設計を出力まで貫く)。
_CALIB_SUFFIX_RE = re.compile(r"[（(]較正中 n=\d+日[）)]\s*$")

DISCLAIMER = ("research-only; unvalidated (未検証); not investment advice; "
              "no order routing; vol/range filter & sizer only, never a direction oracle.")


# ============================================================================
# カバレッジ / 較正(honest-n fix)
# ============================================================================
def _clean(rows):
    out = []
    for r in rows or []:
        if r.get("garbled"):
            continue
        if is_mojibake(r.get("text", "")):
            continue
        out.append(r)
    return out


def dense_session_dates(raw_rows, upto_day, *, min_posts=None, min_buckets=None):
    """
    「密なセッション」の日付リスト(採点日 upto_day は除外=in-sample正規化リーク防止)。
    密 = 投稿数>=SIG_MIN_DAY_COVERAGE かつ 取引時間バケット(09..15の別時)>=SIG_MIN_HOUR_BUCKETS。
    純関数。
    """
    min_posts = config.SIG_MIN_DAY_COVERAGE if min_posts is None else min_posts
    min_buckets = config.SIG_MIN_HOUR_BUCKETS if min_buckets is None else min_buckets
    trading = set(config.TRADING_HOURS)
    per = {}
    for r in _clean(raw_rows):
        ts = r.get("ts") or ""
        if len(ts) < 13:
            continue
        d = ts[:10]
        if d >= upto_day:
            continue
        hh = int(ts[11:13]) if ts[11:13].isdigit() else -1
        e = per.setdefault(d, {"posts": 0, "buckets": set()})
        e["posts"] += 1
        if hh in trading:
            e["buckets"].add(hh)
    dense = [d for d, e in per.items()
             if e["posts"] >= min_posts and len(e["buckets"]) >= min_buckets]
    return sorted(dense)


def calibration_status(dense_count):
    """('calibrated'|'calibrating', ramp_weight)。ramp=min(1, dense/SIG_MIN_CALIB_DAYS)。"""
    ramp = min(1.0, (dense_count or 0) / max(1, config.SIG_MIN_CALIB_DAYS))
    status = "calibrated" if (dense_count or 0) >= config.SIG_MIN_CALIB_DAYS else "calibrating"
    return status, round(ramp, 3)


# ============================================================================
# 頑健正規化(凍結窓・採点セッションを除外)
# ============================================================================
def robust_z(series, window=None, *, winsor=None):
    """
    (x_last - median_w)/(1.4826*MAD_w)。窓 w は x_last を除いた直近window個。winsor済み。
    使える点が SIG_DENSE_MIN_CALIB 未満 or MAD==0 なら None(=較正中)。純関数。
    重い裾のカウントは呼ぶ前に log1p 済みで渡すこと。
    """
    window = config.SIG_ZSCORE_WINDOW if window is None else window
    winsor = config.BVP_WINSOR_Z if winsor is None else winsor
    if not series or len(series) < 2:
        return None
    hist = series[:-1][-window:]
    if len(hist) < config.SIG_DENSE_MIN_CALIB:
        return None
    med = _median(hist)
    mad = _median([abs(x - med) for x in hist])
    if mad == 0:
        return None
    z = (series[-1] - med) / (1.4826 * mad)
    return round(max(-winsor, min(winsor, z)), 3)


def pct_rank(series):
    """最後の要素の trailing percentile-rank(0-1)。歪んだ量に頑健。純関数。
    使える点が SIG_DENSE_MIN_CALIB 未満なら None。"""
    if not series or len(series) < 2:
        return None
    hist = series[:-1]
    if len(hist) < config.SIG_DENSE_MIN_CALIB:
        return None
    x = series[-1]
    below = sum(1 for v in hist if v <= x)
    return round(below / len(hist), 3)


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


# ============================================================================
# 特徴の disagreement(同日値・zでない)
# ============================================================================
def disagreement(ratios):
    """強気/弱気の割れ具合[0,1]。0=一方向, 1=完全に拮抗。n無/一方向のみ None。純関数。"""
    b = ratios.get("bull_ratio")
    r = ratios.get("bear_ratio")
    if b is None or r is None or (b + r) == 0:
        return None
    hi, lo = max(b, r), min(b, r)
    return round(lo / hi, 3) if hi > 0 else None


# ============================================================================
# BVP(非方向・一次プリミティブ)
# ============================================================================
def compute_bvp(feats, *, weights=None):
    """
    z/pct-rank の [post_volume_surprise, velocity_surge, lexicon_intensity,
    sentiment_disagreement] の等加重ブレンド(forward-OOS refit まで等加重で凍結)。
    named_top5_share 高で volume/velocity を減衰(仕込み操作ゲート)、
    other_symbol_ratio で逆減衰。純関数。
    戻り値 {'bvp': float|None, 'parts': {...}, 'gated_by': [...], 'calibrating': bool}。
    feats の各成分は 0-1 正規化済み or None(None は較正中扱い)。
    """
    parts = {
        "post_volume_surprise": feats.get("post_volume_surprise"),
        "velocity_surge": feats.get("velocity_surge"),
        "lexicon_intensity": feats.get("lexicon_intensity"),
        "sentiment_disagreement": feats.get("sentiment_disagreement"),
    }
    gated = []
    # 操作ゲート: ネームド集中が高いと volume/velocity を割り引く
    top5 = feats.get("named_top5_share")
    if top5 is not None and top5 >= 0.5:
        for k in ("post_volume_surprise", "velocity_surge"):
            if parts[k] is not None:
                parts[k] *= 0.5
        gated.append("named_concentration")
    # 他銘柄混入が高い=関心が離散=BVPを逆減衰
    osr = feats.get("other_symbol_ratio")
    damp = 1.0
    if osr is not None and osr > 0:
        damp = max(0.5, 1.0 - min(0.5, osr))
        if osr >= 0.3:
            gated.append("other_symbol_dilution")

    usable = [v for v in parts.values() if v is not None]
    # 4成分のうち過半が None(=z較正未了)なら BVP は出さない
    if len(usable) < 3:
        return {"bvp": None, "parts": parts, "gated_by": gated, "calibrating": True}

    w = weights or config.BVP_FEATURE_WEIGHTS
    if w:
        num = sum(w.get(k, 0) * v for k, v in parts.items() if v is not None)
        den = sum(w.get(k, 0) for k, v in parts.items() if v is not None)
        base = num / den if den else None
    else:
        base = sum(usable) / len(usable)   # 等加重
    if base is None:
        return {"bvp": None, "parts": parts, "gated_by": gated, "calibrating": True}
    bvp = round(max(0.0, min(1.0, base * damp)), 3)
    return {"bvp": bvp, "parts": parts, "gated_by": gated, "calibrating": False}


def classify_regime(bvp, bvp_history):
    """
    'calm'|'normal'|'elevated'|'extreme' を per-symbol rolling percentile(p50/p80/p95)で。
    history が SIG_DENSE_MIN_CALIB 未満 or bvp None なら 'calibrating'。純関数。
    """
    if bvp is None or not bvp_history or len(bvp_history) < config.SIG_DENSE_MIN_CALIB:
        return "calibrating"
    cuts = config.BVP_REGIME_PCT
    p = _percentile_of(bvp_history, bvp)
    if p >= cuts["extreme"]:
        return "extreme"
    if p >= cuts["elevated"]:
        return "elevated"
    if p >= cuts["normal"]:
        return "normal"
    return "calm"


def _percentile_of(hist, x):
    if not hist:
        return 0.0
    below = sum(1 for v in hist if v <= x)
    return below / len(hist) * 100.0


# ============================================================================
# エクスポートスコア
# ============================================================================
def vol_regime_score(bvp):
    """== BVP(0=静穏,1=極端)。None=較正中。"""
    return bvp


def state_dense_honest(gauge_state, dense_count, calibrating):
    """
    gauges.state の較正カウント "(較正中 n=NN日)" を dense session 計数へ揃える(honest-n)。
    signals.py 側 calib_days は非dense(SIG_MIN_DAY_COVERAGEのみ)なので、export/凍結行の
    state 表示は dense_count(calib_days と同じ正本)に統一して二重基準を根絶する。
    calibrating(dense基準)でなければ較正サフィックスを付けない。純関数。
    """
    base = _CALIB_SUFFIX_RE.sub("", gauge_state or "").rstrip()
    if calibrating:
        return f"{base}(較正中 n={dense_count or 0}日)"
    return base


def range_day_score(feats, bvp_parts):
    """
    トピック枯れ/チョップ期待: posts_z<0 + 低 true_volume + 低 語彙強度。
    1 - vol_regime_score ではない。posts_z が None(較正中)なら None。純関数。
    ※ 呼び出し側は dense-honest な posts_z(dense未了なら None)を渡すこと。生の
      sig.posts_z(=posts_z=69アーティファクト源)を素通しさせない(vol_regime_score と整合)。
    """
    posts_z = feats.get("posts_z")
    if posts_z is None:
        return None
    # posts_z が負(閑散)ほど・語彙強度が低いほど range 期待↑
    quiet = max(0.0, min(1.0, (-posts_z) / 2.0))          # z=-2 で 1.0
    lex_intensity = bvp_parts.get("lexicon_intensity")
    calm_lex = (1.0 - lex_intensity) if lex_intensity is not None else 0.5
    score = 0.6 * quiet + 0.4 * calm_lex
    return round(max(0.0, min(1.0, score)), 3)


# ============================================================================
# contrarian(二次・未検証・ログ専用)
# ============================================================================
def direction_candidate(gauges, feats):
    """
    {'side','status':'未検証','strength','rationale'}。fade_down は
    capitulation>FIRE かつ in_low_zone かつ 低 top5_share(=広範なパニック・仕込みでない)。
    単独routeは絶対しない=strength のみ。純関数。
    """
    overheat = gauges.get("overheat") or 0
    cap = gauges.get("capitulation") or 0
    in_low = feats.get("in_low_zone")
    top5 = feats.get("named_top5_share")
    orchestrated = (top5 is not None and top5 >= 0.5)

    side, strength, why = "none", None, "極値なし"
    if cap > config.SIG_CAPITULATION_FIRE and in_low and not orchestrated:
        side = "fade_down"   # 総悲観の底=下げをフェード(=買い候補)
        strength = round(min(1.0, cap / 100.0), 3)
        why = f"阿鼻叫喚{cap:.0f}>FIRE かつ安値圏かつ非仕込み(top5={top5})"
    elif overheat > config.SIG_OVERHEAT_TH and not orchestrated:
        side = "fade_up"     # 過熱の天井=上げをフェード
        strength = round(min(1.0, overheat / 100.0), 3)
        why = f"過熱{overheat:.0f}>TH(top5={top5})"
    return {"side": side, "status": "未検証", "strength": strength, "rationale": why}


def meta_confidence(dense_count, n_raw, score_extremity, calibrating):
    """
    vol/rangeスコアへのメタ信頼度[0,1]。較正中は BVP_CONF_CALIB_CAP 以下に強制。
    dense_count>=20・標本大・極値で増加。方向への信頼ではない。純関数。
    """
    if calibrating or (dense_count or 0) < config.SIG_MIN_CALIB_DAYS:
        # 較正中: n と extremity でわずかに動くが cap 以下
        base = 0.10 + 0.10 * min(1.0, (n_raw or 0) / 2000.0) \
            + 0.10 * min(1.0, (score_extremity or 0))
        return round(min(config.BVP_CONF_CALIB_CAP, base), 3)
    dense_term = min(1.0, (dense_count or 0) / 40.0)
    n_term = min(1.0, (n_raw or 0) / 3000.0)
    ext = min(1.0, score_extremity or 0)
    return round(min(1.0, 0.4 * dense_term + 0.3 * n_term + 0.3 * ext), 3)


# ============================================================================
# spec_hash(凍結パラメータの安定ハッシュ)
# ============================================================================
def spec_hash():
    """凍結 SIG_*/BVP_* パラメータ + 特徴リストの安定ハッシュ。各行に刻む。"""
    keys = [
        "SIG_ZSCORE_WINDOW", "SIG_MIN_CALIB_DAYS", "SIG_MIN_DAY_COVERAGE",
        "SIG_MIN_HOUR_BUCKETS", "SIG_DENSE_MIN_CALIB", "SIG_OVERHEAT_TH",
        "SIG_CAPITULATION_WARN", "SIG_CAPITULATION_FIRE", "SIG_LOW_ZONE_PCT",
        "SIG_ONIYA_VOTES_MAX", "BVP_WINSOR_Z", "BVP_REGIME_PCT",
        "BVP_CONF_CALIB_CAP", "SIGNAL_SCHEMA_VERSION", "SIG_MEASURE_REV",
    ]
    payload = {k: getattr(config, k, None) for k in keys}
    payload["features"] = ["post_volume_surprise", "velocity_surge",
                           "lexicon_intensity", "sentiment_disagreement"]
    payload["weights"] = config.BVP_FEATURE_WEIGHTS or "equal"
    blob = repr(sorted(payload.items(), key=lambda x: x[0])).encode("utf-8")
    return "sh1_" + hashlib.sha256(blob).hexdigest()[:12]


# ============================================================================
# 組立
# ============================================================================
def _daily_volume_series(raw_rows, upto_day):
    """dense日ごとの log1p(投稿数) 系列(採点日除外・古→新)+ 当日 log1p。cross-day z 用。"""
    dense = dense_session_dates(raw_rows, upto_day)
    per = {}
    for r in _clean(raw_rows):
        d = (r.get("ts") or "")[:10]
        if len(d) == 10:
            per[d] = per.get(d, 0) + 1
    series = [math.log1p(per.get(d, 0)) for d in dense]
    series.append(math.log1p(per.get(upto_day, 0)))   # 当日を末尾に
    return series, dense


def build_export_record(sig, *, run_ts, cutoff, raw_rows, spec_hash_val=None,
                        data_health=None):
    """
    compute_signals() 出力 + 文脈 -> schema v1.0 準拠 dict(§6)。純関数。
    dense_session_count は raw から再計算(較正の正本)。
    """
    day = sig.get("day")
    raw_clean = _clean(raw_rows)
    dense = dense_session_dates(raw_rows, day)
    dense_count = len(dense)
    calib_status, ramp = calibration_status(dense_count)
    calibrating = calib_status != "calibrated"

    ratios = sig.get("ratios", {})
    lex = sig.get("lexicon", {})
    votes = sig.get("votes", {})
    named = sig.get("named", {})
    other = sig.get("other_symbols", {})
    gauges = sig.get("gauges", {})
    price = sig.get("price", {})

    # cross-day z(dense<閾値なら robust_z が None を返す=較正中)
    vol_series, _ = _daily_volume_series(raw_rows, day)
    post_vol_z = robust_z(vol_series)
    velocity_surge_z = post_vol_z   # 単一セッションでは同源(較正中は共に None)
    disag = disagreement(ratios)

    # BVP 入力(0-1)。z 系は較正中 None。lexicon_intensity は当日値(0-1)で可。
    lex_intensity = _clip01(max(
        (lex.get("capitulation", {}).get("index") or 0) / 20.0,
        (lex.get("euphoria", {}).get("index") or 0) / 20.0))
    bvp_feats = {
        "post_volume_surprise": _z01(post_vol_z),
        "velocity_surge": _z01(velocity_surge_z),
        "lexicon_intensity": lex_intensity,
        "sentiment_disagreement": disag,
        "named_top5_share": named.get("top5_share"),
        "other_symbol_ratio": other.get("ratio"),
    }
    bvp = compute_bvp(bvp_feats)
    vrs = vol_regime_score(bvp["bvp"])
    regime = classify_regime(bvp["bvp"], [])   # history空=calibrating(蓄積後に接続)

    # cross-day z は dense_session が SIG_DENSE_MIN_CALIB 未満のうちは抑制(None)。
    # signals.py の posts_z(=SIG_MIN_DAY_COVERAGEのみのゲート)を素通しすると
    # posts_z=69 のようなアーティファクトが漏れるため、export では dense-honest 値に統一。
    if calibrating or dense_count < config.SIG_DENSE_MIN_CALIB:
        exp_posts_z = post_vol_z          # robust_z(dense系列) = 現状 None
        exp_cap_z = None
        exp_eup_z = None
    else:
        exp_posts_z = sig.get("posts_z")
        exp_cap_z = sig.get("cap_z")
        exp_eup_z = sig.get("eup_z")

    # range_day_score も dense-honest な posts_z を使う(vol_regime_score と同様に
    # dense未了なら None=較正中)。生の sig.posts_z(アーティファクト源)は素通しさせない。
    rds = range_day_score({"posts_z": exp_posts_z}, bvp["parts"])

    feats_export = {
        "posts_z": exp_posts_z,
        "cap_z": exp_cap_z,
        "eup_z": exp_eup_z,
        "velocity_surge_z": velocity_surge_z,
        "disagreement": disag,
        "bull_ratio": ratios.get("bull_ratio"),
        "bear_ratio": ratios.get("bear_ratio"),
        "capitulation_index": lex.get("capitulation", {}).get("index", 0.0),
        "euphoria_index": lex.get("euphoria", {}).get("index", 0.0),
        "aori_index": lex.get("aori", {}).get("index", 0.0),
        "overheat": gauges.get("overheat", 0.0),
        "capitulation": gauges.get("capitulation", 0.0),
        "max_bullish_votes": int(votes.get("max_bullish_votes") or 0),
        "named_top5_share": named.get("top5_share"),
        "other_symbol_ratio": other.get("ratio", 0.0),
        "in_low_zone": price.get("in_low_zone"),
        "price_change_pct": price.get("change_pct"),
        # state の較正カウントは dense_count(calib_days の正本)へ統一(非dense n を出さない)。
        "state": state_dense_honest(gauges.get("state", ""), dense_count, calibrating),
    }
    dirc = direction_candidate(gauges, {
        "in_low_zone": price.get("in_low_zone"),
        "named_top5_share": named.get("top5_share")})

    extremity = max(vrs or 0.0, (dirc.get("strength") or 0.0))
    conf = meta_confidence(dense_count, sig.get("true_volume"), extremity, calibrating)

    # cross-day z 依存カード(投稿サージ/話題枯れ)は較正中は抑制(z=69アーティファクト由来の
    # 誤発火を export に載せない)。同日値のカード(過熱/セリクラ/語彙等)はそのまま。
    _xday_cards = {"投稿サージ", "話題枯れ"}
    thresholds = [
        {"name": c["name"], "value": c["value"], "threshold": str(c["threshold"])}
        for c in sig.get("cards", []) if c.get("state") == "発火"
        and c["name"] in _CARD_ENUM
        and not (calibrating and c["name"] in _xday_cards)
    ]

    dh = dict(data_health or {})
    dh.setdefault("stale", False)
    dh.setdefault("page_cap_hit", False)
    dh.setdefault("garbled_dropped", max(0, len(raw_rows or []) - len(raw_clean)))
    dh["dense_session"] = _is_today_dense(raw_rows, day)

    return {
        "schema_version": config.SIGNAL_SCHEMA_VERSION,
        "symbol": "285A",
        "run_ts": run_ts,
        "asof_date": day,
        "cutoff": cutoff,
        "calibration_status": calib_status,
        "calib_days": dense_count,
        "vol_regime": regime,
        "vol_regime_score": vrs,
        "range_day_score": rds,
        "direction_candidate": dirc,
        "confidence": conf,
        "n": {
            "raw_today": int(sig.get("true_volume") or 0),
            "analyzed_today": int(sig.get("analyzed_today") or 0),
            "posts_per_hour": float(sig.get("posts_per_hour") or 0.0),
        },
        "features": feats_export,
        "thresholds_crossed": thresholds,
        "data_health": dh,
        "signal_spec_hash": spec_hash_val or spec_hash(),
        "disclaimer": DISCLAIMER,
    }


_CARD_ENUM = {"灼熱メーター(過熱)", "そう思う大量票", "イナゴ語彙(euphoria)",
              "ネームド集中", "他銘柄混入率", "暴落煽り語彙", "阿鼻叫喚(セリクラ)",
              "話題枯れ", "投稿サージ"}


def _is_today_dense(raw_rows, day):
    trading = set(config.TRADING_HOURS)
    posts = 0
    buckets = set()
    for r in _clean(raw_rows):
        ts = r.get("ts") or ""
        if ts[:10] == day and len(ts) >= 13:
            posts += 1
            hh = int(ts[11:13]) if ts[11:13].isdigit() else -1
            if hh in trading:
                buckets.add(hh)
    return posts >= config.SIG_MIN_DAY_COVERAGE and len(buckets) >= config.SIG_MIN_HOUR_BUCKETS


def _clip01(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


def _z01(z):
    """winsor済み z(-W..W)を 0-1 へ(0=極小,0.5=中央,1=極大)。None は None。"""
    if z is None:
        return None
    w = config.BVP_WINSOR_Z
    return round(max(0.0, min(1.0, (z + w) / (2 * w))), 3)
