# -*- coding: utf-8 -*-
"""セリクラ成立チェック(3点ランプ)。2026-11-25新設(ユーザー承認・Gemini助言⑥)。純関数・ネット/ファイル非依存。

3点:
  (a) 心理発火   = シグナルカード『阿鼻叫喚(セリクラ)』が発火(掲示板の心理指標)。表示側がカードから渡す。
  (b) 板買い転換 = 直近3分で板の買い総量が増え、かつ売り総量が減った(板CSV・遅延あり)。
  (c) 出来高と安値の切り上げ = 直近の窓(既定45分)の安値(L)の付近で、1分出来高が直前10分平均の1.5倍以上に膨らみ、
        その後 安値が切り上がり(直近3分の安値 > その前3分の安値、かつ L より上)、現値がLから0.3%以上戻している。
別表示(3点に数えない): VWAP回復(現値>=VWAP)。

注意(表記方針): 心理指標の発火だけで『底』とは見せない。『成立チェック(n/3)』と書き、断定しない。
tickの出来高はCSVの volume 列(当日累計)の差分で1分出来高を作る(売買代金は使わない)。
"""
import datetime as dt

WINDOW_MIN = 45          # (c)の安値(L)を探す窓(分)
SPIKE_NEAR_PCT = 0.003   # Lから+0.3%以内の価格帯を『安値付近』とみなす
SPIKE_RATIO = 1.5        # 安値付近の1分出来高 / 直前10分平均
PRE_MIN = 10             # 『直前平均』の分数(最低5分分が要る)
REBOUND_PCT = 0.003      # 現値がLから0.3%以上戻している
BOARD_SEC = 180          # (b)の窓(秒)


def minute_bars(ticks, as_of=None):
    """ticks=[{t,price,vwap,volume(当日累計)}] -> 分足 [{m(datetime 分切捨て),lo,hi,close,vol(万株でなく株)}](09:00以降・as_of以前)。"""
    out, prev_cum, cur = [], None, None
    for r in ticks:
        t = r["t"]
        if as_of is not None and t > as_of:
            break
        if r.get("price", 0) <= 0 or t.strftime("%H:%M:%S") < "09:00:00":
            continue
        m = t.replace(second=0, microsecond=0)
        if cur is None or cur["m"] != m:
            cur = {"m": m, "lo": r["price"], "hi": r["price"], "close": r["price"], "vol": 0.0}
            out.append(cur)
        cur["lo"] = min(cur["lo"], r["price"])
        cur["hi"] = max(cur["hi"], r["price"])
        cur["close"] = r["price"]
        v = r.get("volume") or 0.0
        if prev_cum is not None and v >= prev_cum:
            cur["vol"] += v - prev_cum
        prev_cum = v
    return out


