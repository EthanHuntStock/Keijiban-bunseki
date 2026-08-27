# -*- coding: utf-8 -*-
"""
tick_direction_advanced.py - tick_tier_classifier.py の「単純net signed volume符号」
方向判定(moomoo実測との一致率30-50%=ほぼチャンス水準で不採用)に対し、
より複雑な代替ロジックを試作・検証するモジュール(2026-08-27)。

背景・位置づけ(必読):
  tick_tier_classifier.py の docstring / DEFAULT_THRESHOLDS のコメントにある通り、
  「tierごとのnet方向(買い優勢/売り優勢)をprice*tickvolの符号だけで当てる」試みは
  moomoo実測(tier_flow_manual_log.csv)との方向一致率30-50%(ほぼ五分五分)で失敗した。
  これは2026-07-10にトレPJが `retail_flow_proxy` で経験した「符号逆転」と同型の
  構造的限界(「プリント≠プレイヤー」=約定サイズが大きくてもそれを主導した口座階層は
  tickデータからは分からない)であり、tick_tier_classifier.py 側は既に
  「方向は使わない・estimate_tier_size_shares()のサイズ仕分けのみ実運用」と結論済み。

  本モジュールはその結論を覆すものではない。ユーザーから2026-08-27
  「方向性は諦めず、より複雑なロジックで再分析してほしい」と明示の追加指示があった
  ため、単純符号一致より複雑な代替ロジックを最低3パターン実装し、
  既存ground truth(tier_flow_manual_log.csv、2026-08-27の5時点=同日内4区間×4階層=
  16件の階層×区間比較、独立試行ではなく同日内の自己相関データ)に対して
  **正直に**(過学習的な後付けを避けて)評価する。

  ★n=7-8(実質は同日16件、独立標本ではない)という極小サンプルであり、
  ここで出る一致率は統計的に確定的な結論を意味しない。改善が見えても
  「有望な仮説」以上には扱わない(前向き検証・追加日のground truth蓄積が必須)。

実装した代替ロジック(いずれも純関数。datetime.now()は使わず、時刻/データは
引数で受け取る):
  1. baseline_naive_sign()          : 既存の単純符号(比較用の再現・新規性なし)。
  2. price_absorption_direction()   : テープリーディングの吸収/拒否
                                       (指示2a)。価格が明確に動いた区間では
                                       「プリントの符号がどうであれ、価格が
                                       示す方向に価格を動かした側が実際には
                                       優勢だった」と読み替える(吸収された側の
                                       プリント符号を反転解釈)。価格がほぼ
                                       フラットな区間は素の符号を採用する。
  3. persistence_filtered_signs()   : ローリング窓での累積・平滑化(指示2b)。
                                       単発の符号は信号を出さず(abstain=None)、
                                       2区間以上連続で同符号が続いた場合のみ
                                       予測を出す(ノイズ単発を捨て持続性を要求)。
  4. reversal_alignment()           : reversal_signals.pyの反転検出との合流検証
                                       (指示2c)。板総計ベースの反転候補
                                       (high_reversal/low_reversal)と、その区間の
                                       tier net方向が理論的に整合するか
                                       (天井=SELL優勢/底=BUY優勢が「整合」)を
                                       判定する。★moomoo ground truthとは別軸の
                                       突合であり、hit-rate表には含めない
                                       (指示4「独立指標としての価値があるか」の
                                       確認=descriptive/exploratory専用)。
  5. bucket_time_of_day()           : 時間帯条件付け(指示2d)。区間を
                                       寄り付き直後/引け前/それ以外へ分類し、
                                       手法1の一致率を時間帯別に見る
                                       (n=4区間×4階層のさらに細分=極小)。

規律(既存モジュールと同型):
  - 読み取り専用。tick_tier_classifier.py/reversal_signals.py/config.py/
    tier_flow_manual_log.py 等の既存ファイルには一切書き込まない・変更しない。
  - 既存ロジックの重複実装はしない(tier分類・板ratio・RSI等は
    tick_tier_classifier / reversal_signals から import して再利用)。
  - datetime.now()禁止。全ての時刻は引数(as_of/date_iso等)で受け取る。
  - 純関数(ネット/pandas非依存)はselftest対象。I/O補助はselftest対象外。

検証: `python tick_direction_advanced.py --selftest`(合成データ・純関数のみ)。
実データ評価: `python tick_direction_advanced.py --evaluate`(2026-08-27の
tier_flow_manual_log.csv + kabu tickCSV + 板CSVを読み、各手法のhit-rateを
表示するだけ。ファイル/台帳への書き込みは一切なし)。
"""
import os
import csv
import sys

