# -*- coding: utf-8 -*-
"""公開ページ最上部の共通スイッチバー(2026-11-25 ユーザー承認④)。静的HTML版とStreamlit版で共用。
左=現在ページ(aria-current=page・青枠+太字・リンクなし)／右=AIセクター ワールドモニター(外部リンク)。
参考: ai_sector_monitor/kpi_cards.py UI_CSS の .sw-bar(読み取り専用で参照・同じ見た目)。
色は既存のCSS変数(--border/--text/--panel)に合わせ、変数が無い環境(Streamlit)は中立色のフォールバック。純関数。"""
import html as _h

MONITOR_URL = "https://ethanhuntstock.github.io/ai-sector-monitor/"
CURRENT_LABEL = "📊 掲示板・板分析"
MONITOR_LABEL = "🧠 AIセクター ワールドモニター"

CSS = (".sw-bar{display:flex;gap:6px;margin:6px 0 8px;flex-wrap:wrap}"
       ".sw-bar .sw-i{flex:1 1 160px;box-sizing:border-box;text-align:center;padding:7px 10px;"
       "border:1px solid var(--border,rgba(128,128,128,.5));border-radius:8px;text-decoration:none;"
       "color:var(--text,inherit);font-size:.9em;background:var(--panel,transparent)}"
       ".sw-bar .sw-i.on{border-color:#3b82f6;font-weight:700;box-shadow:inset 0 0 0 1px #3b82f6}")


def switch_bar_html(with_style=False):
    s = ("<style>" + CSS + "</style>") if with_style else ""
    return (s + "<nav class='sw-bar' id='sw-bar' aria-label='サイト切替'>"
            "<span class='sw-i on' aria-current='page'>" + _h.escape(CURRENT_LABEL) + "</span>"
            "<a class='sw-i' href='" + MONITOR_URL + "' target='_blank' rel='noopener noreferrer'>"
            + _h.escape(MONITOR_LABEL) + "</a></nav>")
