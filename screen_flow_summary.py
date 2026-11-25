# -*- coding: utf-8 -*-
"""層別フロー・板の増減・価格まわり・見立ての機械採点を組み立てる純関数モジュール(2026-10-16新設)。

出所区分(公開可否の線引き):
  - 層別フロー(layer_flow)=moomoo証券の画面を読み取った参考情報(パネルOCR)。★ローカル(dashboard.py)専用。公開側へは出さない
    (コーデ指示 2026-10-16『moomoo由来の数値は公開側への接続を保留』)。
  - 板の増減(board_flow)・価格まわり(price_flow)・見立て採点(grade_*)=板CSV・tick CSV・見立てJSONだけで作る(moomoo非依存)。
    公開側に出せる範囲は public_safe_extras() のみ。

注意: tick CSVの bid/ask 列は逆(bid=実ask)。ここでは使わない(price/vwap/volume/tickvol のみ)。
入力は全て読み取り専用。副作用なし(保存系 save_registry だけ書く)。数値は一次データの値のみ。
"""
import csv
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD_BASE = r"C:\Users\ryuta\kabu_local_record\ticks_board"
SCREEN_DIR = os.path.join(HERE, "data", "moomoo_screen")
REGISTRY_DIR = os.path.join(HERE, "data", "forecast_registry")
LAYERS = (("super", "超大口"), ("large", "大口"), ("mid", "中口"), ("small", "小口"))
EVENT_OKU = 1.0          # 超大口/大口の増減がこの値(億円)以上を『イベント』と表示
SRC_LABEL = "moomoo証券の画面を読み取った参考情報(遅延あり)"
REF_LABEL = "参考・機械採点"


