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
    return {"last_price": last_p, "last_time": d.strftime("%m/%d %H:%M"), "chg": chg,
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
