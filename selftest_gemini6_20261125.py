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
check("昼休み前半(11:30-12:00)=lunch(stop)・記録なし/受付停止の文言・窓別なし", bf_l["lunch"] is True and bf_l["lunch_phase"] == "stop" and not any("直近30秒" in x for x in SF.lines_board(bf_l)) and any("前場の取引終了後〜12:00は記録が無く、注文の受付も止まっているため窓別は表示しません" in x for x in SF.lines_board(bf_l)))
import tempfile, json as _json, csv as _csv
def _mk_postlunch(path, n=80):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["ts", "symbol", "current_price", "current_price_status", "calc_indicative", "bid_price", "bid_qty", "ask_price", "ask_qty", "over_sell_qty", "under_buy_qty", "market_sell_qty", "market_buy_qty", "raw_json"])
        for i in range(n):
            t = D(2026, 11, 25, 12, 9, 0) + dt.timedelta(seconds=5 * i)
            j = {("Buy%d" % k): {"Price": 1000 - k, "Qty": 100.0 * (i + 1)} for k in range(1, 11)}
            j.update({("Sell%d" % k): {"Price": 1000 + k, "Qty": 50.0} for k in range(1, 11)})
            w.writerow([t.strftime("%Y-%m-%d %H:%M:%S"), "285A", 1000, 1, 1000, 999, 100, 1001, 50, 1000.0, 2000.0, 0, 0, _json.dumps(j)])
_pl_path = os.path.join(tempfile.mkdtemp(), "postlunch_board_285A_2026-11-25.csv")
_mk_postlunch(_pl_path)
_pl = SF.read_postlunch_board(_pl_path)
check("後場寄り前の専用記録: 買い=Buy1-10+UnderBuy・売り=Sell1-10+OverSell(成行は足さない)", abs(_pl[0]["B"] - (100.0 * 10 + 2000.0)) < 1e-6 and abs(_pl[0]["S"] - (50.0 * 10 + 1000.0)) < 1e-6, _pl[0])
bf_pre = SF.board_flow(_pl[:60], source="postlunch")
_ln = SF.lines_board(bf_pre)
check("12:10台(専用記録)=窓別を表示・出所『後場寄り前の板(12:00-12:30の専用記録)』を明記・昼休み非表示にしない", bf_pre["lunch"] is False and any("直近30秒" in x for x in _ln) and any("直近5分" in x for x in _ln) and any("後場寄り前の板(12:00-12:30の専用記録)" in x for x in _ln) and not any("表示しません" in x for x in _ln), _ln)
check("12:10台: 公開抽出・旧キー無しでも出所/窓別が残る", SF.public_safe_extras(bf_pre, None, None)["board_flow"].get("source_note") == SF.POSTLUNCH_LABEL and any("直近30秒" in x for x in SF.lines_board(SF.public_safe_extras(bf_pre, None, None)["board_flow"])))
bf_nopl = _bf_at(12, 10)
check("12:10台で専用記録が無い場合は誤った数値を出さず理由を明記(記録なしの旨)", bf_nopl["lunch_phase"] == "pre" and not any("直近30秒" in x for x in SF.lines_board(bf_nopl)) and any("専用記録を取得できない" in x for x in SF.lines_board(bf_nopl)))
check("load_today_public: 12:10は専用記録帯・11:40/12:31は帯の外(平日のみ)", SF.in_postlunch_band(D(2026, 11, 25, 12, 10)) and not SF.in_postlunch_band(D(2026, 11, 25, 11, 40)) and not SF.in_postlunch_band(D(2026, 11, 25, 12, 31)) and not SF.in_postlunch_band(D(2026, 11, 28, 12, 10)))
bf_pm = _bf_at(12, 31)
check("12:31以降は従来どおり窓別を表示", bf_pm["lunch"] is False and bf_pm["lunch_phase"] == "" and any("直近30秒" in x for x in SF.lines_board(bf_pm)))
check("昼休み区間の境界(11:59:59=stop・12:00:00=pre)・旧レコードも時刻で救済", SF._lunch_phase(D(2026, 11, 25, 11, 59, 59)) == "stop" and SF._lunch_phase(D(2026, 11, 25, 12, 0, 0)) == "pre" and any("専用記録" in x for x in SF.lines_board({k: v for k, v in bf_nopl.items() if k not in ("lunch", "lunch_phase")})))
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
# ---- 2026-11-26 UI5点
import ui5 as U5
_rec = {"generated_at": "2026-11-25T23:50:00", "price": {"last": 6895.0, "change_pct": -1.2},
        "board": {"bull_ratio": 0.311, "bear_ratio": 0.641, "post_count_today": 223},
        "extended_hours": {"trend": {"tse": _tse, "pts": _pts, "adr": _adr, "dex": _dex}, "pts": {"change_pct": -2.8}, "dex": {"change_pct": -2.2}},
        "screen_extras": {"board_flow": {"ratio": 0.89, "asof_label": "11/25 15:31時点"}, "selloff": sel_pv}}