import config
import tick_tier_classifier as ttc
import tier_flow_manual_log as tfml

try:
    import reversal_signals as rs
except Exception:  # pragma: no cover - reversal_signals は板CSV依存の重い import を含むため fail-soft
    rs = None


GT_TIER_NAMES = ("mega", "large", "mid", "small")
# tier_flow_manual_log.csv の日本語寄りの階層名 -> tick_tier_classifier.TIER_ORDER の名前
GT_TO_TTC_TIER = {"mega": "super", "large": "big", "mid": "mid", "small": "small"}


# ============================================================================
# 0. 共通の符号判定(純関数)
# ============================================================================
def sign_of(x, eps=0.0):
    """netがepsより大きければBUY、-epsより小さければSELL、それ以外はFLAT。純関数。"""
    if x is None:
        return None
    if x > eps:
        return "BUY"
    if x < -eps:
        return "SELL"
    return "FLAT"


# ============================================================================
# 1. baseline(既存の単純符号・比較用の再現。新規ロジックではない)
# ============================================================================
def baseline_naive_sign(net_amount, eps=0.0):
    """tick_tier_classifier.aggregate_tier_flow()のnetをそのまま符号化するだけの
    既存手法の再現(比較基準)。純関数。"""
    return sign_of(net_amount, eps=eps)


# ============================================================================
# 2a. 価格反応条件付き解釈(テープリーディングの吸収/拒否)
# ============================================================================
def price_absorption_direction(naive_sign, price_start, price_end, flat_pct_th=0.001):
    """テープリーディングの「吸収(absorption)/拒否(rejection)」解釈(純関数)。

    考え方: ある区間でプリントの符号がSELL優勢(売り建玉が多い)にも関わらず
    価格がその区間で明確に上昇したなら、「売り物を吸収して上昇した」=
    実際には隠れた買い優勢だったと読み替える(素の符号と逆に解釈)。
    逆にプリント符号がBUY優勢でも価格が明確に下落したなら「買いを吸収して
    下落した」=隠れた売り優勢と読み替える。
    価格が区間を通じてほぼフラット(|変化率|<flat_pct_th)なら、価格からは
    情報が取れないため素の符号(naive_sign)をそのまま採用する。

    ★正直な注記: 価格が明確に動いた区間では、この関数の出力は
    naive_signの値に関わらず必ず「価格が動いた方向」と一致する
    (吸収/拒否のどちらの場合でも、結論は常に価格方向に収束するため)。
    つまり価格が明確に動く区間では実質「価格方向をそのまま予測に使う」
    ロジックになる。プリント符号が使われるのは価格がフラットな区間のみ。
    これは意図した設計(指示2aの「単純符号とは逆の解釈が正しいかを検証」)
    をそのまま実装した結果であり、恣意的な後付けではない。

    price_start/price_endがNone、またはprice_start<=0の場合はnaive_signを返す
    (価格情報が使えない場合のfail-soft)。
    """
    if price_start is None or price_end is None or price_start == 0:
        return naive_sign
    pct = (price_end - price_start) / price_start
    if abs(pct) < flat_pct_th:
        return naive_sign
    return "BUY" if pct > 0 else "SELL"


# ============================================================================
# 2b. ローリング窓での累積・平滑化(persistence filter)
# ============================================================================
def persistence_filtered_signs(sign_sequence):
    """単発の符号ノイズを捨て、2区間以上連続で同符号が続いた点だけ予測を出す(純関数)。

    sign_sequence: 時系列順の "BUY"/"SELL"/"FLAT"/None のリスト(例: 各区間の
    naive_signを並べたもの)。
    戻り値: 同じ長さのリスト。各要素は、直前の要素と同じ符号(BUY/SELLのみ、
    FLAT/Noneは対象外)が2回連続した時点でその符号を採用、それ以外はNone
    (abstain=信号を出さない)。先頭要素は常にNone(比較対象の前区間が無い)。
    """
    n = len(sign_sequence)
    out = [None] * n
    for i in range(1, n):
        cur = sign_sequence[i]
        prev = sign_sequence[i - 1]
        if cur in ("BUY", "SELL") and cur == prev:
            out[i] = cur
    return out


