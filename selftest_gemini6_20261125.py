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
# ---- 2026-11-25 ユーザー承認: 昼休み非表示・前営業日の確定表示
def _bf_at(h, m):
    t0 = D(2026, 11, 25, h, m, 0)   # 水曜
    return SF.board_flow([{"t": t0 + dt.timedelta(seconds=10 * i), "mid": 1.0, "B": 1e6 + 1e4 * i, "S": 1e6} for i in range(40)])
bf_l = _bf_at(11, 40)
check("昼休み(11:30-12:30)はlunch=True・窓別を出さず理由を明記", bf_l["lunch"] is True and not any("直近30秒" in x for x in SF.lines_board(bf_l)) and any("昼休み(11:30-12:30)は板が静止するため窓別の増減は意味がなく表示しません" in x for x in SF.lines_board(bf_l)))
check("昼休み: 公開用抽出にlunchが残り、旧レコード(lunchキー無し)も時刻で救済", SF.public_safe_extras(bf_l, None, None)["board_flow"]["lunch"] is True and not any("直近30秒" in x for x in SF.lines_board({k: v for k, v in bf_l.items() if k != "lunch"})))
bf_m = _bf_at(10, 0)
bf_p = _bf_at(13, 0)
check("場中(前場10時・後場13時)は従来どおり窓別を表示", bf_m["lunch"] is False and bf_p["lunch"] is False and any("直近30秒" in x for x in SF.lines_board(bf_m)) and any("直近30秒" in x for x in SF.lines_board(bf_p)))
check("昼休みの境界(11:29台=場中・12:30台=場中)", _bf_at(11, 20)["lunch"] is False and SF._is_lunch(D(2026, 11, 25, 12, 29, 59)) is True and SF._is_lunch(D(2026, 11, 25, 12, 30, 0)) is False and SF._is_lunch(D(2026, 11, 25, 11, 30, 0)) is True)
check("昼休み: 土曜は対象外(平日判定のみ)", SF._is_lunch(D(2026, 11, 28, 12, 0, 0)) is False)
# 前営業日CSVの選択(read_ticksを差し替え)
_ok = {"2026-11-20": tk}
_fn = lambda path: _ok.get(os.path.basename(path)[len("ticks_285A_"):-4], [])
pv = SF.find_prev_session("2026-11-26", tick_fn=_fn)
check("前営業日: 11-26なら11-25..21が無い→11-20(存在する最新)を選ぶ", pv is not None and pv[0] == "2026-11-20", pv and pv[0])
_ok["2026-11-25"] = tk
check("前営業日: 11-25のCSVが有れば11-25(最新)を選ぶ", SF.find_prev_session("2026-11-26", tick_fn=_fn)[0] == "2026-11-25")
check("前営業日: 当日自身は対象にしない(11-25の前は11-20)", SF.find_prev_session("2026-11-25", tick_fn=_fn)[0] == "2026-11-20")
check("前営業日: 10日より前にしか無い場合はNone(非表示)", SF.find_prev_session("2026-12-06", tick_fn=_fn) is None)
check("前営業日: 無ければNone", SF.find_prev_session("2026-11-26", tick_fn=lambda p: []) is None)
check("前営業日: 寄り前tickだけの日はスキップ", SF.find_prev_session("2026-11-26", tick_fn=lambda p: [{"t": D(2026, 11, 25, 8, 55), "price": 1.0, "vwap": 1.0, "volume": 1.0}]) is None)
sel_pv = SC.compute(tk_n, bp, prev_date="2026-11-25")
lp_pv = SC.lamps(sel_pv, [{"name": "阿鼻叫喚(セリクラ)", "state": "発火"}])
check("前営業日: 確定モード・15:30までで評価・ラベル/タイトルに前営業日と確定", sel_pv["final"] is True and sel_pv["low_rise"].get("low") != 900.0 and lp_pv["title"].startswith("前営業日(2026-11-25)のセリクラ成立チェック・大引け時点で確定(") and "前営業日(2026-11-25)の大引け時点" in lp_pv["caution"], (sel_pv, lp_pv))
check("前営業日: 当日のカード(a)は数えない(判定対象外)", lp_pv["items"][0]["state"] == "na" and lp_pv["n"] <= 2)
check("前営業日でない通常表示のタイトルは従来どおり", SC.lamps(sel, None)["title"] == "セリクラ成立チェック(0/3)" and "前営業日" not in lpf["title"] and lpf["title"].startswith("本日の"))
# ---- 2026-11-25 ユーザー承認①②③
import trend_stats as TS, numeric_summary as NS
import datetime as _dtm
_JST = _dtm.timezone(_dtm.timedelta(hours=9))
def _ts(y, mo, d, h, mi): return _dtm.datetime(y, mo, d, h, mi, tzinfo=_JST).timestamp()
def _tr(last_ts, price, c5, c60):
    return {"last_price": price, "last_time": _dtm.datetime.fromtimestamp(last_ts, _JST).strftime("%m/%d %H:%M"), "last_ts": last_ts,
            "chg": {"5m": c5, "15m": None, "60m": c60, "180m": None}, "hi": price + 10, "lo": price - 10, "n": 20}