rows5 = U5.recent_rows(_rec, now_ts=_now)
tbl = U5.recent_table_html(rows5)
check("UI②直近変化は表(thead・5分/15分/1時間・最終約定)", "<table class='rc'>" in tbl and "<th>5分</th>" in tbl and "<th>最終約定</th>" in tbl and tbl.count("<tr") == 5)
check("UI②立会時間外の行(ADR・東証)はstaleクラス=薄グレー・PTS/DEXは通常", tbl.count("class='stale'") == 2 and "米国ADR(立会時間外)" in tbl and "<tr class=''><td>PTS</td>" in tbl)
check("UI②上昇=pos(#16a34a)・下落=neg(#dc2626)・ダーク/ライト両対応の変数と色", "class='pos'" in tbl and "class='neg'" in tbl and "#16a34a" in U5.CSS and "#dc2626" in U5.CSS and "var(--border" in U5.CSS)
check("UI⑤tabular-nums・横スクロール枠(overflow-x:auto)・375px向けmedia", "font-variant-numeric:tabular-nums" in U5.CSS and "overflow-x:auto" in U5.CSS and "max-width:480px" in U5.CSS)
s3 = U5.summary3(_rec)
h3 = U5.summary3_html(_rec)
check("UI③3行サマリー: 3行・数値の出所時点つき・決定論(同入力で同出力)", len(s3) == 3 and "23:50時点" in s3[0][1] and "-1.20%" in s3[0][1] and "弱気 64.1%" in s3[2][1] and "大引け時点で確定" in s3[1][1] or "セリクラ" in s3[1][1])
check("UI③3行サマリー: 同入力で同出力・予想/推奨語を含めず『推奨ではありません』の注記は1回", U5.summary3(_rec) == s3 and h3.count("id='sum3'") == 1 and not any(w in h3 for w in ("上がる", "下がる", "買い推奨", "売り推奨")))
check("UI③データ無しはクラッシュせず『データなし』", all(v for _, v in U5.summary3({})) and "データなし" in U5.summary3({})[2][1])
check("UI④免責: 集約リストに既存文言を保持(売買の推奨・売買シグナル・投資助言)", any("売買の推奨ではありません" in x for x in U5.DISCLAIMER_ITEMS) and any("売買シグナルではありません" in x for x in U5.DISCLAIMER_ITEMS) and any("投資助言ではありません" in x for x in U5.DISCLAIMER_ITEMS))
_ex_l = SF.public_extras_lines({"forecast_score": {"label": "x", "count": {"○": 1, "△": 0, "×": 0}, "n_graded": 1, "n_pending": 0, "items": []}}, with_disclaimer=False)
check("UI④見立て採点の行は集約時に『売買の推奨ではありません』を重ねない・既定は従来どおり付く", not any("売買の推奨ではありません" in x for x in _ex_l) and any("売買の推奨ではありません" in x for x in SF.public_extras_lines({"forecast_score": {"label": "x", "count": {}, "n_graded": 0, "n_pending": 0, "items": []}})))
check("UI①用語説明はdetails(既定は閉)に畳む(静的版)", _gs.count('<details class="fold">') >= 2 and '<details class="fold" open' not in _gs)
# ---- 2026-11-26 Gemini5点
_t1 = lambda d, price: [{"t": D(int(d[:4]), int(d[5:7]), int(d[8:10]), 9, 5), "price": price, "vwap": 1.0, "volume": 1.0}, {"t": D(int(d[:4]), int(d[5:7]), int(d[8:10]), 15, 31), "price": price + 1, "vwap": 1.0, "volume": 2.0}]
_okc = {"2026-11-24": _t1("2026-11-24", 8033.0), "2026-11-25": _t1("2026-11-25", 6894.0)}
_fc = lambda path: _okc.get(os.path.basename(path)[len("ticks_285A_"):-4], [])
_sc1 = SF.session_change("2026-11-26", tick_fn=_fc)
check("G1: 日付またぎは直近確定営業日(11/25)の終値と前営業日比を算出(8034→6895=-14.18%)", _sc1 is not None and _sc1["close"] == 6895.0 and _sc1["prev_close"] == 8034.0 and _sc1["pct"] == -14.18 and _sc1["label"] == "11/25終値・前営業日比", _sc1)
check("G1: 当日の立会tickが既にあれば何もしない(場中・大引け後は従来系列)", SF.session_change("2026-11-25", tick_fn=_fc) is None)
_okc2 = {"2026-11-25": _t1("2026-11-25", 6894.0)}
check("G1: 前営業日が無ければNone(『—』のまま・捏造しない)", SF.session_change("2026-11-26", tick_fn=lambda p: _okc2.get(os.path.basename(p)[len("ticks_285A_"):-4], [])) is None)
_r1 = {"generated_at": "2026-11-26T00:27:00", "price": {"last": 6895.0, "change_pct": None}}
_orig_sc = SF.session_change
SF.session_change = lambda date, tick_fn=None: _orig_sc(date, tick_fn=_fc)
U5.fill_prev_session_change(_r1, "2026-11-26")
SF.session_change = _orig_sc
check("G1: priceに騰落率と根拠ラベルが入り、3行サマリーに『11/25終値・前営業日比 -14.18%』と出る", _r1["price"]["change_pct"] == -14.18 and "11/25終値・前営業日比 -14.18%" in U5.summary3(_r1)[0][1], U5.summary3(_r1)[0])
# G2: 0時台の話題枯れ誤発火(同時刻累計が標本なし→全日比へ落ちていた)
_raw = []
for _k in range(12):   # 過去12日: 毎時100件(日計2400件)
    _d = (dt.date(2026, 11, 13) + dt.timedelta(days=_k))
    for _h in range(24):
        _raw += [{"id": "%s-%d-%d" % (_d, _h, _i), "ts": "%sT%02d:%02d:00" % (_d, _h, (_i * 7 + _k) % 60), "text": "x"} for _i in range(100 + (_k % 3))]
