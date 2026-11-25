# -*- coding: utf-8 -*-
"""2026-11-25 Gemini助言6点(話題枯れ同時刻比・他銘柄ピア除外・セリクラ成立チェック等)の自己テスト。ネット/実データ非依存。
実行: PYTHONDONTWRITEBYTECODE=1 py -3.14 -B selftest_gemini6_20261125.py"""
import os, sys, datetime as dt
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals as S
import selloff_check as SC
OK, NG = [], []
def check(n, c, i=""):
    (OK if c else NG).append(n); print(("[OK  ] " if c else "[FAIL] ") + n + ("" if c else "  " + str(i)))

# ---- ① 話題枯れ: 日中の部分日は全日比だと誤点灯、同時刻比なら平常
def rows_for(day, per_hour, upto):
    return [{"id": f"{day}-{h}-{i}", "ts": f"{day}T{h:02d}:30:00", "text": "x"} for h in range(upto + 1) for i in range(per_hour)]
raw = []
days = [dt.date(2026, 11, 1) + dt.timedelta(days=i) for i in range(20)]
for k, d in enumerate(days):           # 過去20日: 1日24h x (40±)件
    raw += rows_for(d.isoformat(), 40 + (k % 5) * 3, 23)
today = "2026-11-25"
raw += rows_for(today, 46, 9)          # 当日は10時まで(0-9時)=同ペース
S_ = S.compute_signals([], raw_rows=raw, day=today)
cd = {c["name"]: c for c in S_["cards"]}
check("①従来z(全日比)は部分日で強く負(誤点灯の再現)", S_["posts_z"] is not None and S_["posts_z"] < -1.0, S_["posts_z"])
check("①同時刻比zは平常域(話題枯れOK)", S_["posts_z_sametime"] is not None and S_["posts_z_sametime"] > -0.5 and cd["話題枯れ"]["state"] == "OK", (S_["posts_z_sametime"], cd["話題枯れ"]))
check("①カードに『同時刻比』表記", "同時刻比" in cd["話題枯れ"]["note"])
raw2 = [r for r in raw if not r["ts"].startswith(today)] + rows_for(today, 8, 9)
S2 = S.compute_signals([], raw_rows=raw2, day=today)
check("①本当に閑散なら同時刻比でも話題枯れ発火", {c["name"]: c for c in S2["cards"]}["話題枯れ"]["state"] == "発火", S2["posts_z_sametime"])
S3 = S.compute_signals([], raw_rows=[r for r in raw if not r["ts"].startswith(today)] + rows_for(today, 40, 23), day=today)
check("①全日(23時まで)は従来zと同一", S3["posts_z"] == S3["posts_z_sametime"], (S3["posts_z"], S3["posts_z_sametime"]))
import config
config.SIG_POST_Z_SAMETIME = False
S4 = S.compute_signals([], raw_rows=raw, day=today)
check("①切替False=従来挙動(カードは全日比)", S4["posts_z_sametime"] is None and "全日比" in {c["name"]: c for c in S4["cards"]}["話題枯れ"]["note"])
config.SIG_POST_Z_SAMETIME = True

# ---- ② 他銘柄混入: ピア除外
t = ["MUが上がった", "8035と6857", "SOXL強い", "SK ハイニックス決算", "アドバンテスト好調", "MUST", "7203買い", "トヨタとマイクロン", "2026年", "ソフトバンク"]
a = S.other_symbol_ratio(t)
b = S.other_symbol_ratio(t, exclude_peers=False)
check("②ピアのみの投稿は他銘柄に数えない", a["hits"] == 3 and a["peer_hits"] == 4, a)
check("②従来定義(ratio_incl_peer)は保持", a["ratio_incl_peer"] == b["ratio"] and b["hits"] == 7, (a, b))
check("②ピア+無関係銘柄の併記は『他銘柄あり』", S.other_symbol_ratio(["マイクロンとトヨタ"])["hits"] == 1)
check("②年号・MUSTは誤爆しない", S.other_symbol_ratio(["2026年MUST"])["hits"] == 0)