_now = _ts(2026, 11, 25, 23, 40)
_adr = _tr(_ts(2026, 11, 25, 6, 55), 3000.0, -1.08, -0.5)      # 約17時間前
_pts = _tr(_ts(2026, 11, 25, 23, 38), 6700.0, -0.2, -0.4)
_dex = _tr(_ts(2026, 11, 25, 23, 39), 6740.0, 0.1, -0.3)
_tse = _tr(_ts(2026, 11, 25, 15, 30), 6895.0, 0.0, 0.1)
fr, nt = TS.split_stale({"米国ADR": _adr, "PTS": _pts, "DEX": _dex, "東証": _tse}, _now)
check("①最終約定から120分超(ADR・東証)は除外・PTS/DEXは残る", set(fr) == {"PTS", "DEX"} and len(nt) == 2, (list(fr), nt))
check("①注記は『最終 M/D HH:MM・立会時間外』形式", "米国ADR 最終 11/25 06:55・立会時間外" in nt and "東証 最終 11/25 15:30・立会時間外" in nt, nt)
fr2, nt2 = TS.split_stale({"X": _tr(_now - 119 * 60, 1.0, 0.1, 0.1), "Y": _tr(_now - 121 * 60, 1.0, 0.1, 0.1)}, _now)
check("①境界: 119分は残り121分は除外", list(fr2) == ["X"] and len(nt2) == 1)
rec = {"extended_hours": {"trend": {"tse": _tse, "pts": _pts, "adr": _adr, "dex": _dex},
                         "pts": {"change_pct": -2.8, "price": 6700.0, "time": "23:38"}, "dex": {"change_pct": -2.2, "price": 6740.0, "time": "23:39"},
                         "adr": {"change_pct": -3.0, "price_yen": 6600.0, "time": "06:55"}}}
