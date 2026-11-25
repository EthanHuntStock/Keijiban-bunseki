# -*- coding: utf-8 -*-
"""公開ページのUI改善5点(2026-11-26 ユーザー承認『モニタ側のUI案も全部入れてください』)の共通部品。静的HTML版・Streamlit版で共用。
参考: ai_sector_monitor/kpi_cards.py の summary3_html / recent_table_html / UI_CSS(読み取り専用・同じ見た目)。
全て純関数・LM不使用・公開レコード(moomoo非依存)の数値だけから決定論的に作る。予想や売買推奨にはしない。"""
import datetime as _dt
import html as _h

CSS = (".sum3,.rc,.rc-wrap,.panel,.metric-card,.caption,.stMarkdown table{font-variant-numeric:tabular-nums}"
       "body,.stApp{font-variant-numeric:tabular-nums}"
       ".fold{margin:6px 0}.fold>summary{cursor:pointer;font-size:.85em;color:var(--muted,#94a3b8)}"
       ".sum3{background:var(--panel,transparent);border:1px solid var(--border,rgba(128,128,128,.5));border-radius:10px;padding:8px 12px;margin:8px 0 14px}"
       ".sum3 .l{display:flex;flex-direction:column;gap:2px;line-height:1.6;padding:4px 0;align-items:flex-start}"
       ".sum3 .k{display:inline-block;font-size:.72em;font-weight:700;padding:1px 8px;border-radius:999px;border:1px solid #3b82f6;"
       "color:var(--text,inherit);background:rgba(59,130,246,.14)}"
       ".sum3 .n{font-size:.75em;color:var(--muted,#94a3b8);margin-top:2px}"
       ".rc-wrap{overflow-x:auto;margin:8px 0 14px;max-width:100%}.rc{width:100%;border-collapse:collapse;font-size:.85em}"
       ".rc th,.rc td{padding:4px 8px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border,rgba(128,128,128,.5))}"
       ".rc th:first-child,.rc td:first-child{text-align:left}.rc .pos{color:#16a34a}.rc .neg{color:#dc2626}"
       ".rc tr.stale td{color:#94a3b8;opacity:.75}.rc tr.stale td.pos,.rc tr.stale td.neg{color:#94a3b8}"
       ".rc caption{caption-side:top;text-align:left;font-size:.8em;color:var(--muted,#94a3b8);padding-bottom:4px}"
       "@media(max-width:480px){.rc{font-size:.78em}.rc th,.rc td{padding:3px 5px}img{max-width:100%;height:auto}}")

# 免責: 各所に散っていた短い文言を消さずにここへ集約して1か所(ページ末尾)に表示する(文言は既存のまま)
DISCLAIMER_ITEMS = (
    "板・約定ティックは遅延あり。売買の推奨ではありません。",
    "見立ての採点は参考・機械採点で、売買の推奨ではありません。",
    "※研究・エンタメ用途・未検証。売買シグナルではありません。",
    "※ニュース見出しの要約であり、投資助言ではありません。",
)


def disclaimer_extra_html():
    return "<p class='caption'>" + "<br>".join(_h.escape(x) for x in DISCLAIMER_ITEMS) + "</p>"


def fill_prev_session_change(rec, date):
    """price.change_pct が無い(日付またぎ)時、直近確定営業日の騰落率を price に補い『11/25終値・前営業日比』の根拠ラベルを付ける。fail-soft。"""
    try:
        pr = (rec or {}).get("price")
        if not isinstance(pr, dict) or pr.get("change_pct") is not None:
            return rec
        import screen_flow_summary as _sf
        sc = _sf.session_change(date)
        if sc:
            pr["change_pct"] = sc["pct"]
            pr["change_basis"] = sc["label"]
            if pr.get("last") is None:
                pr["last"] = sc["close"]
    except Exception:
        pass
    return rec