def _f(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _t(s):
    return dt.datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")


def stamp_label(ts):
    """'10/16 11:30時点'"""
    return f"{ts.month}/{ts.day} {ts:%H:%M}時点"


# ---------------------------------------------------------------- A 層別フロー(ローカル専用)
def read_screen_rows(path):
    """ocr_ok=1 の行だけ。画像パス等は読み捨てて持たない。"""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if r.get("ocr_ok") != "1":
                continue
            try:
                t = _t(r["capture_time"])
            except Exception:
                continue
            d = {"t": t, "adv": _f(r.get("advantage")), "event_flag": r.get("event_flag") == "1",
                 "event_layer": r.get("event_layer") or "", "price": _f(r.get("price"))}
            for k, _ in LAYERS:
                d[k] = (_f(r.get(k + "_buy")), _f(r.get(k + "_sell")))
            out.append(d)
    return out


def layer_flow(rows, ago_min=10):
    """最新行の層別 買・売・差(億円)、買い優勢、ago_min分前比(差の変化)、直近イベント。"""
    if not rows:
        return None
    cur = rows[-1]
    out = {"asof": cur["t"].isoformat(timespec="seconds"), "asof_label": stamp_label(cur["t"]),
           "n_ok": len(rows), "advantage": round(cur["adv"], 2), "layers": {}, "ago_min": ago_min,
           "events": [], "source": SRC_LABEL}
    old = [r for r in rows if r["t"] <= cur["t"] - dt.timedelta(minutes=ago_min)]
    o = old[-1] if old else None
    for k, name in LAYERS:
        b, s = cur[k]
        d = {"name": name, "buy": b, "sell": s, "net": round(b - s, 2), "net_chg": None}
        if o is not None:
            d["net_chg"] = round((b - s) - (o[k][0] - o[k][1]), 2)
        out["layers"][k] = d
    out["advantage_chg"] = round(cur["adv"] - o["adv"], 2) if o is not None else None
    # イベント: 隣接2行間で超大口/大口の 買 or 売 が1億円以上増減した行(直近5件)
    ev = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        for k, name in LAYERS[:2]:
            for side, idx in (("買い", 0), ("売り", 1)):
                dv = b[k][idx] - a[k][idx]
                if abs(dv) >= EVENT_OKU:
                    ev.append({"t": b["t"].strftime("%H:%M:%S"), "layer": name, "side": side, "delta": round(dv, 2)})
    out["events"] = ev[-5:]
    return out


# ---------------------------------------------------------------- B 板の増減
def read_board(path):
    pts = []
    if not os.path.exists(path):
        return pts
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for d in csv.DictReader(fh):
            try:
                t = _t(d["time"])
            except Exception:
                continue
            B = sum(_f(d.get(f"buy{i}qty")) for i in range(1, 11)) + _f(d.get("under_buy_qty"))  # 2026-11-20修正: 成行は板10本に含まれる(二重加算の訂正)
            S = sum(_f(d.get(f"sell{i}qty")) for i in range(1, 11)) + _f(d.get("over_sell_qty"))
            pts.append({"t": t, "mid": _f(d.get("mid")), "B": B, "S": S})
    return pts


def _dir(x):
    return "増加" if x > 0 else "減少" if x < 0 else "横ばい"


def board_flow(pts, windows=(30, 60, 180, 300)):
    if not pts:
        return None
    cur = pts[-1]
    out = {"asof": cur["t"].isoformat(timespec="seconds"), "asof_label": stamp_label(cur["t"]),
           "buy_total_man": round(cur["B"] / 1e4, 1), "sell_total_man": round(cur["S"] / 1e4, 1),
           "ratio": round(cur["B"] / cur["S"], 2) if cur["S"] else None,
           "diff_man": round((cur["B"] - cur["S"]) / 1e4, 1), "windows": []}
    for w in windows:
        tgt = cur["t"] - dt.timedelta(seconds=w)
        c = [x for x in pts if x["t"] <= tgt]
        o = c[-1] if c else pts[0]
        db, ds = cur["B"] - o["B"], cur["S"] - o["S"]
        out["windows"].append({"sec": w, "buy_man": round(db / 1e4, 1), "buy_dir": _dir(db),
                               "sell_man": round(ds / 1e4, 1), "sell_dir": _dir(ds)})
    return out


# ---------------------------------------------------------------- C 価格まわり
def read_ticks(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            try:
                t = dt.datetime.strptime(r["time"][:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            rows.append({"t": t, "price": _f(r.get("price")), "vwap": _f(r.get("vwap")), "volume": _f(r.get("volume"))})
    return rows


def price_flow(ticks):
    """VWAP距離・出来高(tickのvolume列)・売買代金(試算=VWAP×出来高)・日中高安(09:00以降)。"""
    z = [r for r in ticks if r["price"] > 0 and r["t"].strftime("%H:%M:%S") >= "09:00:00"]
    if not z:
        return None
    last = z[-1]
    hi = max(r["price"] for r in z)
    lo = min(r["price"] for r in z)
    out = {"asof": last["t"].isoformat(timespec="seconds"), "asof_label": stamp_label(last["t"]),
           "price": last["price"], "vwap": round(last["vwap"], 1), "high": hi, "low": lo,
           "volume_man": round(last["volume"] / 1e4, 1),
           "value_oku_est": round(last["volume"] * last["vwap"] / 1e8, 1)}
    if last["vwap"] > 0:
        out["vwap_gap_yen"] = round(last["price"] - last["vwap"], 1)
        out["vwap_gap_pct"] = round((last["price"] / last["vwap"] - 1) * 100, 2)
    return out


# ---------------------------------------------------------------- D 見立ての機械採点
def mark_for(lo, hi, main_low, main_high):
    """○=高安が主レンジ内 / △=片側のみ逸脱 / ×=両側逸脱 or 主レンジと重ならない(主シナリオ外れ)。"""
    if lo is None or hi is None or main_low is None or main_high is None:
        return None
    up, dn = hi > main_high, lo < main_low
    if not up and not dn:
        return "○"
    if hi < main_low or lo > main_high or (up and dn):
        return "×"
    return "△"


def grade_forecast(fc, ticks, now=None):
    """見立て1件を採点。戻り値 dict(no,label,mark,source,provisional,hi,lo)。
    主レンジ+issuedがあれば issued〜valid_to(無ければ最新tick)のtick高安で機械採点。
    無ければ保存済みの mark_manual(過去の判定結果)を使う。"""
    res = {"no": fc.get("id"), "label": fc.get("label") or fc.get("note") or "", "mark": None,
           "source": None, "provisional": False, "hi": None, "lo": None}
    ml, mh, iss = fc.get("main_low"), fc.get("main_high"), fc.get("issued")
    if ml is not None and mh is not None and iss:
        try:
            t0 = dt.datetime.fromisoformat(str(iss)[:19])
            t1 = dt.datetime.fromisoformat(str(fc["valid_to"])[:19]) if fc.get("valid_to") else None
        except Exception:
            t0, t1 = None, None
        if t0 is not None:
            zz = [r for r in ticks if r["price"] > 0 and r["t"] >= t0 and (t1 is None or r["t"] <= t1)]
            if zz:
                res["hi"] = max(r["price"] for r in zz)
                res["lo"] = min(r["price"] for r in zz)
                res["mark"] = mark_for(res["lo"], res["hi"], ml, mh)
                res["source"] = "machine"
                last_t = ticks[-1]["t"] if ticks else None
                res["provisional"] = (t1 is None) or (last_t is None) or (last_t < t1)
                return res
    if fc.get("mark_manual"):
        res["mark"], res["source"] = fc["mark_manual"], "manual_history"
    return res


def grade_all(registry, ticks):
    gs = [grade_forecast(fc, ticks) for fc in (registry or {}).get("forecasts", [])]
    cnt = {"○": 0, "△": 0, "×": 0}
    for g in gs:
        if g["mark"] in cnt and not g["provisional"]:
            cnt[g["mark"]] += 1
    n = sum(cnt.values())
    return {"items": gs, "count": cnt, "n_graded": n, "n_pending": sum(1 for g in gs if g["mark"] is None or g["provisional"]),
            "label": REF_LABEL}


def load_registry(date, reg_dir=REGISTRY_DIR):
    p = os.path.join(reg_dir, f"forecasts_{date}.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def save_registry(reg, date, reg_dir=REGISTRY_DIR, overwrite=False):
    """既存ファイルは壊さない(overwrite=Falseで存在すれば何もしない)。"""
    os.makedirs(reg_dir, exist_ok=True)
    p = os.path.join(reg_dir, f"forecasts_{date}.json")
    if os.path.exists(p) and not overwrite:
        return False
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return True


def add_forecast(date, fc, reg_dir=REGISTRY_DIR):
    """見立てを1件追記(id,issued,main_low,main_high,note 他)。同idは追加しない。戻り値=追加したか。"""
    reg = load_registry(date, reg_dir) or {"date": date, "forecasts": []}
    if any(x.get("id") == fc.get("id") for x in reg["forecasts"]):
        return False
    reg["forecasts"].append(fc)
    save_registry(reg, date, reg_dir, overwrite=True)
    return True


# ---------------------------------------------------------------- 出力(ローカル表示・公開可の範囲)
def commentary_slot(ac, now=None):
    """Claude考察の受け口(既存 ai_commentary の text/generated_at を時点名つきに整形)。
    追加フィールド(任意): slot('30分定期'/'臨時'), scenarios[{label,prob}]。無ければ本文だけ。"""
    ac = ac or {}
    if not ac.get("text"):
        return None
    try:
        g = dt.datetime.fromisoformat(str(ac.get("generated_at"))[:19])
        label = stamp_label(g) + "の考察"
    except Exception:
        label = "考察の時点: 不明"
    out = {"text": ac["text"], "label": label, "slot": ac.get("slot"), "scenarios": []}
    for s in ac.get("scenarios") or []:
        try:
            out["scenarios"].append({"label": str(s["label"]), "prob": float(s["prob"])})
        except Exception:
            pass
    if out["scenarios"]:
        out["scenario_note"] = "見立て確率は" + REF_LABEL + "の枠での参考値です(確定した予測ではありません)。"
    return out


def public_safe_extras(board, price, grades, selloff=None):
    """公開側に出せる範囲(moomoo由来を含まない)。キー名はvalidate_no_leakの疑いキー(id/text等)を避ける。"""
    ex = {}
    if board:
        ex["board_flow"] = {k: board[k] for k in ("asof", "asof_label", "buy_total_man", "sell_total_man", "ratio", "diff_man", "windows")}
        ex["board_flow"]["source"] = "板情報(遅延あり)"
    if price:
        ex["price_flow"] = dict(price, source="約定ティック(遅延あり)・売買代金は試算(VWAP×出来高)")
    if grades:
        ex["forecast_score"] = {"label": REF_LABEL, "count": grades["count"], "n_graded": grades["n_graded"],
                                "n_pending": grades["n_pending"],
                                "items": [{"no": g["no"], "mark": g["mark"], "provisional": g["provisional"],
                                           "by_hand": g.get("source") == "manual_history"} for g in grades["items"]]}
    if selloff:
        ex["selloff"] = selloff   # セリクラ成立チェックの板・tick由来部分(数値のみ・moomoo非依存)
    return ex or None


def lines_layer(lf):
    if not lf:
        return ["層別フロー: 当日の有効な読取なし"]
    L = [f"{lf['asof_label']}｜買い優勢 {lf['advantage']:+.2f}億円"
         + (f"（{lf['ago_min']}分前比 {lf['advantage_chg']:+.2f}）" if lf.get("advantage_chg") is not None else "")]
    for k, _ in LAYERS:
        d = lf["layers"][k]
        chg = f"／{lf['ago_min']}分前比(差) {d['net_chg']:+.2f}" if d["net_chg"] is not None else ""
        L.append(f"{d['name']}: 買 {d['buy']:.2f}／売 {d['sell']:.2f}／差 {d['net']:+.2f}億円{chg}")
    if lf["events"]:
        L.append("直近のイベント(1億円以上の増減): " + "、".join(
            f"{e['t']} {e['layer']}{e['side']}{e['delta']:+.2f}億" for e in lf["events"]))
    L.append("出所: " + lf["source"])
    return L


def lines_board(bf):
    if not bf:
        return ["板の増減: データなし"]
    L = [f"{bf['asof_label']}｜買い総量 {bf['buy_total_man']:,.1f}万株／売り総量 {bf['sell_total_man']:,.1f}万株"
         f"／比 {bf['ratio']}／差 {bf['diff_man']:+,.1f}万株"]
    for w in bf["windows"]:
        s = f"{w['sec']}秒" if w["sec"] < 60 else f"{w['sec'] // 60}分"
        L.append(f"直近{s}: 買い {w['buy_man']:+,.1f}万株({w['buy_dir']})／売り {w['sell_man']:+,.1f}万株({w['sell_dir']})")
    return L


def lines_price(pf):
    if not pf:
        return ["価格まわり: データなし"]
    # ★2026-11-25(Gemini助言③): ヘッダー株価(Yahoo/PTSフィード)とは取得元・時刻が異なる値なので出所と時点を明記
    L = [f"{pf['asof_label']}｜約定ティック直近値 {pf['price']:,.0f}円(ヘッダーの株価とは取得元・時刻が異なる)／VWAP {pf['vwap']:,.1f}円"
         + (f"（距離 {pf['vwap_gap_yen']:+,.1f}円・{pf['vwap_gap_pct']:+.2f}%）" if "vwap_gap_yen" in pf else ""),
         f"出来高 {pf['volume_man']:,.1f}万株／売買代金(試算) {pf['value_oku_est']:,.1f}億円／日中高値 {pf['high']:,.0f}円・安値 {pf['low']:,.0f}円"]
    return L


def hand_note(items):
    """手採点(過去の判定結果)と機械採点が混在する時の注記。items=[{no, by_hand|source}]。"""
    hand = [i["no"] for i in items if i.get("by_hand") or i.get("source") == "manual_history"]
    mach = [i["no"] for i in items if not (i.get("by_hand") or i.get("source") == "manual_history")]
    if not hand:
        return None
    if hand and mach:
        return f"#{min(hand)}〜#{max(hand)}は手採点・#{min(mach)}以降は高安による機械採点（判定基準が異なります）"
    return f"#{min(hand)}〜#{max(hand)}は手採点"


def lines_score(g):
    if not g:
        return ["見立ての採点: 登録なし"]
    c = g["count"]
    note = hand_note(g["items"])
    return ([note] if note else []) + [f"{REF_LABEL}: ○{c['○']} △{c['△']} ×{c['×']}（採点済み{g['n_graded']}件・未採点/暫定{g['n_pending']}件）",
            "○=高安が主レンジ内／△=片側のみ逸脱／×=主シナリオ外れ。売買の推奨ではありません。"]


def _selloff(tk, bd):
    """★2026-11-25: セリクラ成立チェックの板・tick由来部分(selloff_check.compute)。失敗/データ無しはNone(fail-soft)。"""
    try:
        import selloff_check as SC
        return SC.compute(tk, bd) if tk else None
    except Exception:
        return None


def load_today(date, now=None):
    """ローカル表示用: 当日分を全部読んで dict で返す。何かが無ければその項目は None。"""
    tk = read_ticks(os.path.join(BOARD_BASE, "285A_キオクシア", "記録データ", f"ticks_285A_{date}.csv"))
    bd = read_board(os.path.join(BOARD_BASE, "_board", f"board_285A_{date}.csv"))
    sc = read_screen_rows(os.path.join(SCREEN_DIR, f"moomoo_screen_285A_{date}.csv"))
    reg = load_registry(date)
    return {"layer": layer_flow(sc), "board": board_flow(bd), "price": price_flow(tk),
            "grades": grade_all(reg, tk) if reg else None, "registry": reg, "selloff": _selloff(tk, bd)}


def load_today_public(date):
    """公開側専用: moomoo画面CSV(data/moomoo_screen)は一切開かない。板CSV・tick CSV・見立てJSONだけ読む(kabu由来＋自前の見立て記録)。"""
    tk = read_ticks(os.path.join(BOARD_BASE, "285A_キオクシア", "記録データ", f"ticks_285A_{date}.csv"))
    bd = read_board(os.path.join(BOARD_BASE, "_board", f"board_285A_{date}.csv"))
    reg = load_registry(date)
    return {"board": board_flow(bd), "price": price_flow(tk), "grades": grade_all(reg, tk) if reg else None,
            "selloff": _selloff(tk, bd)}


def public_extras_lines(ex):
    """公開レコードの rec['screen_extras'](public_safe_extras の出力)を表示行へ。無ければ空リスト。
    moomoo由来の項目は含まない(板・約定ティック・見立ての機械採点のみ)。"""
    if not ex:
        return []
    L = []
    bf, pf, fs = ex.get("board_flow"), ex.get("price_flow"), ex.get("forecast_score")
    if bf:
        L += lines_board(bf)
    if pf:
        L += lines_price(pf)
    if fs:
        c = fs.get("count") or {}
        L.append(f"見立ての採点（{fs.get('label', REF_LABEL)}）: ○{c.get('○', 0)} △{c.get('△', 0)} ×{c.get('×', 0)}"
                 f"（採点済み{fs.get('n_graded', 0)}件・未採点/暫定{fs.get('n_pending', 0)}件）"
                 "。○=高安が主レンジ内／△=片側のみ逸脱／×=主シナリオ外れ。売買の推奨ではありません。")
        note = hand_note(fs.get("items") or [])
        if note:
            L.append(note)
    return L


def public_extras_oneline(ex):
    """★2026-11-25(Gemini助言④): 数値要約用の1行サマリー。詳細(板の増減・価格まわり・見立ての採点)は
    『板・価格まわり・見立ての採点(参考)』のパネルに集約し、数値要約側では重複させない。無ければ None。"""
    if not ex:
        return None
    bf, pf, fs = ex.get("board_flow"), ex.get("price_flow"), ex.get("forecast_score")
    seg = []
    if bf:
        seg.append(f"板 買い/売り比 {bf.get('ratio')}（{bf.get('asof_label')}）")
    if pf:
        g = f"・VWAP距離 {pf['vwap_gap_yen']:+,.1f}円" if "vwap_gap_yen" in pf else ""
        seg.append(f"約定ティック直近値 {pf['price']:,.0f}円{g}")
    if fs:
        c = fs.get("count") or {}
        seg.append(f"見立て採点 ○{c.get('○', 0)}△{c.get('△', 0)}×{c.get('×', 0)}")
    if not seg:
        return None
    return "板・価格・見立て採点（参考・機械採点）: " + "／".join(seg) + "（詳細は下の『板・価格まわり・見立ての採点』）"


def _run_selftests():
    """純関数の自己テスト(ネット・実データ非依存)。失敗数を返す。"""
    import tempfile
    fails = []

    def chk(name, cond):
        print(("[OK  ] " if cond else "[FAIL] ") + "screen_flow: " + name)
        if not cond:
            fails.append(name)

    T = dt.datetime
    rows = [{"t": T(2026, 10, 16, 11, 0, 0), "adv": 50.0, "event_flag": False, "event_layer": "", "price": 1.0,
             "super": (10.0, 8.0), "large": (100.0, 90.0), "mid": (200.0, 180.0), "small": (300.0, 290.0)},
            {"t": T(2026, 10, 16, 11, 9, 50), "adv": 55.0, "event_flag": False, "event_layer": "", "price": 1.0,
             "super": (10.0, 8.0), "large": (100.0, 90.0), "mid": (200.0, 180.0), "small": (300.0, 290.0)},
            {"t": T(2026, 10, 16, 11, 10, 0), "adv": 60.0, "event_flag": True, "event_layer": "large", "price": 1.0,
             "super": (10.0, 8.0), "large": (102.0, 90.0), "mid": (203.0, 180.0), "small": (301.0, 290.0)}]
    lf = layer_flow(rows)
    chk("買い優勢と時点名", lf["advantage"] == 60.0 and lf["asof_label"] == "10/16 11:10時点")
    chk("層別の差", lf["layers"]["large"]["net"] == 12.0 and lf["layers"]["super"]["net"] == 2.0)
    chk("10分前比(差の変化)", lf["layers"]["large"]["net_chg"] == 2.0 and lf["advantage_chg"] == 10.0)
    chk("イベント: 大口買い+2億を検出・中口+3億は対象外", len(lf["events"]) == 1 and lf["events"][0]["layer"] == "大口")
    chk("空入力はNone", layer_flow([]) is None and board_flow([]) is None and price_flow([]) is None)
    pts = [{"t": T(2026, 10, 16, 11, 0, s), "mid": 100.0, "B": 1e6 + s * 1e3, "S": 1e6} for s in range(0, 60, 10)]
    bf = board_flow(pts)
    chk("板: 買い総量・比・差", bf["buy_total_man"] == 105.0 and bf["ratio"] == 1.05 and bf["diff_man"] == 5.0)
    w30 = [w for w in bf["windows"] if w["sec"] == 30][0]
    chk("板: 30秒の買い増加・売り横ばい", w30["buy_dir"] == "増加" and w30["sell_dir"] == "横ばい" and w30["buy_man"] == 3.0)
    tk = [{"t": T(2026, 10, 16, 9, 0, 1), "price": 100.0, "vwap": 100.0, "volume": 1e6},
          {"t": T(2026, 10, 16, 9, 5, 0), "price": 110.0, "vwap": 102.0, "volume": 2e6},
          {"t": T(2026, 10, 16, 9, 10, 0), "price": 104.0, "vwap": 103.0, "volume": 3e6}]
    pf = price_flow(tk)
    chk("価格: 高安・VWAP距離・出来高", pf["high"] == 110.0 and pf["low"] == 100.0 and pf["vwap_gap_yen"] == 1.0
        and pf["volume_man"] == 300.0 and pf["value_oku_est"] == round(3e6 * 103 / 1e8, 1))
    chk("採点: ○", mark_for(101, 109, 100, 110) == "○")
    chk("採点: 片側逸脱=△", mark_for(99, 109, 100, 110) == "△" and mark_for(101, 111, 100, 110) == "△")
    chk("採点: 両側逸脱=×・範囲外=×", mark_for(99, 111, 100, 110) == "×" and mark_for(120, 130, 100, 110) == "×")
    reg = {"forecasts": [{"id": 1, "mark_manual": "○"}, {"id": 2, "mark_manual": "×"},
                         {"id": 3, "issued": "2026-10-16T09:01:00", "valid_to": "2026-10-16T09:10:00", "main_low": 100, "main_high": 110},
                         {"id": 4, "issued": "2026-10-16T09:06:00", "main_low": 100, "main_high": 110},
                         {"id": 5, "issued": "2026-10-16T12:30:00", "main_low": 100, "main_high": 110}]}
    g = grade_all(reg, tk)
    m = {x["no"]: x for x in g["items"]}
    chk("機械採点: #3は主レンジ内○(確定)", m[3]["mark"] == "○" and not m[3]["provisional"] and m[3]["source"] == "machine")
    chk("#4は暫定(valid_to無し)で累計に入れない", m[4]["provisional"] and g["count"] == {"○": 2, "△": 0, "×": 1})
    chk("#5はtick無し=未採点", m[5]["mark"] is None and g["n_pending"] == 2)
    chk("累計ラベル=参考・機械採点", g["label"] == "参考・機械採点")
    with tempfile.TemporaryDirectory() as d:
        chk("保存: 新規作成", save_registry({"forecasts": []}, "2026-01-01", d) is True)
        chk("保存: 既存は壊さない", save_registry({"forecasts": [1]}, "2026-01-01", d) is False
            and load_registry("2026-01-01", d) == {"forecasts": []})
        chk("追記: 同idは重複しない", add_forecast("2026-01-01", {"id": 1, "main_low": 1, "main_high": 2}, d) is True
            and add_forecast("2026-01-01", {"id": 1}, d) is False)
    ex = public_safe_extras(bf, pf, g)
    dump = json.dumps(ex, ensure_ascii=False)
    chk("公開用extrasにmoomoo由来(層別・買い優勢・画像)を含まない", "moomoo" not in dump and "advantage" not in dump and "img" not in dump)
    chk("公開用extras: 参考・機械採点を明記", ex["forecast_score"]["label"] == "参考・機械採点")
    txt = "\n".join(public_extras_lines(ex))
    chk("公開表示に『自社算出』等の語がない", not any(w in txt for w in ("自社算出", "自己算出", "自社で記録")))
    cs = commentary_slot({"text": "本文", "generated_at": "2026-10-16T11:30:00", "scenarios": [{"label": "a", "prob": 45}]})
    chk("考察の受け口: 時点名つき・確率に参考注記", cs["label"] == "10/16 11:30時点の考察" and "参考・機械採点" in cs["scenario_note"])
    chk("考察の受け口: 本文なし=None", commentary_slot({}) is None)
    hn = hand_note([{"no": i, "source": "manual_history"} for i in range(1, 15)] + [{"no": 15, "source": "machine"}])
    chk("手採点と機械採点の注記", hn == "#1〜#14は手採点・#15以降は高安による機械採点（判定基準が異なります）")
    chk("公開表示にも手採点注記", "手採点" in chr(10).join(public_extras_lines(
        {"forecast_score": {"label": "参考・機械採点", "count": {}, "items": [{"no": 1, "by_hand": True}, {"no": 2, "by_hand": False}]}})))
    print(f"screen_flow selftest: fails={len(fails)}")
    return len(fails)


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_selftests() else 0)