# ============================================================================
# 2c. reversal_signals.pyとの合流検証(独立指標としての整合性。moomoo比較とは別軸)
# ============================================================================
_REVERSAL_EXPECTED_SIGN = {
    "high_reversal": "SELL",  # 天井圏=買い優勢の過熱→理論上は超大口が利確売り(distribution)
    "low_reversal": "BUY",    # 底値圏=売り優勢の過熱→理論上は超大口が押し目買い(accumulation)
}


def reversal_alignment(reversal_type, net_sign):
    """板総計ベースの反転検出(reversal_signals.detect_reversal()のtype)と、
    その時点のtier net方向が理論的に整合するかを判定する純関数。

    - reversal_type が "high_reversal"/"low_reversal" のいずれでもなければ
      (Noneや未検出) None を返す(判定対象外)。
    - net_signがNone/FLATならNone(判定不能)。
    - 整合("ALIGNED")/矛盾("CONTRARIAN")のいずれかを返す。

    ★これはmoomoo ground truthとの一致率とは別軸の検証(指示2c「独立指標
    としての価値があるか」)。reversal_signals自体も未検証(研究用)なので、
    ここでの整合/矛盾はどちらも「仮説として面白いか」程度の参考情報。
    """
    expected = _REVERSAL_EXPECTED_SIGN.get(reversal_type)
    if expected is None or net_sign not in ("BUY", "SELL"):
        return None
    return "ALIGNED" if net_sign == expected else "CONTRARIAN"


# ============================================================================
# 2d. 時間帯条件付け
# ============================================================================
def bucket_time_of_day(hhmm, opening_end="09:50", closing_start="14:30"):
    """"HH:MM"文字列を寄り付き直後("opening")/引け前("closing")/それ以外
    ("midday")へ分類する純関数。境界はopening_end未満=opening、
    closing_start以上=closing、それ以外=midday(文字列辞書式比較)。
    """
    if hhmm is None:
        return None
    if hhmm < opening_end:
        return "opening"
    if hhmm >= closing_start:
        return "closing"
    return "midday"


# ============================================================================
# hit-rate集計(純関数)
# ============================================================================
def hit_rate(pairs):
    """[(actual_sign, predicted_sign), ...] から (hits, total, rate, abstained) を返す。
    predicted_signがNone(abstain)の組は total/hits に含めず abstained へ計上する。
    actual_signがNone/FLATの組も判定不能として除外する。純関数。
    totalが0ならrate=None(0除算を捏造しない)。
    """
    hits = 0
    total = 0
    abstained = 0
    for actual, pred in pairs:
        if actual not in ("BUY", "SELL"):
            continue
        if pred is None:
            abstained += 1
            continue
        if pred not in ("BUY", "SELL"):
            continue
        total += 1
        if pred == actual:
            hits += 1
    rate = (hits / total) if total > 0 else None
    return {"hits": hits, "total": total, "rate": rate, "abstained": abstained}


# ============================================================================
# I/O補助(ground truth読み込み・tickデータからのtier net計算)。
# 既存モジュールの再実装はせず import して使う。
# ============================================================================
def load_ground_truth_rows():
    """tier_flow_manual_log.csv の全観測行を読む(既存モジュールへの委譲・重複実装なし)。"""
    return tfml.read_all_observations()


def ground_truth_interval_records(gt_rows):
    """ground truth行(累積buy/sell)を時刻昇順とみなし、連続する2行の差分から
    区間ごとの正味方向(gt_net/gt_sign)を階層別に作る純関数。

    戻り値: [{"t0","t1","tier","gt_net","gt_sign","price0","price1"}, ...]
    (tier は GT_TIER_NAMES の4種×連続ペア数)。
    """
    out = []
    rows = list(gt_rows or [])
    for i in range(1, len(rows)):
        r0, r1 = rows[i - 1], rows[i]
        try:
            price0 = float(r0.get("price"))
            price1 = float(r1.get("price"))
        except (TypeError, ValueError):
            price0 = price1 = None
        for tier in GT_TIER_NAMES:
            try:
                buy0 = float(r0.get(f"{tier}_buy"))
                sell0 = float(r0.get(f"{tier}_sell"))
                buy1 = float(r1.get(f"{tier}_buy"))
                sell1 = float(r1.get(f"{tier}_sell"))
            except (TypeError, ValueError):
                continue
            gt_net = (buy1 - buy0) - (sell1 - sell0)
            out.append({
                "t0": r0.get("time"), "t1": r1.get("time"), "tier": tier,
                "gt_net": gt_net, "gt_sign": sign_of(gt_net),
                "price0": price0, "price1": price1,
            })
    return out