# ---- ⑥ セリクラ成立チェック(合成tick)
D = dt.datetime
def mk(minute_prices, vols):
    tk, cum = [], 0.0
    for i, (p, v) in enumerate(zip(minute_prices, vols)):
        t0 = D(2026, 11, 25, 10, 0) + dt.timedelta(minutes=i)
        for j, pp in enumerate(p):
            cum += v / len(p)
            tk.append({"t": t0 + dt.timedelta(seconds=10 * j + 1), "price": pp, "vwap": 1000.0, "volume": cum})
    return tk
# 0-9分: 平常(出来高10万)・10-11分: 急落+出来高急増・12-: 安値切り上げ
mp = [[1000, 1000]] * 10 + [[980, 960]] + [[955, 950]] + [[960, 965], [968, 970], [975, 972], [980, 982], [985, 984], [990, 988]]
vv = [100000] * 10 + [200000, 250000] + [120000] * 6
tk = mk(mp, vv)
bars = SC.minute_bars(tk)
r = SC.low_rise_volume(bars)
check("⑥(c)出来高急増後に安値切り上げ+戻りで点灯", r["ok"] is True and r["cond_volume"] and r["cond_rise"] and r["cond_rebound"], r)
mp2 = mp[:12] + [[950, 948]] * 6
r2 = SC.low_rise_volume(SC.minute_bars(mk(mp2, vv)))
check("⑥(c)安値を更新し続ける間は消灯", r2["ok"] is False, r2)
vv3 = [100000] * 18
r3 = SC.low_rise_volume(SC.minute_bars(mk(mp, vv3)))
check("⑥(c)出来高が増えていなければ消灯", r3["ok"] is False and not r3["cond_volume"], r3)
bp = [{"t": D(2026, 11, 25, 10, 0, 0), "B": 1e6, "S": 1e6}, {"t": D(2026, 11, 25, 10, 3, 5), "B": 1.2e6, "S": 0.9e6}]
check("⑥(b)3分で買い増・売り減=点灯", SC.board_turn(bp)["ok"] is True)
bp2 = [bp[0], {"t": bp[1]["t"], "B": 1.2e6, "S": 1.1e6}]
check("⑥(b)売りも増えていれば消灯", SC.board_turn(bp2)["ok"] is False)
check("⑥(b)データ不足はNone(点灯でも消灯でもない)", SC.board_turn(bp[:1])["ok"] is None)
sel = {"asof_label": "11/25 10:20時点", "board_turn": {"ok": False, "detail": "x"}, "low_rise": {"ok": False, "detail": "y"}, "vwap": {"recovered": False, "detail": "z"}}
lp = SC.lamps(sel, [{"name": "阿鼻叫喚(セリクラ)", "state": "発火"}])
check("⑥心理発火のみ=1/3・『底とは言えません』と明記", lp["n"] == 1 and "底」とは言えません" in lp["caution"].replace("『", "「").replace("』", "」") and lp["title"] == "セリクラ成立チェック(1/3)", lp)
lp3 = SC.lamps(dict(sel, board_turn={"ok": True, "detail": ""}, low_rise={"ok": True, "detail": ""}), [{"name": "阿鼻叫喚(セリクラ)", "state": "発火"}])
check("⑥3点揃っても断定しない文言", lp3["n"] == 3 and "確定ではありません" in lp3["caution"])
check("⑥カード無し=(a)は判定不能(点灯に数えない)", SC.lamps(sel, None)["items"][0]["state"] == "na")
check("⑥VWAP回復は3点に数えない", SC.lamps(dict(sel, vwap={"recovered": True, "detail": ""}), None)["n"] == 0)
# ---- 2026-11-25 モニタ連絡: 大引け後の窓比較非表示・夜間確定モード
import screen_flow_summary as SF
_bp = [{"t": D(2026, 11, 25, 15, 29, 0) + dt.timedelta(seconds=10 * i), "mid": 1.0, "B": 1e6 + 1e4 * i, "S": 1e6} for i in range(12)]
bf_live = SF.board_flow(_bp[:6])
bf_closed = SF.board_flow([dict(p, t=p["t"] + dt.timedelta(minutes=2)) for p in _bp])
check("板: 場中(15:30前)はclosed=False・窓別を表示", bf_live["closed"] is False and any("直近30秒" in x for x in SF.lines_board(bf_live)))
check("板: 大引け後はclosed=True・窓別を出さず理由を明記", bf_closed["closed"] is True and not any("直近30秒" in x for x in SF.lines_board(bf_closed)) and any("大引け" in x for x in SF.lines_board(bf_closed)))
check("板: 公開用抽出にclosedが残る", SF.public_safe_extras(bf_closed, None, None)["board_flow"]["closed"] is True)
tk_n = [dict(t, t=t["t"].replace(hour=15, minute=31 + (i % 2), second=0)) for i, t in enumerate(tk)]  # 全tick大引け後
tk_n = tk + [{"t": D(2026, 11, 25, 15, 31, 5), "price": 900.0, "vwap": 1000.0, "volume": tk[-1]["volume"] + 1}]
sel_n = SC.compute(tk_n, bp)
sel_d = SC.compute(tk, bp)
check("夜間: 大引け後tickがあれば final=True・ラベルに確定", sel_n.get("final") is True and "確定" in sel_n["asof_label"], sel_n.get("asof_label"))
check("夜間: 大引け後の900円tickが(c)判定に混ざらない(15:30までで評価)", sel_n["low_rise"].get("low") != 900.0, sel_n["low_rise"])
check("場中: final無し", not sel_d.get("final"))
lpf = SC.lamps(dict(sel, final=True), [{"name": "阿鼻叫喚(セリクラ)", "state": "発火"}])
check("夜間: ランプは過去形の確定結果(進行形でない)", lpf["final"] and "確定" in lpf["title"] and "確定結果" in lpf["caution"] and "ありませんでした" in lpf["caution"], lpf)
check("場中: 従来タイトルのまま", SC.lamps(sel, None)["title"] == "セリクラ成立チェック(0/3)")
check("公開表示: 他銘柄混入率の説明にピア除外を明記", "ピア" in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_dashboard.py"), encoding="utf-8").read().split("\"他銘柄混入率\":")[1][:200])
# ---- 統計評価タブの結論(表示専用・数値は変えない)
import stats_explain as SE
def _c(m, h, r, t, d, n): return {"metric_name": m, "horizon_name": h, "r": r, "t": t, "diff_pt": d, "n": n}
check("統計: 強く揃えば✅", SE.cell_level(_c("a", "翌1日", 0.45, 3.4, 2.0, 40)) == "trust")
check("統計: 件数が少なければ必ず🟡(強くても)", SE.cell_level(_c("a", "翌1日", 0.6, 4.0, 2.0, 12)) == "unknown")
check("統計: rとグループ差の向きが食い違えば✅にしない", SE.cell_level(_c("a", "翌1日", 0.45, -3.4, -2.0, 40)) == "unknown")
check("統計: 全て小さければ➖", SE.cell_level(_c("a", "翌1日", 0.03, 0.5, 0.1, 40)) == "none")
_rows = [_c("A", "翌1日", 0.03, -2.17, -5.4, 41), _c("A", "翌3日", 0.1, -1.4, -6.3, 39), _c("B", "翌1日", 0.02, 0.2, 0.1, 41), _c("B", "翌3日", 0.0, 0.1, 0.0, 39)]
_vs = SE.verdicts(_rows)
check("統計: 指標ごとに🟡/➖が付き並び順保持・summaryに内訳", [v["level"] for v in _vs] == ["unknown", "none"] and [v["metric"] for v in _vs] == ["A", "B"] and "🟡まだ不明 1" in SE.summary_line(_vs), _vs)
check("統計: Markdown表を壊す縦線を凡例に含まない", all("|" not in x for g in SE.GLOSSARY for x in g))
print("\n結果: OK=%d NG=%d" % (len(OK), len(NG)))
sys.exit(1 if NG else 0)
