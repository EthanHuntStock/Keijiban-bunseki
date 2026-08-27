# -*- coding: utf-8 -*-
"""
tick_tier_classifier.py - kabu APIのtickデータ(price,tickvol,bid,ask)から
超大口/大口/中口/小口の資金流入出を推定する試作(プロトタイプ)分類器。

背景・目的(2026-08-27):
  moomoo経由の階層別データ自動取得はOpenDログインがCAPTCHAでブロックされ保留中
  (CROSS_PROJECT_LOG 2026-08-24参照)。一方kabu APIのtickデータ
  (株取引API_プロト1\\285A_キオクシア\\記録データ\\ticks_285A_*.csv)は自前で
  保有しており、各約定について price*tickvol(概算金額)を計算すれば、moomooに頼らず
  4段階(超大口/大口/中口/小口)へ仕訳できるのでは、というのが本アイデアの核。
  おにやの tier_flow_manual_log.py が蓄積する手動転記(kabu画面スクショのground truth)
  を答え合わせに使い、閾値を校正する。校正・評価は calibrate_tick_tier_classifier.py
  (このファイルとは別・スクラッチ実行用)で行う。

前提・限界(必読・誠実に書く):
  - buy/sell方向判定は真の約定属性(kabu tick CSVには方向列が無い)ではなく、
    bid/askに対する価格位置によるLee-Ready近似(price>=真ask→BUY, price<=真bid→SELL,
    それ以外はtick test=前tickとの価格比較でuptick=BUY/downtick=SELLへフォールバック)。
    **厳密な約定方向ではない**近似にすぎない。
  - ★kabu tick CSVのbid/ask列は反転している(kabu公式の既知仕様。メモリ
    reference-tse-preopen-mechanics.md「Bid/Askは公式に反転」参照)。285A
    2026-08-27の全tick(48,072件)で実測=100%が bid>ask(反転)・正常順(bid<ask)は
    0件だった。infer_direction()はこれを補正し、tick辞書の"ask"列を真のbid、
    "bid"列を真のaskとして解釈する(補正前は BUY:SELL≈6:1 に偏り、補正後は
    ほぼ半々=現実的な分布になることを確認済み)。
  - moomooの超大口/大口/中口/小口の閾値定義(金額か株数か、桁がいくつか)は非公開。
    本実装は「金額(price*tickvol)ベースの4閾値」という仮説にすぎず、moomoo/kabu内部の
    分類ロジックと一致する保証はない。閾値は外部から渡す設計にして校正データが増える
    都度、再調整できるようにしてある(ハードコードしない)。
  - moomoo/kabu画面が示す値が「その日の累積(寄付からの累積)」なのか「直近区間」なのかは
    観測データ側のdocstring/傾向から推定するしかない(tier_flow_manual_log.csvは
    寄付からの累積と推定される=モジュール外の校正スクリプトで検証)。
  - ★★最重要の限界(2026-07-10 トレPJ `retail_flow_proxy` の先行事例と同型):
    「プリント≠プレイヤー」= 約定サイズが大きいからといって、その約定を主導したのが
    超大口口座とは限らない。kabu tick側の"大きい約定"とmoomoo側の"口座階層"は別の
    情報源であり、サイズだけで方向(買い優勢/売り優勢)を当てにいくと符号が逆転しうる
    (先行事例: moomoo超大口+70.6B買いに対しproxyは−108.8B売り=符号逆、と較正済み)。
    本モジュールでの5点校正でも、bid/ask反転を補正した後のtier別net方向一致率は
    30-50%(ほぼ五分五分〜やや悪い)にとどまり、同じ限界を再現した。
    **⇒ tierごとのnet方向(買い優勢/売り優勢)は方向シグナルとして使わない**。
    使うなら「tierごとの出来高シェア(方向を無視したサイズ分布)」に留めるべきで、
    それでも閾値次第で相関は弱い(校正スクリプトの評価参照)。

純関数(ネット/pandas非依存 = selftest対象):
  infer_direction / classify_tick_tier / classify_tick_tiers / aggregate_tier_flow /
  filter_ticks_window / aggregate_tier_amount / estimate_tier_size_shares
I/O補助(純関数ではないがネット非依存):
  load_ticks_csv

============================================================================
★実運用の入口はこちら(2026-08-27・方向は主張しない):
  estimate_tier_size_shares(ticks, thresholds=DEFAULT_THRESHOLDS)
    -> tierごとの出来高金額シェア(0-1の構成比)のみを返す。buy/sell方向・net等の
       情報は一切含まない。ユーザー承認済み方針=「方向性は無理でも大丈夫、
       仕分けができたなら十分」(2026-08-27)を受けて新設。
  aggregate_tier_flow(classified_ticks) は方向つき集計を維持するが(既存コード互換の
  ため残置)、上記の理由で**実運用では使わないこと**(下記関数docstringの警告参照)。
============================================================================
"""
import csv