def proxy_tier_net_for_window(ticks, date_iso, start_hhmm, end_hhmm, thresholds=None):
    """kabu tickデータから、(start_hhmm, end_hhmm]区間(start_hhmmの分は前区間側に
    帰属済みとみなして除外し、end_hhmmの分まで含める)の階層別net(円)を計算する。
    tick_tier_classifier.filter_ticks_window/classify_tick_tiers/aggregate_tier_flow
    をそのまま再利用するだけの薄いラッパー(重複実装なし)。

    ★区間の切り方(境界重複防止): start_hhmmの分を含めてしまうと、隣接する
    前区間の終端(その分自体)と当区間の双方で同じtickを二重集計してしまう。
    そのためstart_timeは「start_hhmm分の末尾(59.999)」を下限にして
    start_hhmm分自体は含めない(前区間側に属するとみなす)。
    戻り値: {"super":net,"big":net,"mid":net,"small":net}(円)。
    """
    start_time = f"{date_iso} {start_hhmm}:59.999" if start_hhmm else None
    end_time = f"{date_iso} {end_hhmm}:59.999" if end_hhmm else None
    windowed = ttc.filter_ticks_window(ticks, start_time=start_time, end_time=end_time)
    classified = ttc.classify_tick_tiers(windowed, thresholds=thresholds)
    agg = ttc.aggregate_tier_flow(classified)
    return {tier: agg[tier]["net"] for tier in ttc.TIER_ORDER}


# ============================================================================
# 実データ評価オーケストレーション(read-only・書き込み一切なし)
# ============================================================================
def evaluate_methods(gt_rows, ticks, date_iso, thresholds=None):
    """4手法(baseline/absorption/persistence)をground truthに対して評価する。
    reversal_alignment(手法2c)はmoomoo比較の対象外のため別途扱う(呼び出し側で
    detect_reversal結果と組み合わせて使う想定・本関数には含めない)。

    戻り値: {
      "records": [...интервал単位の生データ...],
      "baseline": hit_rate結果,
      "absorption": hit_rate結果,
      "persistence": hit_rate結果,
      "by_tier": {tier: {"baseline":..,"absorption":..}},
      "by_time_bucket": {bucket: baseline hit_rate結果},
    }
    """
    intervals = ground_truth_interval_records(gt_rows)

    # 各区間×階層のproxy net(区間ごとに一度だけtick集計すればよいのでキャッシュ)
    window_cache = {}
    for rec in intervals:
        key = (rec["t0"], rec["t1"])
        if key not in window_cache:
            window_cache[key] = proxy_tier_net_for_window(
                ticks, date_iso, rec["t0"], rec["t1"], thresholds=thresholds)
        proxy_net = window_cache[key][GT_TO_TTC_TIER[rec["tier"]]]
        rec["proxy_net"] = proxy_net
        rec["naive_sign"] = baseline_naive_sign(proxy_net)
        rec["absorption_sign"] = price_absorption_direction(
            rec["naive_sign"], rec["price0"], rec["price1"])
        rec["time_bucket"] = bucket_time_of_day(rec["t0"])

    # persistence: 階層ごとに時系列順(intervalsは既にt0昇順で生成されている)
    by_tier_seq = {}
    for rec in intervals:
        by_tier_seq.setdefault(rec["tier"], []).append(rec)
    for tier, recs in by_tier_seq.items():
        seq = [r["naive_sign"] for r in recs]
        preds = persistence_filtered_signs(seq)
        for r, p in zip(recs, preds):
            r["persistence_sign"] = p

    baseline_pairs = [(r["gt_sign"], r["naive_sign"]) for r in intervals]
    absorption_pairs = [(r["gt_sign"], r["absorption_sign"]) for r in intervals]
    persistence_pairs = [(r["gt_sign"], r["persistence_sign"]) for r in intervals]

    by_tier = {}
    for tier, recs in by_tier_seq.items():
        by_tier[tier] = {
            "baseline": hit_rate([(r["gt_sign"], r["naive_sign"]) for r in recs]),
            "absorption": hit_rate([(r["gt_sign"], r["absorption_sign"]) for r in recs]),
            "persistence": hit_rate([(r["gt_sign"], r["persistence_sign"]) for r in recs]),
        }

    by_time_bucket = {}
    for rec in intervals:
        bucket = rec["time_bucket"]
        by_time_bucket.setdefault(bucket, []).append((rec["gt_sign"], rec["naive_sign"]))
    by_time_bucket = {b: hit_rate(pairs) for b, pairs in by_time_bucket.items()}

    return {
        "records": intervals,
        "baseline": hit_rate(baseline_pairs),
        "absorption": hit_rate(absorption_pairs),
        "persistence": hit_rate(persistence_pairs),
        "by_tier": by_tier,
        "by_time_bucket": by_time_bucket,
    }


