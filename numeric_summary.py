# -*- coding: utf-8 -*-
"""公開ダッシュボードの『数値要約』(機械的テンプレート・LM不使用)を組み立てる純関数モジュール。

★2026-10-13 ユーザー決定『考察・要約もLMをやめる』『2段構成(数値要約＋時点名つきClaude考察)＋急変時の臨時考察』に基づく。
- 入力は公開レコード(latest.json の dict)だけ。個別投稿は受け取らない。数値は元データの値をそのまま表示する
  (文章の事実誤りが構造的に出ない)。値が無い項目は『算出不可/当日分析なし』と明記し、数値から結論を書かない。
- 変化点は record['previous'](前回集計との比較用)から閾値を超えた項目だけを列挙する(固定ルール)。
- 出力: dict(heading, asof, lines[list[str]], changes[list[str]], note)。副作用なし・ネット非依存。
"""
import datetime as dt

HEADING = "数値要約"
NOTE = "強気・弱気は機械判定（辞書＋軽量モデル）の値です。日単位の向きはLM判定と近く（相関0.8台）、時間単位は粗い点に注意してください。"
# 変化点の閾値(固定)
TH_RATIO_PT = 1.0       # 強気/弱気比率の変化(百分率ポイント)
TH_PRICE_PT = 0.5       # 前日終値比の変化(百分率ポイント)
TH_POSTS = 100          # 投稿数の増加(件)
STATE_ORDER = ("発火", "警戒")


def _pct(x):
    return "%.1f%%" % (x * 100.0) if isinstance(x, (int, float)) else "—"


def _fmt_time(s):
    try:
        return dt.datetime.fromisoformat(str(s)[:19]).strftime("%H:%M")
    except Exception:
        return "—"


