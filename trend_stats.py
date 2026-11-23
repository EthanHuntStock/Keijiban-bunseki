# -*- coding: utf-8 -*-
"""東証・PTS・米国ADR・DEXの『直近変化(5分・15分・1時間・3時間)』を機械的に出す純関数モジュール。
★2026-11-23(ユーザー指示『東証もPTSもADRもDEXも、瞬時値だけでなく数時間から数分の変化も分析してください』)。
ai_sector_monitor/adr_pts_feed.py の trend_stats と同じ仕様の独立コピー(config衝突回避のため直接importしない・
仕様を変える時は両方を揃える=CLAUDE.md『共有ロジックは独立コピー・規律で揃える』)。I/Oなし・ネット非依存。"""
import datetime as dt
import json
import os

WINDOWS_MIN = (5, 15, 60, 180)


def trend_stats(points, windows=WINDOWS_MIN):
    """points=[(ts_epoch_sec, price)...] -> {last_price,last_time(JST 'MM/DD HH:MM'),chg:{'5m':%,..},hi,lo,n} / 2点未満はNone。
    各窓w分: 最終点のw分前以前で最も新しい点が基準。ただし基準点が (2w+5)分より古い(=データの空白・取引の途切れ)なら None。"""
    pts = sorted((float(t), float(p)) for t, p in (points or []) if t is not None and p is not None)
    if len(pts) < 2:
        return None
    last_t, last_p = pts[-1]
    if not last_p:
        return None
    chg = {}
    for w in windows:
        target = last_t - w * 60
        ref = None
        for t, p in reversed(pts[:-1]):
            if t <= target:
                ref = (t, p)
                break
        if ref and (last_t - ref[0]) <= (2 * w + 5) * 60 and ref[1]:
            chg[f"{w}m"] = round((last_p - ref[1]) / ref[1] * 100.0, 2)
        else:
            chg[f"{w}m"] = None
    recent = [p for t, p in pts if t >= last_t - 180 * 60]
    d = dt.datetime.fromtimestamp(last_t, dt.timezone.utc) + dt.timedelta(hours=9)
    return {"last_price": last_p, "last_time": d.strftime("%m/%d %H:%M"), "last_ts": last_t, "chg": chg,
            "hi": max(recent), "lo": min(recent), "n": len(recent)}


def rows_trends(rows):
    """ADR/PTSフィード行(ts,tse,pts,adr_yen)から東証・PTS・ADRそれぞれの直近変化を返す。"""
    out = {}
    for key, col in (("tse", "tse"), ("pts", "pts"), ("adr", "adr_yen")):
        out[key] = trend_stats([(r.get("ts"), r.get(col)) for r in (rows or []) if r.get(col) is not None])
    return out


def tick_points(rows, today):
    """kabuティックCSV行(time='YYYY-MM-DD HH:MM:SS.mmm' JST・price)から本日分の [(epoch_sec, price)]。不正行は無視。"""
    out = []
    for r in rows or []:
        t = (r.get("time") or "").strip()
        if len(t) < 19 or not t.startswith(today):
            continue
        try:
            p = float(r.get("price"))
            d = dt.datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        out.append(((d - dt.timedelta(hours=9)).replace(tzinfo=dt.timezone.utc).timestamp(), p))
    return out


def dex_points(path, max_lines=600):
    """DEX 5分系列JSONL(ts_ms, jpy)の末尾から [(epoch_sec, 円)]。読めなければ []。"""
    try:
        if not os.path.exists(path):
            return []
        with open(path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 200 * max_lines))
            lines = f.read().decode("utf-8", "replace").splitlines()
        out = []
        for l in lines[-max_lines:]:
            l = l.strip()
            if l.startswith("{"):
                try:
                    j = json.loads(l)
                    out.append((int(j["ts_ms"]) / 1000.0, float(j["jpy"])))
                except (ValueError, KeyError, TypeError):
                    continue
        return out
    except Exception:
        return []


def text(tr):
    """trend_stats -> '5分 -0.2%・15分 +0.1%・1時間 …・3時間 …／直近3時間の高値 x・安値 y（最終値 z円・MM/DD HH:MM）' / 無ければ None。"""
    if not isinstance(tr, dict) or not isinstance(tr.get("chg"), dict):
        return None
    lab = (("5m", "5分"), ("15m", "15分"), ("60m", "1時間"), ("180m", "3時間"))
    seg = [f"{n} {tr['chg'][k]:+.2f}%" for k, n in lab if tr["chg"].get(k) is not None]
    if not seg:
        return None
    s = "・".join(seg)
    if tr.get("hi") is not None and tr.get("lo") is not None:
        s += f"／直近3時間の高値 {tr['hi']:,.0f}・安値 {tr['lo']:,.0f}"
    return f"{s}（最終値 {tr.get('last_price'):,.0f}円・{tr.get('last_time')}）"


# ============================================================================
# 直近変化の『読み』(2026-11-23追加・ユーザー指示「変化が何を意味するのかの考察が必要」)。
# 数字を並べるだけでなく、各市場の動きの性質(加速/減速/反転/横ばい・直近3時間のどこにいるか)と、
# 市場間の同時間帯の揃い/乖離、時間外の水準が東証最終値に対して何を示すかを、固定ルールで言葉にする
# (純関数・断定しない・予告しない)。ai_sector_monitor/adr_pts_feed.py と おにや trend_stats.py は同一仕様の独立コピー。
# ============================================================================
_READ_FLAT_S, _READ_FLAT_L = 0.10, 0.30      # 短い窓/長い窓の『ほぼ横ばい』閾値(%)
_READ_NEAR = 0.15                            # 3時間レンジの端から15%以内=高値圏/安値圏
_READ_SAME_WINDOW_SEC = 3 * 3600             # 同時間帯とみなす最終値時刻の差