_day = "2026-11-26"
_raw += [{"id": "n%d" % _i, "ts": "%sT00:%02d:00" % (_day, min(27, _i % 28)), "text": "x"} for _i in range(100)]   # 00:00〜00:27に100件=過去と同ペース
_g2 = S.compute_signals([], raw_rows=_raw, day=_day)
_cg2 = {c["name"]: c for c in _g2["cards"]}
check("G2(再現): 全日比zは0時台で強く負(従来はこれで話題枯れが誤発火)", _g2["posts_z"] is not None and _g2["posts_z"] < -1.0, _g2["posts_z"])
check("G2(修正): 0時台は直近60分・同時刻帯比で判定し、平常ペースなら話題枯れは発火しない", _g2["posts_z_sametime"] is not None and abs(_g2["posts_z_sametime"]) < 2 and _cg2["話題枯れ"]["state"] != "発火" and "直近60分・同時刻帯比" in _cg2["話題枯れ"]["note"], (_g2["posts_z_sametime"], _cg2["話題枯れ"]))
_raw_q = [r for r in _raw if not r["ts"].startswith(_day)] + [{"id": "q1", "ts": _day + "T00:20:00", "text": "x"}]
_g2q = S.compute_signals([], raw_rows=_raw_q, day=_day)
check("G2: 本当に閑散(直近60分1件)なら話題枯れ発火", {c["name"]: c for c in _g2q["cards"]}["話題枯れ"]["state"] == "発火")
_raw_s = [r for r in _raw if not r["ts"].startswith(_day)] + [{"id": "s%d" % _i, "ts": _day + "T00:%02d:00" % (_i % 28), "text": "x"} for _i in range(1500)]
check("G2: 直近60分が過去の同時刻帯より大幅に多ければ投稿サージ発火", {c["name"]: c for c in S.compute_signals([], raw_rows=_raw_s, day=_day)["cards"]}["投稿サージ"]["state"] == "発火")
_raw_h = [r for r in _raw if r["ts"][:10] >= "2026-11-24"] + [{"id": "h", "ts": _day + "T00:10:00", "text": "x"}]
check("G2: 履歴不足は全日比へ落とさず較正中(OK・誤発火しない)", {c["name"]: c for c in S.compute_signals([], raw_rows=_raw_h, day=_day)["cards"]}["話題枯れ"]["state"] == "OK")
# G3
check("G3a: 最上部の大きな姉妹サイトバナーを削除(静的版はAI考察タブ末尾の再掲のみ・スイッチバーは残る)", _gs.count('class="sister-card" href') == 1 and "_switch_bar_html()" in _gs and "ワールドモニターを開く" not in _pd.split("def main")[1][:6000])
check("G3b: 読み方ガイドは<details>既定閉(静的はopen無し・StreamlitはexpanderでTrue指定なし)", '<details class="panel">' in _gs and '<details class="panel" open' not in _gs and 'st.expander("📖 このダッシュボードの読み方（初めての方向け）"' in _pd and "expanded=True" not in _pd.split("このダッシュボードの読み方（初めての方向け）")[1][:80])
_sel_f = SC.compute(tk_n, bp, prev_date="2026-11-25")
_r3 = {"generated_at": "2026-11-26T00:27:00", "price": {"last": 6895.0, "change_pct": -14.18}, "screen_extras": {"selloff": _sel_f}}
_l2 = U5.summary3(_r3)[1][1]
check("G3c: 3行サマリー2行目は確定の時点表示を1回だけ(二重出力なし)", _l2.count("大引け") == 1 and _l2.count("前営業日(2026-11-25)") == 1, _l2)
check("G3d: 表がある時は『夜間PTS・米国ADR・DEX(東証最終値比)』の箇条書きを出さない(静的・Streamlit両方)", _gs.count('x.startswith("夜間PTS・米国ADR・DEX")') == 1 and _pd.count('x.startswith("夜間PTS・米国ADR・DEX")') == 1)
# G4
_h3 = U5.summary3_html(_r3)
check("G4: 3行サマリーのラベルはバッジ(.k)・縦並び・ダーク/ライトは変数/rgba・375px", "class='k'" in _h3 and ".sum3 .k{display:inline-block" in U5.CSS and "flex-direction:column" in U5.CSS and "border-radius:999px" in U5.CSS)
# G5
_peer_txt = ["MUが強い", "SNDK決算", "SKハイニックス", "サムスン", "NVDA", "アドテスト", "TELが上げ", "東エレクが", "レーザーテック", "ディスコ", "SCREEN", "KOKUSAI", "SOXが", "SOXL"]
check("G5: 指定12系統(MU/SNDK/SKハイニックス/サムスン/NVDA/アドテスト/TEL/東エレク/レーザーテック/ディスコ/SCREEN/KOKUSAI/SOX)は全て除外", SG.other_symbol_ratio(_peer_txt)["hits"] == 0, SG.other_symbol_ratio(_peer_txt))
check("G5: 英字は前後が英字でない時のみ(MUST/TELEGRAM/HOTEL/SCREENSHOT/NVDAX は他銘柄扱いにならずピアとしても消費しない)", SG._PEER_TICKER_RE.sub(" ", "MUST TELEGRAM HOTEL SCREENSHOT") == "MUST TELEGRAM HOTEL SCREENSHOT")
check("G5: 285A本文は誤除外されない(285A/キオクシアと無関係銘柄の併記は他銘柄に数える)", SG.other_symbol_ratio(["285Aと7203", "キオクシアとトヨタ", "TELとトヨタ"])["hits"] == 3 and SG.other_symbol_ratio(["285Aキオクシア"])["hits"] == 0)
# ---- 2026-11-26 灼熱メーターの投稿サージ成分
_gm = _g2["gauges"]["components"]["heat"]
_zst = _g2["posts_z_sametime"]
check("灼熱: 0時台の投稿サージ成分は全日比(0固定)でなく話題枯れ/サージと同じz(直近60分・同時刻帯比)に基づく", _g2["posts_z"] < -1.0 and abs(_gm["post_surge"] - max(0.0, min(1.0, _zst / 3.0))) < 1e-9, (_gm, _zst))
_parts = [v for v in _gm.values() if v is not None]
check("灼熱: 合算・正規化は従来どおり(成分の単純平均×100・0-100)", abs(_g2["gauges"]["overheat"] - round(sum(_parts) / len(_parts) * 100.0, 1)) < 1e-9 and 0 <= _g2["gauges"]["overheat"] <= 100)
_gb = S.compute_signals([], raw_rows=_raw_h, day=_day)["gauges"]
check("灼熱: 履歴不足(較正中)は投稿サージ成分を除外し、全日比へも0固定へも落とさない", _gb["components"]["heat"]["post_surge"] is None)
_gq = S.compute_signals([], raw_rows=_raw_s, day=_day)["gauges"]
check("灼熱: 直近60分が過去の同時刻帯より大幅に多ければ成分が高くなる(上限1.0)", _gq["components"]["heat"]["post_surge"] == 1.0)
_g_mid = S.compute_signals([], raw_rows=[r for r in _raw if r["ts"] <= "2026-11-24T09:30:00"], day="2026-11-24")
check("灼熱: 場中(9:30)も同時刻比で成分が決まり全日比に落ちない", _g_mid["posts_z_sametime"] is not None and _g_mid["gauges"]["components"]["heat"]["post_surge"] == max(0.0, min(1.0, _g_mid["posts_z_sametime"] / 3.0)))
config.SIG_POST_Z_SAMETIME = False
_g_off = S.compute_signals([], raw_rows=_raw, day=_day)
config.SIG_POST_Z_SAMETIME = True
check("灼熱: 切替False=従来挙動(全日比z)", _g_off["gauges"]["components"]["heat"]["post_surge"] == max(0.0, min(1.0, _g_off["posts_z"] / 3.0)))
# ---- 2026-11-26 latest.txt
_rec_lt = dict(_rec, ai_commentary={"text": "本文1行目。" + chr(10) + "免責: 本情報は投資助言ではありません。", "generated_at": "2026-11-25T23:50:00"},
               disclaimer="本情報は掲示板投稿を統計的に集計したものであり、投資助言ではありません。", signal_cards=[{"name": "話題枯れ", "state": "OK", "value": 0.54}])