def board_turn(pts, as_of=None, sec=BOARD_SEC):
    """(b) 板買い転換。pts=[{t,B,S}]。直近sec秒で 買い総量↑かつ売り総量↓ なら ok=True。データ不足はok=None。"""
    z = [p for p in pts if as_of is None or p["t"] <= as_of]
    if len(z) < 2:
        return {"ok": None, "detail": "板データ不足"}
    cur = z[-1]
    c = [p for p in z if p["t"] <= cur["t"] - dt.timedelta(seconds=sec)]
    if not c:
        return {"ok": None, "detail": "直近%d秒の比較データなし" % sec}
    o = c[-1]
    db, ds = cur["B"] - o["B"], cur["S"] - o["S"]
    return {"ok": bool(db > 0 and ds < 0), "buy_man": round(db / 1e4, 1), "sell_man": round(ds / 1e4, 1),
            "sec": sec, "detail": "直近%d分 買い%+.1f万株・売り%+.1f万株" % (sec // 60, db / 1e4, ds / 1e4)}


def low_rise_volume(bars, window_min=WINDOW_MIN):
    """(c) 出来高と安値の切り上げ。bars=minute_bars()の結果(古→新)。"""
    if len(bars) < 12:
        return {"ok": None, "detail": "分足データ不足"}
    last = bars[-1]
    t0 = last["m"] - dt.timedelta(minutes=window_min)
    win = [b for b in bars if b["m"] >= t0]
    L = min(b["lo"] for b in win)
    # 安値付近(L+0.3%以内)の分のうち出来高最大の分
    near = [b for b in win if b["lo"] <= L * (1 + SPIKE_NEAR_PCT)]
    sp = max(near, key=lambda b: b["vol"])
    pre = [b["vol"] for b in bars if sp["m"] - dt.timedelta(minutes=PRE_MIN) <= b["m"] < sp["m"]]
    ratio = (sp["vol"] / (sum(pre) / len(pre))) if len(pre) >= 5 and sum(pre) > 0 else None
    last3 = [b for b in bars if b["m"] > last["m"] - dt.timedelta(minutes=3)]
    prev3 = [b for b in bars if last["m"] - dt.timedelta(minutes=6) < b["m"] <= last["m"] - dt.timedelta(minutes=3)]
    lo3 = min(b["lo"] for b in last3) if last3 else None
    lop = min(b["lo"] for b in prev3) if prev3 else None
    rebound = (last["close"] / L - 1.0) if L else None
    c_vol = ratio is not None and ratio >= SPIKE_RATIO
    c_rise = lo3 is not None and lop is not None and lo3 > lop and lo3 > L
    c_reb = rebound is not None and rebound >= REBOUND_PCT
    ok = bool(c_vol and c_rise and c_reb)
    if ratio is None:
        ok = None
    parts = ["安値%s円(%s)" % (format(L, ",.0f"), sp["m"].strftime("%H:%M")),
             "付近の出来高は直前平均の%s倍" % ("%.1f" % ratio if ratio is not None else "—"),
             "直近3分の安値%s前3分比%s" % (format(lo3, ",.0f") if lo3 else "—",
                                          "切り上がり" if c_rise else "切り上がらず"),
             "安値から%+.2f%%" % (rebound * 100 if rebound is not None else 0.0)]
    return {"ok": ok, "low": L, "low_time": sp["m"].strftime("%H:%M"), "vol_ratio": round(ratio, 2) if ratio is not None else None,
            "low_last3": lo3, "low_prev3": lop, "rebound_pct": round(rebound * 100, 2) if rebound is not None else None,
            "cond_volume": bool(c_vol), "cond_rise": bool(c_rise), "cond_rebound": bool(c_reb),
            "detail": "／".join(parts)}


def vwap_state(ticks, as_of=None):
    """別表示: VWAP回復(現値>=VWAP)。3点には数えない。"""
    z = [r for r in ticks if r.get("price", 0) > 0 and r["t"].strftime("%H:%M:%S") >= "09:00:00"
         and (as_of is None or r["t"] <= as_of)]
    if not z or not z[-1].get("vwap"):
        return {"recovered": None, "detail": "VWAPなし"}
    p, v = z[-1]["price"], z[-1]["vwap"]
    return {"recovered": bool(p >= v), "price": p, "vwap": round(v, 1), "gap_yen": round(p - v, 1),
            "detail": "現値%s円・VWAP%s円(距離%+.1f円)" % (format(p, ",.0f"), format(v, ",.1f"), p - v)}


CLOSE_HHMMSS = "15:30:00"


def compute(ticks, board_pts, as_of=None, prev_date=None):
    """板・tick由来の2点(b,c)とVWAPをまとめる(公開レコード用・moomoo非依存)。(a)は表示側でカードから足す。
    ★2026-11-25(モニタ連絡): 最新tickが大引け(15:30)以降なら『当日の確定結果』モード(final=True)。
    判定は大引け(15:30:59)までのデータで行い、夜間に動かない板を進行形として評価しない。"""
    if prev_date and ticks:
        # ★2026-11-25(ユーザー承認): 日付が変わった後〜翌寄り前は前営業日のtickで確定表示(判定は15:30:59まで・既存どおり)
        last_t = ticks[-1]["t"]
        cap = last_t.replace(hour=15, minute=30, second=59, microsecond=0)
        return dict(_compute(ticks, board_pts, min(last_t, cap)), final=True, prev_date=str(prev_date),
                    asof_label="前営業日(%s) 大引け(15:30)時点・確定" % prev_date)
    if as_of is None and ticks:
        last_t = ticks[-1]["t"]
        if last_t.strftime("%H:%M:%S") >= CLOSE_HHMMSS:
            as_of = last_t.replace(hour=15, minute=30, second=59, microsecond=0)
            return dict(_compute(ticks, board_pts, as_of), final=True,
                        asof_label="%d/%d 大引け(15:30)時点・確定" % (last_t.month, last_t.day))
    return _compute(ticks, board_pts, as_of)


def _compute(ticks, board_pts, as_of=None):
    bars = minute_bars(ticks, as_of)
    asof = None
    for r in reversed(ticks):
        if as_of is None or r["t"] <= as_of:
            asof = r["t"]
            break
    return {"asof": asof.isoformat(timespec="seconds") if asof else None,
            "asof_label": ("%d/%d %02d:%02d時点" % (asof.month, asof.day, asof.hour, asof.minute)) if asof else None,
            "board_turn": board_turn(board_pts, as_of), "low_rise": low_rise_volume(bars),
            "vwap": vwap_state(ticks, as_of)}


def psych_fired(cards):
    """(a) シグナルカードの『阿鼻叫喚(セリクラ)』が発火か。カード無し=None。"""
    for c in cards or []:
        if c.get("name") == "阿鼻叫喚(セリクラ)":
            return c.get("state") == "発火"
    return None


def lamps(sel, cards):
    """表示用: {'n':点灯数,'total':3,'items':[{label,state('on'/'off'/'na'),detail}],'vwap':{...},'title':'成立チェック(n/3)','caution':...}。
    sel=compute()の出力(None可)。心理指標の発火だけでは底と見せない旨を必ず付ける。"""
    a = psych_fired(cards)
    items = [{"label": "心理発火(阿鼻叫喚指数)", "state": "na" if a is None else ("on" if a else "off"),
              "detail": "掲示板の心理指標。これだけでは底の根拠にならない"}]
    for key, lab in (("board_turn", "板買い転換(直近3分)"), ("low_rise", "出来高＋安値切り上げ")):
        d = (sel or {}).get(key) or {}
        ok = d.get("ok")
        items.append({"label": lab, "state": "na" if ok is None else ("on" if ok else "off"), "detail": d.get("detail", "データなし")})
    prev_date = (sel or {}).get("prev_date")
    if prev_date:  # 前営業日表示: (a)は当日のシグナルカードなので前営業日の判定には使えない=判定対象外
        items[0] = dict(items[0], state="na", detail="前営業日の心理指標は保持していないため判定対象外(最大2点)")
    n = sum(1 for i in items if i["state"] == "on")
    # 旧レコード(finalキー無し)も、時点(asof)が大引け(15:30)以降なら確定扱い
    final = bool((sel or {}).get("final")) or str((sel or {}).get("asof") or "")[11:19] >= "15:30:00"
    only_psych = n == 1 and items[0]["state"] == "on"
    caution = ("心理指標が点灯しているだけで、板・出来高の裏付けはまだありません(『底』とは言えません)。" if only_psych
               else "3点が揃っても底の確定ではありません。研究用・未検証の参考表示です。")
    if final:  # 夜間・大引け後は過去形の確定結果(進行形にしない)
        if only_psych:
            caution = "心理指標は点灯しましたが、板・出来高の裏付けはありませんでした(『底』とは言えません)。"
        caution = "本日の大引け時点の確定結果です(進行中の判定ではありません)。" + caution
    if prev_date:
        caution = caution.replace("本日の大引け時点", "前営業日(%s)の大引け時点" % prev_date)
        title = "前営業日(%s)のセリクラ成立チェック・大引け時点で確定(%d/3)" % (prev_date, n)
    else:
        title = ("本日のセリクラ成立チェック・大引け時点で確定(%d/3)" if final else "セリクラ成立チェック(%d/3)") % n
    return {"n": n, "total": 3, "final": final, "prev_date": prev_date,
            "title": title, "items": items,
            "vwap": (sel or {}).get("vwap"), "asof_label": (sel or {}).get("asof_label"), "caution": caution}


def lamps_text(lp):
    """ランプを1行テキストへ(Streamlit/HTML共通の素材)。"""
    mark = {"on": "🟢", "off": "⚪", "na": "－"}
    s = lp["title"] + "：" + "　".join("%s%s" % (mark[i["state"]], i["label"]) for i in lp["items"])
    v = lp.get("vwap") or {}
    if v.get("recovered") is not None:
        s += "｜別表示 VWAP回復: " + ("はい" if v["recovered"] else "いいえ") + "(" + v["detail"] + ")"
    return s