def evaluate_reversal_convergence(gt_rows, board_rows, tick_rows, date_iso):
    """手法2c(reversal_signals合流検証)。moomoo比較の対象外・descriptive専用。
    reversal_signalsが読み込めない環境(rs is None)ではfail-softで[]を返す。

    各ground truth区間の終端時刻(t1)をas_ofとしてdetect_reversalを呼び、
    反転候補が出た区間についてground truthのmega/large tier方向との
    整合(ALIGNED/CONTRARIAN)を記録する(純関数呼び出しの組み合わせのみ)。
    """
    if rs is None:
        return []
    intervals = ground_truth_interval_records(gt_rows)
    out = []
    for rec in intervals:
        if rec["tier"] not in ("mega", "large"):
            continue
        analyzed_rows = []  # センチメントは本検証に不要(read_today_analyzed_rowsはI/O重いため省略)
        det = rs.detect_reversal(board_rows, tick_rows, analyzed_rows, date_iso, as_of=rec["t1"])
        rtype = det["type"] if det else None
        alignment = reversal_alignment(rtype, rec["gt_sign"])
        out.append({
            "t0": rec["t0"], "t1": rec["t1"], "tier": rec["tier"],
            "reversal_type": rtype, "gt_sign": rec["gt_sign"], "alignment": alignment,
        })
    return out


def run_evaluation(date_iso="2026-08-27", ticks_path=None, board_path=None):
    """評価一式を実行し、結果を表示する(orchestration・fail-soft・書き込みなし)。
    ticks_path省略時はconfig.KABU_PROTO1_285A_TICKS_DIR配下の当日ファイルを使う。
    """
    gt_rows = load_ground_truth_rows()
    gt_rows = [r for r in gt_rows if r.get("date") == date_iso]
    if len(gt_rows) < 2:
        print(f"ground truth行が2件未満(date={date_iso})のため区間比較不能。"
              f"tier_flow_manual_log.csvの蓄積を待ってください。")
        return None

    ticks_path = ticks_path or os.path.join(
        config.KABU_PROTO1_285A_TICKS_DIR, f"ticks_{config.SYMBOL}_{date_iso}.csv")
    if not os.path.isfile(ticks_path):
        print(f"tick CSVが見つかりません: {ticks_path}")
        return None
    ticks = ttc.load_ticks_csv(ticks_path)

    result = evaluate_methods(gt_rows, ticks, date_iso)

    print(f"=== tick_direction_advanced 評価結果 (date={date_iso}, "
          f"n_intervals={len(gt_rows) - 1}, n_tier_interval_pairs={len(result['records'])}) ===")
    print("※同日内の区間データであり独立試行ではない(自己相関あり)。n=7-8程度の"
          "極小サンプルにつき統計的に確定的な結論は出せない。")
    for name in ("baseline", "absorption", "persistence"):
        r = result[name]
        rate_str = f"{r['rate']:.1%}" if r["rate"] is not None else "N/A"
        print(f"[{name:11s}] hits={r['hits']}/{r['total']} ({rate_str})  abstained={r['abstained']}")

    print("\n--- 階層別 ---")
    for tier, methods in result["by_tier"].items():
        line = f"  {tier:6s}: "
        for name in ("baseline", "absorption", "persistence"):
            r = methods[name]
            rate_str = f"{r['rate']:.0%}" if r["rate"] is not None else "N/A"
            line += f"{name}={r['hits']}/{r['total']}({rate_str}) "
        print(line)

    print("\n--- 時間帯別(baseline手法) ---")
    for bucket, r in result["by_time_bucket"].items():
        rate_str = f"{r['rate']:.0%}" if r["rate"] is not None else "N/A"
        print(f"  {bucket:8s}: hits={r['hits']}/{r['total']} ({rate_str})")

    board_path = board_path or os.path.join(
        config.KABU_PROTO1_285A_BOARD_DIR, f"board_{config.SYMBOL}_{date_iso}.csv")
    if rs is not None and os.path.isfile(board_path):
        board_rows = rs._read_csv_rows(board_path)
        tick_rows_dict = ttc.load_ticks_csv(ticks_path)
        conv = evaluate_reversal_convergence(gt_rows, board_rows, tick_rows_dict, date_iso)
        detected = [c for c in conv if c["reversal_type"] is not None]
        print(f"\n--- reversal_signals合流検証(descriptive・moomoo比較外・"
              f"n={len(detected)}件検出/{len(conv)}件中) ---")
        if not detected:
            print("  当日の区間終端では反転候補が検出されなかった(または閾値未達)。")
        for c in detected:
            print(f"  {c['t0']}->{c['t1']} {c['tier']:5s} reversal={c['reversal_type']} "
                  f"gt_sign={c['gt_sign']} alignment={c['alignment']}")
    else:
        print("\n--- reversal_signals合流検証: 板CSV未検出またはreversal_signals import失敗のためスキップ ---")

    return result