_lt = U5.latest_txt(_rec_lt, "2026-11-25T23:55:00")
check("latest.txt: 10KB以下・UTF-8・主要節(3行サマリー/時間外比較/シグナル/AI考察/免責)を含む", 0 < len(_lt.encode("utf-8")) <= 10240 and all(x in _lt for x in ("## 3行サマリー", "## 時間外・直近変化", "## シグナル発火状況", "## AI考察", "## 免責")))
check("latest.txt: AI考察本文は公開HTMLと同一の文面(免責行も削らない)", "本文1行目。" + chr(10) + "免責: 本情報は投資助言ではありません。" in _lt and _rec_lt["ai_commentary"]["text"] in _lt)
check("latest.txt: 数値は同じrecord由来(現在値・前営業日比・更新時刻)", "6,895円" in _lt and "-1.20%" in _lt and "2026-11-25T23:55:00" in _lt)
_big = dict(_rec_lt, signal_cards=[{"name": "x" * 50 + str(i), "state": "OK", "value": 0.1} for i in range(200)])
_ltb = U5.latest_txt(_big, "t")
check("latest.txt: 10KB超は表・シグナルの節から削りAI考察本文は残す", len(_ltb.encode("utf-8")) <= 10240 and _rec_lt["ai_commentary"]["text"] in _ltb)
check("latest.txt: AI考察が無ければ『準備中』と明記・失敗(不正rec)は空文字で例外を出さない", "AI考察は準備中です" in U5.latest_txt(dict(_rec_lt, ai_commentary={})) and U5.latest_txt(object()) == "")
check("latest.txt: 生成失敗でもindex.htmlの公開は止めない(try/exceptで囲み・index生成の後に実行)", "_ui5.latest_txt(rec, generated_at_str)" in _gs and _gs.index("render_html(rec, images, extras, generated_at_str)") < _gs.index("_ui5.latest_txt(rec, generated_at_str)") and "index.html is unaffected" in _gs)
import publish_static_dashboard as _PUB
_src = tempfile.mkdtemp(); _dst = tempfile.mkdtemp()
open(os.path.join(_src, "index.html"), "w").write("a"); open(os.path.join(_src, "latest.txt"), "w", encoding="utf-8").write("x")
_PUB.sync_docs_dir(_src, _dst)
check("latest.txt: sync_docs_dirがdocsへコピーする", os.path.exists(os.path.join(_dst, "latest.txt")) and os.path.exists(os.path.join(_dst, "index.html")))
# ---- 2026-11-26 除外辞書の照合・考察『直近の出来事』制約
_all22 = ["MU", "マイクロン", "SNDK", "サンディスク", "SKハイニックス", "ハイニックス", "サムスン", "NVDA", "エヌビディア", "アドテスト", "アドバンテスト", "TEL", "東京エレクトロン", "東エレク", "レーザーテック", "ディスコ", "SCREEN", "KOKUSAI", "SOX", "NAND", "DRAM", "HBM"]
check("除外辞書: 指定22語は全て単独投稿で他銘柄に数えない(漏れなし)", all(SG.other_symbol_ratio([w + "が強い"])["hits"] == 0 for w in _all22), [w for w in _all22 if SG.other_symbol_ratio([w + "が強い"])["hits"] != 0])
check("除外辞書: ASCII語は語境界(MUST/TELEGRAM/HOTEL/NANDA/DRAMA は消費しない)", SG._PEER_TICKER_RE.sub(" ", "MUST TELEGRAM HOTEL NANDA DRAMA HBMX") == "MUST TELEGRAM HOTEL NANDA DRAMA HBMX")
import claude_commentary_intake as ICK
_t_ok = "■ 直近の出来事" + chr(10) + "11-25の終値は-14.18%でした。同日、子会社から配当2兆2,149億円を受け取りました。" + chr(10) + chr(10) + "■ 次" + chr(10) + "11-18は+1%"
_t_ng = "■ 直近の出来事" + chr(10) + "".join("11-%02dは%+d%%、" % (25 - i, i + 1) for i in range(6)) + chr(10)
check("考察の制約: ■直近の出来事が直近3営業日+適時開示なら適合(他の節の日付は数えない)", ICK.lint_recent_events(_t_ok)["ok"] is True and ICK.lint_recent_events(_t_ok)["n_days"] == 1)
check("考察の制約: 10営業日級の騰落の羅列(4日超)は違反として検出・節が無ければ適合", ICK.lint_recent_events(_t_ng)["ok"] is False and ICK.lint_recent_events(_t_ng)["n_days"] == 6 and ICK.lint_recent_events("節なし")["ok"] is True)
_isrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_commentary_intake.py"), encoding="utf-8").read()
check("考察の制約: 取込は本文を変えず(text/generated_atのみ書く)・警告ログのみで公開は止めない", 'j["ai_commentary"] = {"text": text, "generated_at": gen19}' in _isrc and "公開は止めない" in _isrc and "return \"rejected\"" in _isrc.split("_lint = lint_recent_events")[0])
# ---- 2026-11-26 100の倍数の4桁(株価水準)を銘柄コード扱いしない
_px = ["夢の5000円台突入へ", "6000割れたら強制決済", "3000くらいまでいくでしょ", "7000まで戻る", "1000円台に戻る"]
check("他銘柄: 100の倍数の4桁(5000円台・6000割れ等=285A自身の株価水準)は他銘柄に数えない", SG.other_symbol_ratio(_px)["hits"] == 0, SG.other_symbol_ratio(_px))
_real = ["7203買い", "6758が強い", "8306を売る", "9433の決算", "コード6000の銘柄", "6000銘柄", "6000.T", "6000HDが上げ"]
check("他銘柄: 実銘柄コード(100の倍数以外)と、100の倍数でも『コード/銘柄/.T/HD』の文脈があるものは残る", SG.other_symbol_ratio(_real)["hits"] == len(_real), SG.other_symbol_ratio(_real))
check("他銘柄: 『1000株買った』(株数)や『株価5000』は他銘柄に数えない", SG.other_symbol_ratio(["1000株買った"])["hits"] == 0 and SG.other_symbol_ratio(["株価5000円"])["hits"] == 0)
check("他銘柄: 285A・年号・ピア除外は従来どおり(2025年・MU・8035は数えない)", SG.other_symbol_ratio(["2025年", "MUが強い", "8035と6857"])["hits"] == 0)
config.SIG_OTHER_EXCLUDE_ROUND_PRICE = False
config.SIG_OTHER_EXCLUDE_UNIT_NUMBERS = False
_old = SG.other_symbol_ratio(_px)["hits"]
config.SIG_OTHER_EXCLUDE_ROUND_PRICE = True
config.SIG_OTHER_EXCLUDE_UNIT_NUMBERS = True
check("他銘柄: 切替False=旧定義(全ての4桁をコード扱い)に戻せる", _old == len(_px))
_pd2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_dashboard.py"), encoding="utf-8").read()
check("他銘柄: 公開版(Streamlit)と静的版の用語説明に定義変更日(2026-11-26)と旧新定義を注記", all("定義変更 2026-11-26" in x and "旧定義" in x and "5000・6000" in x for x in (_pd2, _gs)))
check("他銘柄: 公開版と静的版は同じ判定関数(signals.other_symbol_ratio)由来のカードを表示(判定の二重実装なし)", "_has_other_symbol" not in _pd2 and "_has_other_symbol" not in _gs)
# ---- 2026-11-26 単位つき4桁の除外・しきい値の仮置き
_unit = ["5000円台突入", "6000円", "6523円まで", "7034円で", "4000円割れ", "1005円300株", "1000株", "3倍になる", "2月", "15日", "10時", "30分", "12億円", "3万株", "6880 円"]
check("単位: 4桁の直後が単位(円・円台・円割れ・株・万株・億円・倍・月・日・時・分)なら100の倍数でなくても他銘柄に数えない", all(SG.other_symbol_ratio([w])["hits"] == 0 for w in _unit), [w for w in _unit if SG.other_symbol_ratio([w])["hits"]])
check("単位: 単位が付かない実銘柄コードは従来どおり数える(7203・6758・8306)", SG.other_symbol_ratio(["7203", "6758が強い", "8306を売る", "6523の決算"])["hits"] == 4)
check("優先順位: 単位が先(『コード6000円』は価格として除外)・単位が無ければ100の倍数+コード文脈で残す(『コード6000の銘柄』)", SG.other_symbol_ratio(["コード6000円"])["hits"] == 0 and SG.other_symbol_ratio(["コード6000の銘柄"])["hits"] == 1)
check("優先順位: 年号→単位→100の倍数→ピアの順(2025年・5000円台・6000(文脈なし)・8035(ピア)は全て数えない)", SG.other_symbol_ratio(["2025年", "5000円台", "6000割れ", "8035が上げ"])["hits"] == 0)
check("単位: 銘柄名辞書は併用(『トヨタ6500円』は単位で数字は除外されるが銘柄名で他銘柄)", SG.other_symbol_ratio(["トヨタ6500円"])["hits"] == 1)
config.SIG_OTHER_EXCLUDE_UNIT_NUMBERS = False
_o = SG.other_symbol_ratio(["6523円"])["hits"]
config.SIG_OTHER_EXCLUDE_UNIT_NUMBERS = True
check("単位: 切替False=単位ルールなし(旧挙動)に戻せる", _o == 1)
check("しきい値: 他銘柄混入率の警戒/発火は旧0.08/0.11の半分(0.04/0.055・仮置き)", config.SIG_OTHER_WARN == 0.04 and config.SIG_OTHER_FIRE == 0.055)
_c = {c["name"]: c for c in S.compute_signals([], raw_rows=_raw, day=_day)["cards"]}["他銘柄混入率"]
check("しきい値: カードの条件欄が新しきい値で表示される(>=0.055)", "0.055" in _c["threshold"], _c["threshold"])
check("用語説明: 公開版・静的版とも変更日・旧新定義・単位除外・しきい値仮置きを注記", all(all(w in x for w in ("定義変更 2026-11-26", "旧定義", "単位", "0.04/0.055", "仮置き")) for x in (_pd2, _gs)))
print("\n結果: OK=%d NG=%d" % (len(OK), len(NG)))
sys.exit(1 if NG else 0)