# ============================================================================
# 既定閾値(円建て・ハードコードだが「既定値」であって固定ではない=
# 呼び出し側は必ず thresholds を渡して上書きできる。校正データが増えたら
# 校正スクリプト側で再探索し、この既定値も更新していく想定)。
#
# 2026-08-27校正(285A・tier_flow_manual_log.csv 5点+過去2点)で3グリッドを比較した結果、
# mid=50M/big=200M/super=1000M が「tierごとの出来高シェア(方向を無視したサイズ分布)」の
# スピアマン順位相関で最も高かった(平均rho≈0.6・09:46以降の4点はrho=0.8)ため、これを既定に
# 採用する。ただし**net方向(買い優勢/売り優勢)の符号一致率はどのグリッドでも30-50%(ほぼ
# 五分五分)にとどまり閾値では改善しない**=方向判定には使わない(モジュールdocstring参照)。
# n=7(同日5点+別日2点)という極小サンプルでの選択であり、確定的な最適値ではない。
# ============================================================================
DEFAULT_THRESHOLDS = {
    "super": 1_000_000_000,  # 円(10億)。この額以上の約定 = 超大口
    "big": 200_000_000,      # 円(2億)。この額以上(superに満たない) = 大口
    "mid": 50_000_000,       # 円(5000万)。この額以上(bigに満たない) = 中口
    # mid 未満 = 小口
}

TIER_ORDER = ("super", "big", "mid", "small")