# ============================================================================
# 自己テスト(ネット非依存・合成データ)
# ============================================================================
def _run_selftests():
    fails = []

    def ck(name, cond):
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # ---- sign_of ----
    ck("sign_of positive -> BUY", sign_of(10) == "BUY")
    ck("sign_of negative -> SELL", sign_of(-5) == "SELL")
    ck("sign_of zero -> FLAT", sign_of(0) == "FLAT")
    ck("sign_of None -> None", sign_of(None) is None)
    ck("sign_of eps境界: epsちょうどはFLAT", sign_of(5, eps=5) == "FLAT")

    # ---- baseline_naive_sign(既存手法の再現) ----
    ck("baseline_naive_sign: 正味netがBUY", baseline_naive_sign(1000) == "BUY")
    ck("baseline_naive_sign: 正味netがSELL", baseline_naive_sign(-1000) == "SELL")

    # ---- price_absorption_direction ----
    ck("absorption: 価格上昇+naive=SELL -> 吸収解釈でBUYへ反転",
       price_absorption_direction("SELL", 100.0, 105.0) == "BUY")
    ck("absorption: 価格下落+naive=BUY -> 吸収解釈でSELLへ反転",
       price_absorption_direction("BUY", 100.0, 95.0) == "SELL")
    ck("absorption: 価格上昇+naive=BUY -> 確認でBUY(変化なし)",
       price_absorption_direction("BUY", 100.0, 105.0) == "BUY")
    ck("absorption: 価格フラット -> naive_signをそのまま採用",
       price_absorption_direction("SELL", 100.0, 100.05) == "SELL")
    ck("absorption: price_start=None -> naive_signへfail-soft",
       price_absorption_direction("BUY", None, 105.0) == "BUY")
    ck("absorption: price_start=0 -> naive_signへfail-soft(0除算回避)",
       price_absorption_direction("SELL", 0.0, 5.0) == "SELL")

    # ---- persistence_filtered_signs ----
    seq = ["SELL", "BUY", "BUY", "SELL"]
    preds = persistence_filtered_signs(seq)
    ck("persistence: 先頭は常にNone(前区間なし)", preds[0] is None)
    ck("persistence: SELL->BUYは不一致でNone(abstain)", preds[1] is None)
    ck("persistence: BUY->BUYは一致でBUYを採用", preds[2] == "BUY")
    ck("persistence: BUY->SELLは不一致でNone(abstain)", preds[3] is None)

    seq_flat_none = ["BUY", "FLAT", None, "SELL"]
    preds2 = persistence_filtered_signs(seq_flat_none)
    ck("persistence: FLAT/Noneは連続一致の対象外(常にNone)",
       preds2[1] is None and preds2[2] is None and preds2[3] is None)

    # ---- reversal_alignment ----
    ck("reversal_alignment: high_reversal時にSELLはALIGNED",
       reversal_alignment("high_reversal", "SELL") == "ALIGNED")
    ck("reversal_alignment: high_reversal時にBUYはCONTRARIAN",
       reversal_alignment("high_reversal", "BUY") == "CONTRARIAN")
    ck("reversal_alignment: low_reversal時にBUYはALIGNED",
       reversal_alignment("low_reversal", "BUY") == "ALIGNED")
    ck("reversal_alignment: low_reversal時にSELLはCONTRARIAN",
       reversal_alignment("low_reversal", "SELL") == "CONTRARIAN")
    ck("reversal_alignment: reversal_type=None -> None", reversal_alignment(None, "BUY") is None)
    ck("reversal_alignment: net_sign=FLAT -> None", reversal_alignment("high_reversal", "FLAT") is None)

    # ---- bucket_time_of_day ----
    ck("bucket: 09:10はopening", bucket_time_of_day("09:10") == "opening")
    ck("bucket: 09:50境界はmidday(未満条件のためopeningでない)",
       bucket_time_of_day("09:50") == "midday")
    ck("bucket: 11:06はmidday", bucket_time_of_day("11:06") == "midday")
    ck("bucket: 14:30境界はclosing", bucket_time_of_day("14:30") == "closing")
    ck("bucket: 15:30はclosing", bucket_time_of_day("15:30") == "closing")
    ck("bucket: None -> None", bucket_time_of_day(None) is None)

    # ---- hit_rate ----
    pairs = [("BUY", "BUY"), ("SELL", "BUY"), ("BUY", "SELL"), ("SELL", "SELL"), ("BUY", None)]
    r = hit_rate(pairs)
    ck("hit_rate: 2/4的中(Noneは母数除外・abstain計上)", r["hits"] == 2 and r["total"] == 4)
    ck("hit_rate: rate=0.5", abs(r["rate"] - 0.5) < 1e-9)
    ck("hit_rate: abstained=1", r["abstained"] == 1)
    r_empty = hit_rate([])
    ck("hit_rate: 空リストはrate=None(0除算を捏造しない)", r_empty["rate"] is None)
    r_actual_flat = hit_rate([("FLAT", "BUY")])
    ck("hit_rate: actualがFLATの組は判定不能として除外", r_actual_flat["total"] == 0)

    # ---- ground_truth_interval_records(合成2行) ----
    gt_rows = [
        {"date": "2026-01-01", "time": "09:10", "price": "100",
         "mega_buy": "10", "mega_sell": "5", "large_buy": "1", "large_sell": "1",
         "mid_buy": "1", "mid_sell": "1", "small_buy": "1", "small_sell": "1"},
        {"date": "2026-01-01", "time": "09:46", "price": "110",
         "mega_buy": "10", "mega_sell": "20", "large_buy": "5", "large_sell": "1",
         "mid_buy": "2", "mid_sell": "2", "small_buy": "3", "small_sell": "1"},
    ]
    recs = ground_truth_interval_records(gt_rows)
    ck("gt_interval: 2行から4階層分=4レコード", len(recs) == 4)
    mega_rec = next(r for r in recs if r["tier"] == "mega")
    ck("gt_interval: mega区間net=(10-10)-(20-5)=-15", abs(mega_rec["gt_net"] - (-15)) < 1e-9)
    ck("gt_interval: mega区間符号はSELL", mega_rec["gt_sign"] == "SELL")
    large_rec = next(r for r in recs if r["tier"] == "large")
    ck("gt_interval: large区間net=(5-1)-(1-1)=4", abs(large_rec["gt_net"] - 4) < 1e-9)
    ck("gt_interval: price0/price1を保持", mega_rec["price0"] == 100.0 and mega_rec["price1"] == 110.0)

    # ---- ground_truth_interval_records: 不正値/欠損は静かにスキップ ----
    gt_rows_bad = [
        {"date": "x", "time": "09:10", "price": "bad", "mega_buy": "10", "mega_sell": "5"},
        {"date": "x", "time": "09:46", "price": "110", "mega_buy": "abc", "mega_sell": "1"},
    ]
    recs_bad = ground_truth_interval_records(gt_rows_bad)
    ck("gt_interval: 数値変換不能な階層はスキップ(空リスト)", recs_bad == [])

    print("PASS" if not fails else f"FAIL: {len(fails)}")
    for name in fails:
        print("  - " + name)
    return len(fails)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _run_selftests() else 0)

    if "--evaluate" in sys.argv:
        date_arg = "2026-08-27"
        if "--date" in sys.argv:
            date_arg = sys.argv[sys.argv.index("--date") + 1]
        run_evaluation(date_iso=date_arg)
        sys.exit(0)

    print(__doc__)