def _pp(x):
    """+0.00 / -0.00 を '0.00' に正規化した符号つき小数(読み専用)"""
    s = f"{x:+.2f}"
    return "0.00" if s in ("+0.00", "-0.00") else s


def _dir_word(x):
    return "上昇" if x > 0 else "下落"


def _read_one(label, tr):
    """1市場の動きの性質を1文で。窓が足りなければその旨。戻り値 (文, 短い窓の符号 -1/0/1)"""
    c = (tr or {}).get("chg") or {}
    s_k = next((k for k in ("5m", "15m") if c.get(k) is not None), None)
    l_k = next((k for k in ("60m", "180m") if c.get(k) is not None), None)
    if s_k is None or l_k is None:
        got = [f"{k.replace('m', '分')} {_pp(c[k])}%" for k in ("5m", "15m", "60m", "180m") if c.get(k) is not None]
        return (f"{label}: " + ("・".join(got) if got else "変化率なし") + "(取引が疎で、加速/減速の判定はしない)", 0)
    s, l = c[s_k], c[l_k]
    ms, ml = int(s_k[:-1]), int(l_k[:-1])
    lab_s, lab_l = f"{ms}分", ("1時間" if ml == 60 else "3時間")
    if abs(s) < _READ_FLAT_S and abs(l) < _READ_FLAT_L:
        desc, sign = f"ほぼ横ばい({lab_s} {_pp(s)}%・{lab_l} {_pp(l)}%)", 0
    elif s * l < 0 and abs(s) >= _READ_FLAT_S:
        desc = (f"{lab_l}では{_dir_word(l)}({_pp(l)}%)だが直近{lab_s}は逆向き({_pp(s)}%)＝流れが反転しかけている"
                f"(短い窓だけの動きで、続くかは未確認)")
        sign = 1 if s > 0 else -1
    else:
        sign = 1 if (s > 0 or (s == 0 and l > 0)) else -1
        ps, pl = abs(s) / ms, abs(l) / ml
        w = _dir_word(sign)
        if pl > 0 and ps >= 1.5 * pl:
            desc = f"{w}が加速({lab_s} {_pp(s)}%は{lab_l} {_pp(l)}%のペースより速い)"
        elif pl > 0 and ps <= 0.5 * pl:
            desc = f"{w}基調だが勢いは鈍化({lab_s} {_pp(s)}%は{lab_l} {_pp(l)}%のペースより遅い)"
        else:
            desc = f"{w}が同程度のペースで継続({lab_s} {_pp(s)}%・{lab_l} {_pp(l)}%)"
        if abs(s) < _READ_FLAT_S:
            sign = 0
    hi, lo, last = tr.get("hi"), tr.get("lo"), tr.get("last_price")
    if hi is not None and lo is not None and last is not None and hi > lo:
        pos = (last - lo) / (hi - lo)
        if pos <= _READ_NEAR:
            desc += "／直近3時間の安値圏"
        elif pos >= 1 - _READ_NEAR:
            desc += "／直近3時間の高値圏"
    return (f"{label}: {desc}", sign)


def reading(series, levels=None):
    """series={ラベル: trend_stats結果}・levels={ラベル: 東証最終値比%}(PTS/ADR/DEXなど)から、
    『変化の読み』の文リストを返す純関数。断定的な予測はしない(性質の記述と、比較の材料の提示に留める)。"""
    out, signs, tss = [], {}, {}
    for label, tr in (series or {}).items():
        if not isinstance(tr, dict) or not isinstance(tr.get("chg"), dict):
            continue
        txt, sg = _read_one(label, tr)
        out.append(txt)
        signs[label] = sg
        tss[label] = tr.get("last_ts")
    live = [k for k in signs if tss.get(k) is not None]
    if len(live) >= 2:
        newest = max(tss[k] for k in live)
        near = [k for k in live if newest - tss[k] <= _READ_SAME_WINDOW_SEC]
        if len(near) >= 2:
            nz = {k: signs[k] for k in near if signs[k] != 0}
            if len(nz) >= 2:
                ups = [k for k, v in nz.items() if v > 0]
                dns = [k for k, v in nz.items() if v < 0]
                if not ups or not dns:
                    out.append(f"同時間帯の比較({'・'.join(near)}): 向きは{'上昇' if ups else '下落'}で揃っている")
                else:
                    out.append(f"同時間帯の比較({'・'.join(near)}): 向きが分かれている"
                               f"(上昇={'・'.join(ups)}／下落={'・'.join(dns)})")
        stale = [k for k in live if k not in near]
        if stale:
            out.append(f"{'・'.join(stale)}は最終値の時刻が他と大きく離れているため、同時間帯の比較には含めない")
    lv = {k: v for k, v in (levels or {}).items() if v is not None}
    if lv:
        parts = "・".join(f"{k} {v:+.2f}%" for k, v in lv.items())
        neg, pos_ = [v for v in lv.values() if v < 0], [v for v in lv.values() if v > 0]
        if neg and not pos_:
            mean = "いずれも東証最終値より下＝次の寄り付きが終値より低く始まる場合の水準感を示す材料(予告ではない)"
        elif pos_ and not neg:
            mean = "いずれも東証最終値より上＝次の寄り付きが終値より高く始まる場合の水準感を示す材料(予告ではない)"
        else:
            mean = "東証最終値に対して上下が分かれており、時間外の水準感は市場により異なる"
        spread = (max(lv.values()) - min(lv.values())) if len(lv) >= 2 else 0
        extra = f"／市場間の水準差 {spread:.1f}pt(取引量・時点が異なるため単純比較はしない)" if spread >= 3 else ""
        out.append(f"東証最終値比の水準: {parts}→{mean}{extra}")
    return out