def _to_float(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ============================================================================
# buy/sell方向の簡易近似判定(純関数)
# ============================================================================
def infer_direction(tick, prev_price=None):
    """
    Lee-Ready近似のbuy/sell方向判定。

    ★重要(2026-08-27発見・実データで検証済み): kabu APIのBid/Ask列は
    「本来の意味である『買気配』と『売気配』と逆」というkabu公式の既知仕様がある
    (メモリ reference-tse-preopen-mechanics.md 参照。寄り前板で実測=88%が
    bid_price>ask_price)。この tick CSV でも同じ検証を行い、285A 2026-08-27の
    全tick(48,072件)で bid>ask(反転)・0件が正常順(bid<ask)だった=同じ反転が
    tickデータのbid/ask列にも及んでいることを確認済み。
    したがって本関数は **tick辞書の "ask" 列を真の買気配(bid)、"bid" 列を
    真の売気配(ask) として扱う**(列名と実際の意味を入れ替えて解釈する)。
    この補正をしないと BUY 判定が SELL の約6倍に偏る(実測: 補正前 41243 BUY /
    6833 SELL、補正後 24032 BUY / 24044 SELL とほぼ半々になり現実的になった)。

    判定: price>=真のask(=tick['bid']) -> "BUY"、price<=真のbid(=tick['ask']) -> "SELL"。
    その間、または bid/ask欠損時は、前tickとの価格比較(tick test)にフォールバック
    (上昇=BUY・下落=SELL・同値/前値なし=None)。
    price欠損なら None。**それでも厳密な約定方向ではない近似**(モジュールdocstring参照)。
    """
    price = _to_float((tick or {}).get("price"))
    if price is None:
        return None
    # ★列名を入れ替えて解釈(上のdocstring参照): tick['ask']が真のbid、tick['bid']が真のask。
    true_bid = _to_float((tick or {}).get("ask"))
    true_ask = _to_float((tick or {}).get("bid"))
    if true_bid is not None and true_ask is not None and true_ask > 0 and true_bid > 0:
        if price >= true_ask:
            return "BUY"
        if price <= true_bid:
            return "SELL"
    if prev_price is not None:
        if price > prev_price:
            return "BUY"
        if price < prev_price:
            return "SELL"
    return None


# ============================================================================
# 金額(円) -> 4段階tier分類(純関数)
# ============================================================================
def classify_tick_tier(amount_yen, thresholds=None):
    """
    1件の約定金額(円)を "super"/"big"/"mid"/"small" の4段階へ分類する純関数。
    thresholds省略時はDEFAULT_THRESHOLDSを使う。amount_yenがNone/負なら None。
    """
    if amount_yen is None or amount_yen < 0:
        return None
    th = thresholds or DEFAULT_THRESHOLDS
    if amount_yen >= th["super"]:
        return "super"
    if amount_yen >= th["big"]:
        return "big"
    if amount_yen >= th["mid"]:
        return "mid"
    return "small"


def classify_tick_tiers(ticks, thresholds=None):
    """
    tickのリスト([{time,price,tickvol,bid,ask,...}, ...])を1件ずつ
    金額(price*tickvol)算出 -> tier分類 -> direction推定し、
    分類結果付きのリストを返す純関数。

    各出力要素: {time, price, tickvol, amount_yen, tier, direction}

    - tickvol欠損/0以下、priceが欠損の行はスキップする(出力に含めない)。
    - 空/None入力 -> [] を返す。
    """
    out = []
    if not ticks:
        return out
    prev_price = None
    for t in ticks:
        if not isinstance(t, dict):
            continue
        price = _to_float(t.get("price"))
        vol = _to_float(t.get("tickvol"))
        if price is None:
            # priceだけ拾えても方向判定用に前値として引き継ぐ(volが無くてもtick testは維持)
            continue
        if vol is None or vol <= 0:
            prev_price = price
            continue
        amount = price * vol
        tier = classify_tick_tier(amount, thresholds)
        direction = infer_direction(t, prev_price=prev_price)
        out.append({
            "time": t.get("time"),
            "price": price,
            "tickvol": vol,
            "amount_yen": amount,
            "tier": tier,
            "direction": direction,
        })
        prev_price = price
    return out


# ============================================================================
# 集計(moomoo parse_capital_distribution() と同じ出力形状に揃える)
# ============================================================================
def aggregate_tier_flow(classified_ticks):
    """
    classify_tick_tiers() の出力を集計し、moomoo_source.parse_capital_distribution()
    と同じ出力形状 {tier: {in,out,net}} に揃える(比較しやすくするため)。
    単位=円(呼び出し側で百万円/億円に換算してよい)。
    direction不明(None)のtickは in/out どちらにも計上しない(除外・netにも入らない)。
    空/None入力でも {super:{in:0,out:0,net:0}, ...} の形は返す(全tier0)。純関数。

    ⚠️⚠️警告(実運用者向け・必読): この関数が返す net(=in-out・買い優勢/売り優勢)は
    **信頼性が低い**。2026-08-27の5点校正でbid/ask反転補正後もtierごとのnet方向の
    moomoo実測との符号一致率は30-50%(ほぼ五分五分〜やや悪い)にとどまった
    (2026-07-10 トレPJ `retail_flow_proxy` の先行事例「プリント≠プレイヤー」と同型の
    構造的限界。モジュールdocstring参照)。
    **実運用では方向(in/out/net)を使わず、tierごとの出来高シェア(サイズ分類)のみに
    使うこと**。サイズシェアが目的なら本関数でなく estimate_tier_size_shares() を使う。
    """
    out = {tier: {"in": 0.0, "out": 0.0, "net": 0.0} for tier in TIER_ORDER}
    for c in (classified_ticks or []):
        if not isinstance(c, dict):
            continue
        tier = c.get("tier")
        direction = c.get("direction")
        amount = c.get("amount_yen")
        if tier not in out or amount is None:
            continue
        if direction == "BUY":
            out[tier]["in"] += amount
        elif direction == "SELL":
            out[tier]["out"] += amount
    for tier in TIER_ORDER:
        out[tier]["net"] = out[tier]["in"] - out[tier]["out"]
    return out


# ============================================================================
# ★実運用の入口(サイズ分類のみ・方向は一切主張しない) — 2026-08-27新設
# ============================================================================
def aggregate_tier_amount(classified_ticks):
    """
    classify_tick_tiers() の出力から、方向(buy/sell)を一切無視して
    tierごとの合計約定金額(円)だけを集計する純関数。
    ⚠️direction列は読まない・出力にも含めない(サイズのみ)。
    空/None入力でも {super:0.0, big:0.0, mid:0.0, small:0.0} の形は返す。
    """
    out = {tier: 0.0 for tier in TIER_ORDER}
    for c in (classified_ticks or []):
        if not isinstance(c, dict):
            continue
        tier = c.get("tier")
        amount = c.get("amount_yen")
        if tier not in out or amount is None:
            continue
        out[tier] += amount
    return out


def estimate_tier_size_shares(ticks, thresholds=None):
    """
    ★実運用向け公開API(2026-08-27新設)。tickのリストから、tierごとの
    出来高金額シェア(0.0-1.0の構成比)だけを推定する純関数。

    **方向(買い優勢/売り優勢)は一切主張しない・出力にも含まない**。
    理由: aggregate_tier_flow()のnet方向はmoomoo実測との符号一致率30-50%
    (2026-07-10 retail_flow_proxy先行事例と同型の構造的限界)で信頼できない。
    一方、tierごとの出来高シェア(サイズ分類)はmoomoo実測とのスピアマン順位相関
    rho≈0.6-0.8(2026-08-27校正・DEFAULT_THRESHOLDS参照)を確認済みで、
    「サイズだけを見る」用途なら実用に足る。

    内部では classify_tick_tiers() -> aggregate_tier_amount() を呼ぶだけの
    薄いラッパー(direction列は計算されるが本関数の戻り値には一切現れない)。

    戻り値:
      {
        "super": {"amount_yen": float, "share": float},
        "big":   {"amount_yen": float, "share": float},
        "mid":   {"amount_yen": float, "share": float},
        "small": {"amount_yen": float, "share": float},
        "total_amount_yen": float,
        "n_ticks": int,   # 分類対象になったtick数(tickvol<=0/price欠損は除外済み)
      }
    合計金額が0(空入力・全件除外等)の場合は0除算を避け、全shareを0.0にする。
    """
    classified = classify_tick_tiers(ticks, thresholds)
    amounts = aggregate_tier_amount(classified)
    total = sum(amounts.values())
    out = {}
    for tier in TIER_ORDER:
        share = (amounts[tier] / total) if total > 0 else 0.0
        out[tier] = {"amount_yen": amounts[tier], "share": share}
    out["total_amount_yen"] = total
    out["n_ticks"] = len(classified)
    return out


# ============================================================================
# 時間窓での絞り込み(純関数・文字列時刻の辞書式比較)
# ============================================================================
def filter_ticks_window(ticks, start_time=None, end_time=None):
    """
    tickのリストを time列(文字列 'YYYY-MM-DD HH:MM:SS...')の辞書式比較で
    [start_time, end_time] の閉区間へ絞り込む純関数。start/end省略時はその側は無制限。
    time欠損の行は除外。
    """
    out = []
    for t in (ticks or []):
        tm = (t or {}).get("time")
        if not tm:
            continue
        if start_time is not None and tm < start_time:
            continue
        if end_time is not None and tm > end_time:
            continue
        out.append(t)
    return out


# ============================================================================
# I/O補助(ネット非依存・selftest対象外だが単純)
# ============================================================================
def load_ticks_csv(path):
    """kabu tick CSV(time,price,vwap,volume,bid,ask,tickvol)をdictのリストへ読み込む。"""
    out = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            out.append(row)
    return out


if __name__ == "__main__":
    print(__doc__)
    print("selftestは同ディレクトリの selftest_tick_tier_classifier.py を実行してください:")
    print("  python selftest_tick_tier_classifier.py")
