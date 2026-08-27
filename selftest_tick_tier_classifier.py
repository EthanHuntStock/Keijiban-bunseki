# -*- coding: utf-8 -*-
"""
selftest_tick_tier_classifier.py - tick_tier_classifier.py の純関数selftest
(境界値・空入力・合計の整合性)。ネット/pandas非依存でどの環境でも実行できる。

実行: python selftest_tick_tier_classifier.py
"""
import tick_tier_classifier as ttc

_failures = []


def check(name, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        _failures.append(name)


def approx(a, b, eps=1e-6):
    return a is not None and b is not None and abs(a - b) < eps


def main():
    # ---- infer_direction: Lee-Ready近似の境界とフォールバック ----
    # ★kabu tick CSVのbid/ask列は反転している(tick_tier_classifier.infer_direction の
    #   docstring参照・実データで検証済み)。この関数は tick辞書の "bid" 列を真のask、
    #   "ask" 列を真のbidとして解釈するので、以下のテストのbid/ask値もその前提で作る
    #   (=辞書キー名と実際の買気配/売気配が食い違って見えるのは意図通り)。
    check("infer_direction price>=真ask(=tick['bid']) -> BUY",
          ttc.infer_direction({"price": 100, "bid": 100, "ask": 98}) == "BUY")
    check("infer_direction price<=真bid(=tick['ask']) -> SELL",
          ttc.infer_direction({"price": 98, "bid": 100, "ask": 98}) == "SELL")
    check("infer_direction mid-spread falls back to tick test (up)",
          ttc.infer_direction({"price": 99, "bid": 100, "ask": 98}, prev_price=97) == "BUY")
    check("infer_direction mid-spread falls back to tick test (down)",
          ttc.infer_direction({"price": 99, "bid": 100, "ask": 98}, prev_price=101) == "SELL")
    check("infer_direction no bid/ask uses tick test",
          ttc.infer_direction({"price": 105}, prev_price=100) == "BUY")
    check("infer_direction no info at all -> None",
          ttc.infer_direction({"price": 100}) is None)
    check("infer_direction missing price -> None",
          ttc.infer_direction({"bid": 98, "ask": 100}) is None)

    # ---- classify_tick_tier: 4段階の境界値(項目1) ----
    th = {"super": 200_000_000, "big": 50_000_000, "mid": 10_000_000}
    check("classify_tick_tier super boundary (>=) -> super",
          ttc.classify_tick_tier(200_000_000, th) == "super")
    check("classify_tick_tier just under super -> big",
          ttc.classify_tick_tier(199_999_999, th) == "big")
    check("classify_tick_tier big boundary (>=) -> big",
          ttc.classify_tick_tier(50_000_000, th) == "big")
    check("classify_tick_tier mid boundary (>=) -> mid",
          ttc.classify_tick_tier(10_000_000, th) == "mid")
    check("classify_tick_tier just under mid -> small",
          ttc.classify_tick_tier(9_999_999, th) == "small")
    check("classify_tick_tier zero amount -> small",
          ttc.classify_tick_tier(0, th) == "small")
    check("classify_tick_tier None amount -> None",
          ttc.classify_tick_tier(None, th) is None)
    check("classify_tick_tier negative amount -> None",
          ttc.classify_tick_tier(-1, th) is None)

    # ---- classify_tick_tiers: 空入力(項目2) ----
    check("classify_tick_tiers empty list -> []", ttc.classify_tick_tiers([]) == [])
    check("classify_tick_tiers None -> []", ttc.classify_tick_tiers(None) == [])

    # ---- classify_tick_tiers: 一連のtickをend-to-endで分類 ----
    # ★bid/ask列は反転している前提でtick['bid']=真ask・tick['ask']=真bidとして構成(上と同じ理由)。
    ticks = [
        {"time": "2026-08-27 09:00:00", "price": 52000, "tickvol": 100, "bid": 51900, "ask": 52000},   # 5.2M -> small, 真ask=51900<=price -> BUY
        {"time": "2026-08-27 09:00:01", "price": 51900, "tickvol": 1000, "bid": 52100, "ask": 51900},  # 51.9M -> big, 真ask=52100>price・真bid=51900>=price -> SELL
        {"time": "2026-08-27 09:00:02", "price": 52500, "tickvol": 5000, "bid": 52400, "ask": 52300},  # 262.5M -> super, 真ask=52400<=price -> BUY
        {"time": "2026-08-27 09:00:03", "price": 52500, "tickvol": 0, "bid": 52400, "ask": 52500},     # tickvol<=0 -> skip
        {"time": "2026-08-27 09:00:04", "price": None, "tickvol": 100},                                 # price欠損 -> skip
        {"time": "2026-08-27 09:00:05", "price": 52200, "tickvol": 200, "bid": 52300, "ask": 52100},   # 10.44M -> mid, 真ask=52300>price>真bid=52100 -> mid-spread -> tick test vs prev(52500) -> down -> SELL
    ]
    ct = ttc.classify_tick_tiers(ticks, th)
    check("classify_tick_tiers skips zero-vol/None-price rows", len(ct) == 4)
    check("classify_tick_tiers tiers correct",
          [c["tier"] for c in ct] == ["small", "big", "super", "mid"])
    check("classify_tick_tiers directions correct",
          [c["direction"] for c in ct] == ["BUY", "SELL", "BUY", "SELL"])
    check("classify_tick_tiers amount_yen correct (price*tickvol)",
          approx(ct[1]["amount_yen"], 51_900_000.0))

    # ---- aggregate_tier_flow: 合計の整合性(項目3) ----
    agg = ttc.aggregate_tier_flow(ct)
    check("aggregate_tier_flow has all 4 tiers", set(agg.keys()) == set(ttc.TIER_ORDER))
    check("aggregate_tier_flow super in", approx(agg["super"]["in"], 262_500_000.0))
    check("aggregate_tier_flow big out", approx(agg["big"]["out"], 51_900_000.0))
    check("aggregate_tier_flow mid out", approx(agg["mid"]["out"], 10_440_000.0))
    check("aggregate_tier_flow small in", approx(agg["small"]["in"], 5_200_000.0))
    total_in = sum(v["in"] for v in agg.values())
    total_out = sum(v["out"] for v in agg.values())
    total_amount = sum(c["amount_yen"] for c in ct)
    check("aggregate_tier_flow in+out == sum(amount_yen) (このケースはdirection不明が無い)",
          approx(total_in + total_out, total_amount))
    check("aggregate_tier_flow net == in - out for every tier",
          all(approx(v["net"], v["in"] - v["out"]) for v in agg.values()))

    # ---- aggregate_tier_flow: 空入力(項目4) ----
    zero_shape = {tier: {"in": 0.0, "out": 0.0, "net": 0.0} for tier in ttc.TIER_ORDER}
    check("aggregate_tier_flow empty -> all-zero shape", ttc.aggregate_tier_flow([]) == zero_shape)
    check("aggregate_tier_flow None -> all-zero shape", ttc.aggregate_tier_flow(None) == zero_shape)

    # direction不明(bid/ask/前値いずれも無い)のtickはin/outどちらにも計上されない
    ambiguous = ttc.classify_tick_tiers(
        [{"time": "t0", "price": 100, "tickvol": 100}],
        {"super": 1e12, "big": 1e11, "mid": 1e10},
    )
    check("ambiguous direction -> tier is small but direction None",
          ambiguous[0]["direction"] is None and ambiguous[0]["tier"] == "small")
    agg_amb = ttc.aggregate_tier_flow(ambiguous)
    check("ambiguous direction not counted in in/out (除外される)",
          agg_amb["small"]["in"] == 0.0 and agg_amb["small"]["out"] == 0.0)

    # ---- aggregate_tier_amount: 方向無視のtier別金額集計(項目6・2026-08-27新設) ----
    agg_amt = ttc.aggregate_tier_amount(ct)
    check("aggregate_tier_amount has all 4 tiers", set(agg_amt.keys()) == set(ttc.TIER_ORDER))
    check("aggregate_tier_amount super = in(BUY 262.5M)のみ(このケースはsuperにSELLが無い)",
          approx(agg_amt["super"], 262_500_000.0))
    check("aggregate_tier_amount big = 51.9M(direction問わず合算)",
          approx(agg_amt["big"], 51_900_000.0))
    check("aggregate_tier_amount mid = 10.44M(direction問わず合算)",
          approx(agg_amt["mid"], 10_440_000.0))
    check("aggregate_tier_amount small = 5.2M(direction問わず合算)",
          approx(agg_amt["small"], 5_200_000.0))
    check("aggregate_tier_amount empty -> all-zero",
          ttc.aggregate_tier_amount([]) == {tier: 0.0 for tier in ttc.TIER_ORDER})
    check("aggregate_tier_amount None -> all-zero",
          ttc.aggregate_tier_amount(None) == {tier: 0.0 for tier in ttc.TIER_ORDER})
    # directionが不明でも金額集計には計上される(方向を見ない関数であることの確認)
    agg_amt_amb = ttc.aggregate_tier_amount(ambiguous)
    check("aggregate_tier_amount counts ambiguous-direction ticks too (方向無視の確認)",
          approx(agg_amt_amb["small"], 10_000.0))

    # ---- estimate_tier_size_shares: 実運用向け公開API(項目7・2026-08-27新設) ----
    shares = ttc.estimate_tier_size_shares(ticks, th)
    check("estimate_tier_size_shares has all 4 tiers + total + n_ticks",
          set(shares.keys()) == set(ttc.TIER_ORDER) | {"total_amount_yen", "n_ticks"})
    check("estimate_tier_size_shares n_ticks matches classify_tick_tiers output length",
          shares["n_ticks"] == len(ct))
    total_expected = 262_500_000.0 + 51_900_000.0 + 10_440_000.0 + 5_200_000.0
    check("estimate_tier_size_shares total_amount_yen correct",
          approx(shares["total_amount_yen"], total_expected))
    check("estimate_tier_size_shares super share correct",
          approx(shares["super"]["share"], 262_500_000.0 / total_expected))
    check("estimate_tier_size_shares shares sum to 1.0",
          approx(sum(shares[t]["share"] for t in ttc.TIER_ORDER), 1.0))
    check("estimate_tier_size_shares does not leak direction/in/out/net keys (方向を含まない確認)",
          all(set(shares[t].keys()) == {"amount_yen", "share"} for t in ttc.TIER_ORDER))
    empty_shares = ttc.estimate_tier_size_shares([])
    check("estimate_tier_size_shares empty input -> all shares 0.0 (0除算回避)",
          all(empty_shares[t]["share"] == 0.0 for t in ttc.TIER_ORDER)
          and empty_shares["total_amount_yen"] == 0.0 and empty_shares["n_ticks"] == 0)
    check("estimate_tier_size_shares None input -> same as empty",
          ttc.estimate_tier_size_shares(None) == empty_shares)
    check("estimate_tier_size_shares uses DEFAULT_THRESHOLDS when thresholds omitted",
          ttc.estimate_tier_size_shares(ticks) ==
          ttc.estimate_tier_size_shares(ticks, ttc.DEFAULT_THRESHOLDS))

    # ---- filter_ticks_window: 窓の境界(項目5) ----
    win_ticks = [
        {"time": "2026-08-27 09:00:00"},
        {"time": "2026-08-27 09:10:00"},
        {"time": "2026-08-27 09:20:00"},
        {"time": None},
    ]
    w = ttc.filter_ticks_window(win_ticks, start_time="2026-08-27 09:05:00", end_time="2026-08-27 09:15:00")
    check("filter_ticks_window keeps only in-range + drops missing time",
          len(w) == 1 and w[0]["time"] == "2026-08-27 09:10:00")
    check("filter_ticks_window no bounds -> passthrough minus missing-time rows",
          len(ttc.filter_ticks_window(win_ticks)) == 3)
    check("filter_ticks_window empty -> []",
          ttc.filter_ticks_window([]) == [] and ttc.filter_ticks_window(None) == [])

    print(f"\n{'PASS' if not _failures else 'FAIL'}: {len(_failures)} failure(s)")
    for name in _failures:
        print("  - " + name)
    return len(_failures)


if __name__ == "__main__":
    import sys
    sys.exit(1 if main() else 0)