txt = "\n".join(NS.build_numeric_summary(rec, now_ts=_now)["lines"])
check("①夜間: ADRの『5分 -1.08%』『下落が加速』を残さない", "-1.08" not in txt and "米国ADR: " not in txt.split("立会時間外の市場")[-1].split("\n")[0] and "立会時間外の市場(直近変化は出さない): " in txt and "米国ADR 最終 11/25 06:55・立会時間外" in txt, txt)
check("①夜間: 直近変化の行にPTS/DEXは残る", "直近変化: PTS" in txt)
txt_live = "\n".join(NS.build_numeric_summary(rec, now_ts=_ts(2026, 11, 25, 7, 10))["lines"])
check("①ADRが120分以内なら従来どおり直近変化に載る", "米国ADR 5分 -1.08%" in txt_live)
# ③ 前日安値割れ
rd = TS.reading({"PTS": _pts, "DEX": _dex}, {"PTS": -2.8, "DEX": -2.2}, now=_dtm.datetime(2026, 11, 25, 23, 40), prices={"PTS": 6700.0, "DEX": 6760.0}, prev_low=6753.0)
lvl = [x for x in rd if x.startswith("東証最終値比の水準")][0]
check("③PTSが前日安値(6,753円)を下回れば水準記述に『前日安値割れ(下値模索の目安・予告ではない)』", "PTS 6,700円" in lvl and "前日安値割れ(下値模索の目安・予告ではない)" in lvl and "DEX 6,760" not in lvl, lvl)
rd2 = TS.reading({"PTS": _pts, "DEX": _dex}, {"PTS": -2.8, "DEX": -2.2}, now=_dtm.datetime(2026, 11, 25, 23, 40), prices={"PTS": 6760.0, "DEX": 6800.0}, prev_low=6753.0)
check("③割れていなければ何も足さない", not any("前日安値割れ" in x for x in rd2))
check("③prev_low無しなら何も足さない", not any("前日安値割れ" in x for x in TS.reading({"PTS": _pts}, {"PTS": -2.8}, now=_dtm.datetime(2026, 11, 25, 23, 40), prices={"PTS": 1.0})))
rd3 = TS.reading({"PTS": _pts}, {"PTS": -2.8}, now=_dtm.datetime(2026, 11, 25, 10, 0), prices={"PTS": 6700.0}, prev_low=6753.0)
check("③立会時間中は足さない", not any("前日安値割れ" in x for x in rd3))
_okk = {"2026-11-25": [{"t": D(2026, 11, 25, 9, 1), "price": 7000.0, "vwap": 1, "volume": 1}, {"t": D(2026, 11, 25, 13, 0), "price": 6753.0, "vwap": 1, "volume": 2}, {"t": D(2026, 11, 25, 15, 31), "price": 6000.0, "vwap": 1, "volume": 3}]}
_fk = lambda path: _okk.get(os.path.basename(path)[len("ticks_285A_"):-4], [])
check("③前営業日安値=tick CSVの立会中の最安値(大引け後tickは含めない)・当日CSVが有れば当日", SF.latest_session_low("2026-11-25", tick_fn=_fk) == ("2026-11-25", 6753.0), SF.latest_session_low("2026-11-25", tick_fn=_fk))
check("③日付が変わった後は前営業日CSVから取る", SF.latest_session_low("2026-11-26", tick_fn=_fk) == ("2026-11-25", 6753.0))
check("③CSVが無ければNone", SF.latest_session_low("2026-11-26", tick_fn=lambda p: []) is None)
# ② 除外語の補完
import signals as SG
_codes = ["6857", "8035", "6920", "6146", "7735", "6525", "4062", "5803", "9984", "7011"]
check("②追加コードを含め指定10コードは全てピア除外(他銘柄に数えない)", SG.other_symbol_ratio([c + "が強い" for c in _codes])["hits"] == 0, SG.other_symbol_ratio([c + "が強い" for c in _codes]))
_names = ["マイクロン", "サンディスク", "SKハイニックス", "サムスン", "エヌビディア", "アドテスト", "東エレク", "レーザーテック", "ディスコ", "SCREENが上げ", "KOKUSAIも", "SOX高", "NAND市況", "DRAM価格", "HBM需要"]
check("②指定の名称・語は全てピア除外", SG.other_symbol_ratio(_names)["hits"] == 0, SG.other_symbol_ratio(_names))
check("②過剰除外しない: 無関係銘柄(7203・トヨタ・ソフトバンク)は従来どおり他銘柄", SG.other_symbol_ratio(["7203買い", "トヨタ", "ソフトバンクが", "9984とトヨタ"])["hits"] == 4)
check("②過剰除外しない: 285A/キオクシアの言及だけの投稿は他銘柄に数えない・ピア併記も本体は消えない", SG.other_symbol_ratio(["285Aキオクシア", "285AとDRAM"])["hits"] == 0 and SG.other_symbol_ratio(["285Aと2371"])["hits"] == 1)
check("②英単語screen/hbm(小文字)は誤爆しない", "screen" in SG._PEER_TICKER_RE.sub(" ", "screen shot") )
# ---- 2026-11-25 ユーザー承認④: 共通スイッチバー
import switch_bar as SWB
_h = SWB.switch_bar_html()
check("④バー: 存在・現在ページ=aria-current・青枠on", "id='sw-bar'" in _h and "aria-current='page'" in _h and "sw-i on" in _h and "#3b82f6" in SWB.CSS and "掲示板・板分析" in _h)
check("④バー: 外部リンク先URLと rel=noopener・左が現在ページ/右がモニター", "href='https://ethanhuntstock.github.io/ai-sector-monitor/'" in _h and "rel='noopener noreferrer'" in _h and _h.index("掲示板・板分析") < _h.index("ワールドモニター"))
check("④バー: flex・gap6px・角丸8px・枠1px・既存CSS変数(ダーク/ライト両対応)・375px対応(wrap)", all(x in SWB.CSS for x in ("display:flex", "gap:6px", "border-radius:8px", "1px solid var(--border", "var(--text", "var(--panel", "flex-wrap:wrap")))
_gs = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_static_dashboard.py"), encoding="utf-8").read()
_pd = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_dashboard.py"), encoding="utf-8").read()
check("④バー: 静的版はタイトル(h1)直下・Streamlit版はタイトル直下に配置", "（静的版）</h1>"+chr(10)+"      {_switch_bar_html()}" in _gs and "投資情報"+chr(34)+")"+chr(10)+"    try:" in _pd.replace(chr(13), ""))
print("\n結果: OK=%d NG=%d" % (len(OK), len(NG)))
sys.exit(1 if NG else 0)