def _f(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _pct(v):
    v = _f(v)
    return "—" if v is None else "%+.2f%%" % v


def _cls(v):
    v = _f(v)
    return "" if v is None or v == 0 else ("pos" if v > 0 else "neg")


def recent_rows(rec, now_ts=None, limit_min=120):
    """直近変化の表の行。[{name,last_price,level,c5,c15,c60,last_time,stale}]。東証/PTS/米国ADR/DEXのうち系列がある市場だけ。"""
    import time as _t
    now_ts = _t.time() if now_ts is None else now_ts
    eh = (rec or {}).get("extended_hours") or {}
    tr = eh.get("trend") or {}
    lv = {"tse": ((rec or {}).get("price") or {}).get("change_pct")}
    for k in ("pts", "adr", "dex"):
        lv[k] = (eh.get(k) or {}).get("change_pct") if isinstance(eh.get(k), dict) else None
    out = []
    for name, k in (("東証", "tse"), ("PTS", "pts"), ("米国ADR", "adr"), ("DEX", "dex")):
        t = tr.get(k)
        if not isinstance(t, dict) or not isinstance(t.get("chg"), dict):
            continue
        c = t["chg"]
        lt = t.get("last_ts")
        out.append({"name": name, "last_price": t.get("last_price"), "level": lv.get(k),
                    "c5": c.get("5m"), "c15": c.get("15m"), "c60": c.get("60m"), "last_time": t.get("last_time") or "—",
                    "stale": bool(lt is not None and now_ts - float(lt) > limit_min * 60)})
    return out


def recent_table_html(rows):
    if not rows:
        return ""
    body = ""
    for r in rows:
        lp = r["last_price"]
        body += ("<tr class='%s'><td>%s%s</td><td>%s</td><td class='%s'>%s</td><td class='%s'>%s</td><td class='%s'>%s</td><td class='%s'>%s</td><td>%s</td></tr>" % (
            "stale" if r["stale"] else "", _h.escape(r["name"]), "(立会時間外)" if r["stale"] else "",
            ("%s" % format(float(lp), ",.0f")) if _f(lp) is not None else "—",
            _cls(r["level"]), _pct(r["level"]), _cls(r["c5"]), _pct(r["c5"]), _cls(r["c15"]), _pct(r["c15"]),
            _cls(r["c60"]), _pct(r["c60"]), _h.escape(str(r["last_time"]))))
    head = "<tr><th>市場</th><th>最終値(円)</th><th>終値比</th><th>5分</th><th>15分</th><th>1時間</th><th>最終約定</th></tr>"
    return ("<div class='rc-wrap' id='recent-table'><table class='rc'><caption>直近変化（東証行の終値比は前営業日終値比、PTS・ADR・DEXは東証最終値比。"
            "最終約定から120分超の行は薄グレー＝立会時間外）</caption><thead>" + head + "</thead><tbody>" + body + "</tbody></table></div>")


def summary3(rec, now=None):
    """3行サマリー(決定論・数値の言い換えのみ)。[(見出し, 文)]。材料が無い行は『データなし』。"""
    r = rec or {}
    pr, bd = r.get("price") or {}, r.get("board") or {}
    ga = str(r.get("generated_at") or "")
    try:
        g = _dt.datetime.fromisoformat(ga[:19])
        gl = "%d/%d %02d:%02d時点" % (g.month, g.day, g.hour, g.minute)
    except Exception:
        gl = "時点不明"
    last, chg = _f(pr.get("last")), _f(pr.get("change_pct"))
    basis = pr.get("change_basis")
    l1 = (("285A %s円(%s %s)" % (format(last, ",.0f"), basis, _pct(chg))) if (last is not None and basis) else
          ("285A %s円(前営業日終値比 %s)" % (format(last, ",.0f"), _pct(chg))) if last is not None else "株価データなし") + "｜" + gl
    eh = r.get("extended_hours") or {}
    ex = ["%s %s" % (lab, _pct((eh.get(k) or {}).get("change_pct"))) for lab, k in (("PTS", "pts"), ("ADR", "adr"), ("DEX", "dex"))
          if isinstance(eh.get(k), dict) and _f(eh[k].get("change_pct")) is not None]
    if ex:
        l1 += "｜時間外(東証最終値比) " + "・".join(ex)
    se = r.get("screen_extras") or {}
    parts = []
    sel = se.get("selloff")
    if sel:
        try:
            import selloff_check as _sc
            lp = _sc.lamps(sel, r.get("signal_cards"))
            parts.append(lp["title"] if lp.get("final") else "%s(%s)" % (lp["title"], lp.get("asof_label") or "時点不明"))   # 確定表示はタイトルに日付・時点が入るので二重にしない
        except Exception:
            pass
    bf = se.get("board_flow")
    if bf and bf.get("ratio") is not None:
        parts.append("板の買い/売り比 %s(%s)" % (bf["ratio"], bf.get("asof_label") or "時点不明"))
    l2 = "｜".join(parts) if parts else "データなし"
    bull, bear = _f(bd.get("bull_ratio")), _f(bd.get("bear_ratio"))
    if bear is not None and bull is not None:
        l3 = "投稿の弱気 %.1f%%・強気 %.1f%%(本日%s件)｜%s" % (bear * 100, bull * 100, format(int(bd.get("post_count_today") or 0), ","), gl)
    else:
        l3 = "データなし"
    return [("値位置・時間外", l1), ("需給・セリクラ", l2), ("掲示板の心理", l3)]


def summary3_html(rec):
    try:
        rows = summary3(rec)
        body = "".join("<div class='l'><span class='k'>%s</span><span>%s</span></div>" % (_h.escape(k), _h.escape(v)) for k, v in rows)
        return ("<div class='sum3' id='sum3'>" + body +
                "<div class='n'>公開レコードの数値の言い換えです(予想ではありません)。</div></div>")
    except Exception:
        return ""


def latest_txt(rec, generated_at_str=None, now_ts=None):
    """公開ルート直下の latest.txt(Markdown・UTF-8・10KB以下)。2026-11-26(ユーザー承認)。
    数値は公開HTMLと同じrec(apply_live_overrides後)から作る。AI考察本文は公開版と同一の文面(免責も削らない)。
    10KBを超える時は比較表・シグナルの節から削り、AI考察本文は削らない。失敗時は空文字(呼び手がindex.html公開を止めない)。"""
    try:
        r = rec or {}
        pr = r.get("price") or {}
        ac = r.get("ai_commentary") or {}
        out = ["# 掲示板投稿の詳細分析による投資情報(キオクシア285A・公開サマリー)", "",
               "- 更新: %s(公開ページ index.html と同一スナップショット)" % (generated_at_str or r.get("generated_at") or "—"),
               "- 研究・エンタメ用途・未検証。売買の推奨・投資助言ではありません。"]
        last = _f(pr.get("last"))
        if last is not None:
            out.append("- 285A 現在値: %s円(%s %s)" % (format(last, ",.0f"), pr.get("change_basis") or "前営業日終値比", _pct(pr.get("change_pct"))))
        out += ["", "## 3行サマリー"]
        for k, v in summary3(r):
            out.append("- %s: %s" % (k, v))
        sec_tbl = ["", "## 時間外・直近変化(東証・PTS・米国ADR・DEX)"]
        for x in recent_rows(r, now_ts):
            sec_tbl.append("- %s%s: 最終値 %s円・終値比 %s・5分 %s・15分 %s・1時間 %s・最終約定 %s" % (
                x["name"], "(立会時間外)" if x["stale"] else "",
                format(float(x["last_price"]), ",.0f") if _f(x["last_price"]) is not None else "—",
                _pct(x["level"]), _pct(x["c5"]), _pct(x["c15"]), _pct(x["c60"]), x["last_time"]))
        if len(sec_tbl) == 2:
            sec_tbl.append("- 直近変化のデータなし")
        sec_sig = ["", "## シグナル発火状況(9指標)"]
        for c in r.get("signal_cards") or []:
            v = c.get("value")
            sec_sig.append("- %s: %s%s" % (c.get("name"), c.get("state"), (" (%s)" % v) if v is not None else ""))
        sec_ext = ["", "## 信用倍率・次の重要イベント",
                   "- このサイトの公開データには含まれません(姉妹サイト AIセクター ワールドモニター https://ethanhuntstock.github.io/ai-sector-monitor/ で確認できます)。"]
        sec_ai = ["", "## AI考察(公開ページと同一の本文)"]
        if ac.get("text"):
            sec_ai.append(str(ac.get("text")))
            sec_ai += ["", "%s(生成時刻: %s)" % ("", ac.get("generated_at") or "不明")]
        else:
            sec_ai.append("AI考察は準備中です。")
        foot = ["", "## 免責"] + ["- " + str(r.get("disclaimer") or "")] + ["- " + x for x in DISCLAIMER_ITEMS]
        def build(*parts):
            return "\n".join(out + [l for p in parts for l in p] + sec_ai + foot) + "\n"
        txt = build(sec_tbl, sec_sig, sec_ext)
        if len(txt.encode("utf-8")) > 10 * 1024:
            txt = build(sec_tbl)
        if len(txt.encode("utf-8")) > 10 * 1024:
            txt = build()
        return txt
    except Exception:
        return ""