def build_numeric_summary(rec, now_ts=None):
    rec = rec or {}
    price = rec.get("price") or {}
    board = rec.get("board") or {}
    prev = rec.get("previous") or {}
    cards = rec.get("signal_cards") or []
    lines = []

    last = price.get("last")
    chg = price.get("change_pct")
    if isinstance(last, (int, float)):
        s = "株価: {:,.0f}円".format(last)
        if isinstance(chg, (int, float)):
            s += "（前営業日終値比 %+.2f%%）" % chg
        lines.append(s)
    else:
        lines.append("株価: 取得できず")

    bull, bear, neu = board.get("bull_ratio"), board.get("bear_ratio"), board.get("neutral_ratio")
    posts, pph = board.get("post_count_today"), board.get("posts_per_hour")
    if all(isinstance(v, (int, float)) for v in (bull, bear)):
        s = "掲示板: 強気 %s／弱気 %s／中立 %s" % (_pct(bull), _pct(bear), _pct(neu))
        if isinstance(posts, (int, float)):
            s += "・投稿 {:,}件".format(int(posts))
        if isinstance(pph, (int, float)):
            s += "（約%d件/時）" % round(pph)
        lines.append(s)
    else:
        s = "掲示板: 当日の感情分析なし（強気・弱気比率は算出不可）"
        if isinstance(posts, (int, float)):
            s += "・投稿 {:,}件".format(int(posts))
        lines.append(s)

    fired = [c for c in cards if c.get("state") == "発火"]
    warn = [c for c in cards if c.get("state") == "警戒"]
    ok = [c for c in cards if c.get("state") == "OK"]

    def _nm(c):
        v = c.get("value")
        return "%s(%s)" % (c.get("name"), ("%.2f" % v).rstrip("0").rstrip(".") if isinstance(v, (int, float)) else "—")

    if cards:
        parts = []
        if fired:
            parts.append("発火＝" + "・".join(_nm(c) for c in fired))
        if warn:
            parts.append("警戒＝" + "・".join(_nm(c) for c in warn))
        if ok:
            parts.append("平常＝" + "・".join(_nm(c) for c in ok[:4]) + ("ほか" if len(ok) > 4 else ""))
        lines.append("シグナル: " + "／".join(parts))

    # ★2026-10-16追加: 板の増減・価格まわり(tick)・見立ての機械採点(rec['screen_extras']がある時だけ。moomoo由来は含まない)
    try:
        import screen_flow_summary as _sf
        _x1 = _sf.public_extras_oneline(rec.get("screen_extras"))   # 2026-11-25: 詳細パネルと重複しないよう1行に圧縮
        if _x1:
            lines.append(_x1)
    except Exception:
        pass

    # ★2026-11-23追加(ユーザー指示「ADRの分析もAI分析に入れる」): 夜間PTS・米国ADR円換算(東証最終値比)。取引が無い時は行ごと省略。
    try:
        _eh = rec.get("extended_hours") or {}
        _seg = []
        for _k, _lab in (("pts", "PTS"), ("adr", "米国ADR"), ("dex", "DEX(24時間取引)")):
            _v = _eh.get(_k)
            if isinstance(_v, dict) and isinstance(_v.get("change_pct"), (int, float)):
                _seg.append("%s %+.2f%%（%s時点）" % (_lab, _v["change_pct"], _v.get("time") or "—"))
        if _seg:
            lines.append("夜間PTS・米国ADR・DEX(東証最終値比): " + "／".join(_seg))
        # ★2026-11-23(ユーザー指示『数時間から数分の変化も分析』): 直近変化(5分/15分/1時間/3時間)
        import trend_stats as _tsm
        _tp = []
        # ★2026-11-25(ユーザー承認): 最終約定から120分超(立会時間外)の市場は直近変化・変化の読みから除外し『最終 M/D HH:MM・立会時間外』の1行にする
        _fresh, _stale = _tsm.split_stale({_lab: (_eh.get("trend") or {}).get(_k) for _lab, _k in (("東証", "tse"), ("PTS", "pts"), ("米国ADR", "adr"), ("DEX", "dex"))}, now_ts)
        for _lab, _k in (("東証", "tse"), ("PTS", "pts"), ("米国ADR", "adr"), ("DEX", "dex")):
            _tr = _fresh.get(_lab)
            _tt = _tsm.text(_tr)
            if _tt:
                _tp.append("%s %s（%s時点）" % (_lab, _tt.split("／")[0], _tr.get("last_time")))
        if _tp:
            lines.append("直近変化: " + "／".join(_tp))
        if _stale:
            lines.append("立会時間外の市場(直近変化は出さない): " + "／".join(_stale))
        # ★2026-11-23(ユーザー指示『変化が何を意味するのかの考察』): 固定ルールの『読み』(加速/減速/反転・市場間の揃い/乖離・水準の意味)
        _ser = {l: _fresh.get(l) for l in ("東証", "PTS", "米国ADR", "DEX")}
        _lv = {l: (_eh.get(k) or {}).get("change_pct") for l, k in (("PTS", "pts"), ("米国ADR", "adr"), ("DEX", "dex")) if isinstance(_eh.get(k), dict)}
        _prices = {l: (_eh.get(k) or {}).get("price") for l, k in (("PTS", "pts"), ("DEX", "dex")) if isinstance(_eh.get(k), dict)}
        _prev_low = None
        try:  # ③ 前営業日(直近の確定した立会)のザラ場安値=tick CSVの最安値(B の前営業日CSV読込を流用)
            import screen_flow_summary as _sfl
            _jst = (dt.datetime.now(dt.timezone.utc) if now_ts is None else dt.datetime.fromtimestamp(now_ts, dt.timezone.utc)) + dt.timedelta(hours=9)
            _pl = _sfl.latest_session_low(_jst.strftime("%Y-%m-%d"))
            _prev_low = _pl[1] if _pl else None
        except Exception:
            _prev_low = None
        for _rl in _tsm.reading({k: v for k, v in _ser.items() if v}, _lv, prices=_prices, prev_low=_prev_low):
            lines.append("変化の読み: " + _rl)
    except Exception:
        pass

    # 変化点(前回集計比)
    changes = []
    try:
        if isinstance(bull, (int, float)) and isinstance(prev.get("bull_ratio"), (int, float)):
            d = (bull - prev["bull_ratio"]) * 100
            if abs(d) >= TH_RATIO_PT:
                changes.append("強気比率 %+.1fpt" % d)
        if isinstance(bear, (int, float)) and isinstance(prev.get("bear_ratio"), (int, float)):
            d = (bear - prev["bear_ratio"]) * 100
            if abs(d) >= TH_RATIO_PT:
                changes.append("弱気比率 %+.1fpt" % d)
        if isinstance(chg, (int, float)) and isinstance(prev.get("change_pct"), (int, float)):
            d = chg - prev["change_pct"]
            if abs(d) >= TH_PRICE_PT:
                changes.append("前営業日終値比 %+.2fpt" % d)
        if isinstance(posts, (int, float)) and isinstance(prev.get("post_count_today"), (int, float)):
            d = posts - prev["post_count_today"]
            if d >= TH_POSTS:
                changes.append("投稿 %+d件" % d)
    except Exception:
        pass
    lines.append("変化点: " + ("・".join(changes) if changes else "前回の数値要約から大きな変化なし"))
    return {"heading": HEADING, "asof": _fmt_time(rec.get("generated_at")), "lines": lines, "changes": changes, "note": NOTE}
