# -*- coding: utf-8 -*-
"""
generate_static_dashboard.py - 一般公開用「画像貼り付け版」静的ダッシュボード生成スクリプト。

★2026-08-24追加(ユーザー依頼「Streamlitを使わない、画像貼り付け版の静的ダッシュボードを、
別アドレスで新規に作りたい」)。既存の public_dashboard.py (Streamlit・Streamlit Community
Cloudで公開中) には一切手を触れず、完全に独立した追加の成果物として作る。

【最重要・構造的な漏洩防止】このスクリプトは data/public_export/latest.json (=
public_export.build_public_record() が組み立て、validate_no_leak() を通過済みの
集計値のみのレコード) と、板CSV(config.KABU_PROTO1_285A_BOARD_DIR・読み取り専用・
board_totals_bridge.py と同じソース) だけを入力にする。個別投稿の抜粋・著者情報・
URL・投稿ID等はどちらの入力にも構造的に含まれない(public_export.py側のホワイト
リスト方式+validate_no_leak()の多重防御・board CSVは板の数量集計のみで投稿を含まない)。

データソース(既存Streamlit版との違い):
  - Streamlit Cloud版はGoogle Sheetsブリッジ経由でデータを受け取るが、このスクリプトは
    家PC1のローカルパイプラインの一部として動く想定のため、data/public_export/latest.json
    をローカルファイルとして直接読み、板総計(board_totals_60s_series)もローカルの
    板CSVから直接組み立てる(ネットワーク越しの取得は不要)。
  - 過去24時間センチメント・当日センチメント・14日価格/センチメント等、latest.json に
    既に含まれるフィールドはそのまま使う(raw_comments.jsonl等の重い生データは読まない=
    このスクリプトはpublic_export.pyの集計済み出力のみを消費)。
  - ★2026-08-24追加(ユーザー依頼「株価・PTS・ADRを真の1分更新にしたい」): 株価
    (rec["price"]・本日の価格チャート系列)とPTS・米国ADR(rec["extended_hours"])は
    latest.json由来だと上流BBSパイプライン(10分間隔)の鮮度にしか追随できないため、
    board総計と同じ考え方で、生成のたびに元データを直接取得して差し替える
    (apply_live_overrides()参照)。株価はkabuティックCSV(live_price_bridge.pyと同一
    ロジック)、PTS/ADRはnikkei225jp.comへの直接HTTPリクエスト。いずれも取得できな
    ければlatest.json由来の値のままfail-softで動作継続する。

画像生成: plotly + kaleido で静的PNGへ書き出す(Streamlit版のダッシュボードと配色・
構成を極力揃える)。kaleido が使えない場合は matplotlib へフォールバックする
(このスクリプト内で自動判定・報告時にどちらを使ったか明記する)。

出力: public_static_site/index.html + public_static_site/images/*.png
      ビルドステップ不要・GitHub Pages等どのホスティングでもそのまま配信できる形。

実行方法: python generate_static_dashboard.py
  (手動実行のみ。タスクスケジューラへの新規登録はこのスクリプトの範囲外)

既存ファイルへの影響: public_dashboard.py / public_export.py / config.py は
一切編集しない(読み取り専用でimportするだけ)。
"""
import csv
import datetime as dt
import html
import json
import os
import sys
import time

import config
try:
    import oniya_phase2_paths as _p2   # Phase2: 当日ticks/boardをローカル/OneDriveのmtime新しい方で解決(ポインタ未設置なら従来パス)
except Exception:
    _p2 = None
import public_export
import ui5 as _ui5
from switch_bar import CSS as _SWITCH_BAR_CSS, switch_bar_html as _switch_bar_html  # 2026-11-25 ④

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except Exception:
    HAS_PLOTLY = False

try:
    import kaleido  # noqa: F401 (存在確認のみ・実際のexportはfig.write_image()経由)
    HAS_KALEIDO = HAS_PLOTLY
    # ★2026-09-09追加(Cドライブ逼迫の再調査で発見・真因の本丸)。
    # kaleidoの実体であるchoreographerは画像出力のたびに使い捨ての一時プロファイル
    # (--user-data-dir=毎回新規のTempフォルダ)でヘッドレスChromeを新規起動する。
    # Chromeは「初めて見るプロファイル」と判断し、毎回Googleドキュメントオフライン
    # 等バンドル済み既定拡張機能のインストールを試みる(Temp\scoped_dir*\CRX_INSTALL
    # が生成される)。ヘッドレス実行はそのインストールを完走せず終了するため、
    # scoped_dirが画像1枚の出力ごとに1〜2個ずつ永久に残り続けディスクを逼迫させて
    # いた(実測:本タスクは1分毎実行・200秒で新規6個ペース=1日で数千個規模。
    # Web2PDF拡張機能の外部インストールレジストリキー(HKCU/HKLM)を削除した後も
    # 同じ症状が続いたことで、レジストリ経由の特定拡張機能でなくChromeの既定拡張
    # インストール機構そのものが原因と判明)。
    # choreographerのget_cli()(browsers/chromium.py)は--disable-extensions/
    # --disable-default-appsを渡していないため、呼び出し側の本ファイルで実行時に
    # get_cliをmonkey-patchして追加する(site-packages本体は編集しない=pip更新で
    # 消えても実害なし・patch失敗時もfail-softで画像生成自体は継続)。
    # ★2026-09-09追記＝上記2フラグだけではscoped_dir\CRX_INSTALLフォルダの生成は
    # 止まったが、Temp直下に単独で残る<GUID>.tmp(拡張機能CRXパッケージ本体の
    # ダウンロードファイル。先頭4バイトCr24で確認・5.4MB/248KB/161KBの3サイズが
    # 数千個単位で反復生成されているのを実測)はまだ止まっていなかった。これは
    # 拡張機能の「読み込み」を止める--disable-extensionsとは別に、Chromeの
    # バックグラウンド更新チェッカー(拡張機能アップデータを含む)がネットワーク
    # 経由でCRXをダウンロードする段階で発生するため、そのネットワーク処理自体を
    # 止める--disable-background-networkingを追加する。
    try:
        from choreographer.browsers import chromium as _choreo_chromium
        _orig_chromium_get_cli = _choreo_chromium.Chromium.get_cli

        def _get_cli_no_default_extensions(self):
            cli = list(_orig_chromium_get_cli(self))
            for _flag in ("--disable-extensions", "--disable-default-apps",
                          "--disable-background-networking"):
                if _flag not in cli:
                    cli.append(_flag)
            return cli

        _choreo_chromium.Chromium.get_cli = _get_cli_no_default_extensions
    except Exception as _e:
        print(f"[WARN] kaleido/choreographer chrome-flag patch failed (fail-soft, "
              f"scoped_dir cleanup patch skipped): {_e}")
except Exception:
    HAS_KALEIDO = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

IMAGE_BACKEND = "plotly+kaleido" if HAS_KALEIDO else ("matplotlib" if HAS_MATPLOTLIB else None)


# ============================================================================
# 出力先
# ============================================================================
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "public_static_site")
IMG_DIR = os.path.join(OUT_DIR, "images")
INDEX_PATH = os.path.join(OUT_DIR, "index.html")

# ★2026-08-24追加(コーデ経由のユーザー指摘対応: 「サーバー側は1分間隔で中身が
# 更新されていても、閲覧者側のブラウザタブは手動リロードしない限り古いまま」)。
# public_dashboard.py の PUBLIC_DASHBOARD_AUTOREFRESH_SEC(=60) と揃え、生成される
# index.html自体に<meta http-equiv="refresh">を埋め込んでブラウザ側を自動再読み込み
# させる(BBS_StaticDashboard_285Aタスクのサーバー側更新間隔=1分と一致させる)。
STATIC_DASHBOARD_AUTOREFRESH_SEC = 60


# ============================================================================
# 配色(dashboard.py の COL/REGIME_BANDS/_REGIME_LABEL_JA と揃える・意図的な
# 独立コピー)。dashboard.py を直接importするとStreamlit依存の重いモジュール群
# (insight/alerts/cluster_trend/moomoo_source/research等)が連鎖的にロードされる
# ため、この軽量スクリプトでは色定数のみを複製する。dashboard.py側の配色を
# 変えた場合はこちらも合わせて更新すること(このプロジェクトの他の意図的独立
# コピーと同じ規律)。
# ============================================================================
COL = {
    "bg": "#F4F6F9", "panel": "#FFFFFF", "border": "#D5DBE5", "muted": "#55606F",
    "text": "#1B2333", "orange": "#C2570B", "red": "#C81E1E", "green": "#15803D",
    "purple": "#6D28D9", "blue": "#1D4ED8", "yellow": "#B45309", "grey": "#8B94A3",
}
REGIME_BANDS = [("calm", "#D6F0E0"), ("normal", "#D6E4FA"),
                ("elevated", "#FBE7C6"), ("extreme", "#F7CFCF")]
_REGIME_LABEL_JA = {"calm": "平穏", "normal": "平常", "elevated": "警戒",
                     "extreme": "急変", "calibrating": "較正中"}
_STATE_COLOR = {"発火": COL["red"], "警戒": COL["orange"], "OK": COL["green"]}

_SIGNAL_DESC = {
    "灼熱メーター(過熱)": "掲示板全体の強気/弱気の偏りと投稿の勢いから算出した「過熱度」の合成指標。",
    "そう思う大量票": "Yahoo!掲示板の「そう思う」共感ボタンが1投稿に集中して大量に押されているか。",
    "イナゴ語彙(euphoria)": "「爆上げ」「握力」等、熱狂・便乗を示す語彙が投稿にどれだけ含まれるか。",
    "ネームド集中": "特定の常連投稿者(ハッシュ化ID)に投稿が偏っていないか(少数の声が目立ちすぎていないか)。",
    "他銘柄混入率": "本銘柄の掲示板で他の銘柄コードへの言及がどれだけ多いか(関心が他へ移りつつあるサイン)。メモリ/半導体ピア(MU・SOX・ハイニックス等)の言及は除外して数えます。",
    "暴落煽り語彙": "「暴落」「終わった」等、悲観・煽りを示す語彙が投稿にどれだけ含まれるか。",
    "阿鼻叫喚(セリクラ)": "株価が安値圏にある中で悲観語彙が急増しているか(セリングクライマックスの兆候)。",
    "話題枯れ": "過去の平均と比べて本日の投稿数が著しく少ないか(閑散・関心低下のサイン)。",
    "投稿サージ": "過去の平均と比べて本日の投稿数が著しく多いか(急な注目集中のサイン)。",
}


# ============================================================================
# 純関数(データ整形・ロジック) - selftest_generate_static_dashboard.py で検証
# ============================================================================
def mmdd(d):
    """"YYYY-MM-DD" -> "M/D"。不正な形式はそのまま返す(public_dashboard.py._mmddと同一ロジック)。"""
    if not d or "-" not in d:
        return d
    parts = d.split("-")
    if len(parts) != 3:
        return d
    return f"{int(parts[1])}/{int(parts[2])}"


def today_time_buckets_10m():
    """東証立会時間(前場9:00-11:20・後場12:30-15:30、昼休み除外)ぶんの10分刻み
    "HH:MM"ラベルを時刻昇順で返す(public_dashboard.py._today_time_bucketsと同一ロジック)。"""
    labels = []
    for start_min, end_min in ((9 * 60, 11 * 60 + 20), (12 * 60 + 30, 15 * 60 + 30)):
        for m in range(start_min, end_min + 1, 10):
            labels.append(f"{m // 60:02d}:{m % 60:02d}")
    return labels


def today_time_buckets_60s():
    """同上の1分刻み版(板総計チャート用。public_dashboard.py._today_time_buckets_60sと同一ロジック)。"""
    labels = []
    for start_min, end_min in ((9 * 60, 11 * 60 + 20), (12 * 60 + 30, 15 * 60 + 30)):
        for m in range(start_min, end_min + 1, 1):
            labels.append(f"{m // 60:02d}:{m % 60:02d}")
    return labels


_TODAY_TIME_BUCKETS = today_time_buckets_10m()
_TODAY_TIME_BUCKETS_SET = set(_TODAY_TIME_BUCKETS)
_TODAY_TIME_BUCKETS_60S = today_time_buckets_60s()
_TODAY_TIME_BUCKETS_60S_SET = set(_TODAY_TIME_BUCKETS_60S)


def effective_buckets(template, actual_times):
    """固定テンプレと実データ時刻集合の和集合を時刻順で返す(固定rangeの外に実データが
    押し出されて非表示になる事故の再発防止・public_dashboard.py._effective_today_bucketsと同一ロジック)。"""
    return sorted(set(template or []) | set(actual_times or []))


def rgba(hex_color, alpha):
    """#RRGGBB -> 'rgba(r,g,b,a)'(plotlyは8桁hexを受け付けないため。dashboard.py._rgbaと同一ロジック)。"""
    h = (hex_color or "").lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def html_escape_multiline(text):
    """自由記述テキスト(AI考察等)をHTMLへ安全に埋め込む形へ変換する純関数。
    html.escape()で特殊文字(<, >, &, ", ')をエスケープしたうえで改行を<br>へ変換する。"""
    if not text:
        return ""
    escaped = html.escape(str(text))
    return escaped.replace("\r\n", "\n").replace("\n", "<br>\n")


def fmt_price(value):
    return f"{value:,.0f}円" if value is not None else "—"


def fmt_pct(value, signed=True):
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def fmt_ratio_pct(value):
    return f"{value:.1%}" if value is not None else "—"


def chg_color(value):
    if value is None:
        return COL["grey"]
    return COL["red"] if value > 0 else (COL["blue"] if value < 0 else COL["grey"])


def state_badge_html(state, note=""):
    color = _STATE_COLOR.get(state, COL["grey"])
    return (f"<span style='display:inline-block;border-radius:999px;padding:2px 10px;"
            f"font-size:.82em;font-weight:600;color:#fff;background:{color};"
            f"white-space:nowrap'>{html.escape(str(state))}{(' ' + html.escape(note)) if note else ''}</span>")


# ============================================================================
# ★2026-08-24追加(Streamlit版public_dashboard.pyとのパリティ監査で発見した
# 抜け漏れの是正)。以下4つはいずれもStreamlit版に既にある表示要素の画像版
# ポート(同じ計算はpublic_export.py側の純関数へ委譲し、ここではHTML整形のみを
# 担う・selftest_generate_static_dashboard.pyで個別に検証)。
# ============================================================================
def outlier_caption_html(times, values, label="投稿量"):
    """件数・数量系の時系列に外れ値があれば注記HTMLを返す(無ければ空文字列)。
    public_dashboard.py._outlier_captionと同一ロジック(検出のみ・値は書き換えない・
    捏造しない設計原則どおり)。"""
    pts = [{"time": t, "v": v} for t, v in zip(times or [], values or [])]
    flagged = public_export.detect_series_outliers(pts, "v")
    if not flagged:
        return ""
    shown = "、".join(str(t) for t in flagged[:5])
    more = f" ほか{len(flagged) - 5}件" if len(flagged) > 5 else ""
    return (f"<p class='caption'>⚠️ {html.escape(label)}に自動検出された外れ値: "
            f"{html.escape(shown)}{html.escape(more)}"
            "(収集の一括キャッチアップ等が原因の可能性・値は捏造/削除せずそのまま表示しています)</p>")


def staleness_warning_html(generated_at, now, stale_minutes, in_trading_hours):
    """データの最終更新(generated_at)からnowまでの経過分がstale_minutesを超えており、
    かつ取引時間中(in_trading_hours)であれば警告HTMLを返す(それ以外は空文字列)。
    public_dashboard.py._header内のstale_warningと同型のロジック(経過分の計算は
    public_export.live_price_staleness_minutes()を共用し、ここでは戻り値の解釈のみを
    純関数として切り出してテスト可能にする)。"""
    staleness = public_export.live_price_staleness_minutes(generated_at, now=now)
    if staleness is not None and staleness > stale_minutes and in_trading_hours:
        return (f"<p class='caption'>⚠️ データの更新が約{staleness:.0f}分前から止まっている"
                f"可能性があります(通常は1分毎に自動生成)。</p>")
    return ""


def format_visit_count_html(count):
    """累計閲覧数バッジのHTML化(サーバー側で取得した初期値・フォールバック用)。
    countがNoneなら非表示=空文字列。public_dashboard.py._visit_counter_badgeと
    同じ表示形式。id='visit-count-badge'を持たせ、閲覧者のブラウザ側JS
    (visit_counter_js())が成功すればこの中身を実際のhit後の値へ差し替える
    (JS失敗時はこのサーバー側取得値がそのまま表示され続ける=二段構えのfail-soft)。"""
    if count is None:
        return "<p id='visit-count-badge' class='caption' style='display:none'></p>"
    return f"<p id='visit-count-badge' class='caption'>👀 累計閲覧数: {count:,}</p>"


LIKE_PROMPT_TEXT = "このダッシュボードが役に立ったら押してください"
LIKE_DONE_TEXT = "いいねしました！"


def format_like_button_html(count):
    """★2026-08-27追加(ユーザー依頼「ダッシュボードに、いいねボタンを足せますか」)。
    いいねボタンのHTML化(サーバー側で取得した初期の累計いいね数)。
    countがNoneなら非表示=空文字列。public_dashboard.py._like_buttonと同じ表示形式。
    ボタン要素(id='like-button')・ラベル要素(id='like-count-label')を持たせ、
    閲覧者のブラウザ側JS(like_button_js())がクリックイベント登録と表示更新を
    担当する(このHTML自体はJS無効時のフォールバック表示になる=二段構えの
    fail-soft・visit_counter_js/format_visit_count_htmlと同じ設計)。
    """
    if count is None:
        return "<p id='like-button-wrap' class='caption' style='display:none'></p>"
    count_text = f"（累計 {count:,} いいね）"
    return (
        "<p id='like-button-wrap' class='caption'>"
        "<button id='like-button' type='button' "
        "style='cursor:pointer;border:1px solid var(--border);border-radius:6px;"
        "padding:1px 10px;margin-right:6px;background:var(--panel);"
        "color:var(--text);font-size:1em;vertical-align:middle'>👍</button>"
        f"<span id='like-count-label'>{html.escape(LIKE_PROMPT_TEXT)}{html.escape(count_text)}</span>"
        "</p>"
    )


def like_button_js(url):
    """★2026-08-27追加(ユーザー依頼「ダッシュボードに、いいねボタンを足せますか」)。
    visit_counter_js()と同じGAS Web App(apps_script_visit_counter.gs)を流用し、
    action=like_hit(+1して現在値)・action=like_read(現在値のみ)でいいね数を
    集計する。閲覧数カウンターはsessionStorage(タブを閉じるまでの1回)で
    二重加算を防ぐのに対し、いいねは「一度押したら以後は押せない」のが自然な
    挙動のため、localStorage(ブラウザに永続・タブを閉じても再訪問時に押せない)
    でクリック済み判定する点が異なる。GAS Web Appは実測で一時的に404/タイムアウト
    を返すことがある既知の不安定さがあるため、fetchに5秒のタイムアウトを設け、
    失敗時は例外を握りつぶしボタンを再度押せる状態へ戻す(コンソールにwarnだけ
    出す・ページの他の表示には一切影響させない)。
    """
    if not url:
        return ""
    url_js = json.dumps(url)
    liked_text_js = json.dumps(LIKE_DONE_TEXT)
    count_prefix_js = json.dumps("（累計 ")
    count_suffix_js = json.dumps(" いいね）")
    return (
        "<script>(function(){"
        f"var url={url_js};"
        "var KEY='bbs_liked';"
        "var btn=document.getElementById('like-button');"
        "var label=document.getElementById('like-count-label');"
        "if(!btn||!label)return;"
        "function renderLiked(count){"
        "btn.disabled=true;btn.style.cursor='default';btn.style.opacity='0.6';"
        "btn.textContent='\\u2705';"
        f"label.textContent={liked_text_js}+(typeof count==='number'?"
        f"{count_prefix_js}+count.toLocaleString()+{count_suffix_js}:'');"
        "}"
        "if(localStorage.getItem(KEY)){renderLiked(null);}"
        "btn.addEventListener('click',function(){"
        "if(localStorage.getItem(KEY))return;"
        "btn.disabled=true;"
        "var ctrl=(typeof AbortController!=='undefined')?new AbortController():null;"
        "var timer=ctrl?setTimeout(function(){ctrl.abort();},5000):null;"
        "fetch(url+'?action=like_hit',{signal:ctrl?ctrl.signal:undefined})"
        ".then(function(r){if(timer)clearTimeout(timer);return r.json();})"
        ".then(function(data){"
        "localStorage.setItem(KEY,'1');"
        "renderLiked(data&&typeof data.count==='number'?data.count:null);"
        "})"
        ".catch(function(e){if(timer)clearTimeout(timer);btn.disabled=false;"
        "console.warn('like button fetch failed (fail-soft):',e);});"
        "});"
        "})();</script>"
    )


def tab_nav_js():
    """★2026-09-08追加(ユーザー指示「AIセクターワールドモニターと同様に、各表示を
    グルーピングしてタブで選択できるように」)。モニタ(ai_sector_monitor)の
    static/js/tab_nav.jsと全く同じ発想(素のJS・CSSクラスの付け外しだけでグループの
    表示を切り替える・新規の外部ライブラリは使わない)をこのダッシュボード向けに
    移植したもの。render_html()側で`.tab-bar#tabBar`+`.tab-btn[data-tab]`+
    `[data-tab-panel]`のマークアップを用意済み。

    モニタ版との違い: このダッシュボードは1分毎の自動更新(autorefresh_js)で
    ページ全体を再読み込みする設計のため、タブ切替時にページ内スクロールは
    動かさない(モニタのようにタブバーへスクロールしない)——自動更新の
    たびにトップへ戻らないようにした2026-09-06の対策と同じ理由で、ユーザーの
    閲覧位置を不必要に動かさない設計に統一する。

    失敗しても壊れない設計: タブバー/パネルが見つからなければ何もしない
    (他のJS[visit_counter_js等]と同じ「静かに劣化する」方針)。
    """
    return (
        "<script>(function(){"
        "var DEFAULT_TAB='aiinsight';"
        "function activate(tabName){"
        "var buttons=document.querySelectorAll('.tab-bar .tab-btn');"
        "var panels=document.querySelectorAll('[data-tab-panel]');"
        "if(!buttons.length||!panels.length)return;"
        "var matched=false;"
        "panels.forEach(function(p){"
        "var on=p.getAttribute('data-tab-panel')===tabName;"
        "if(on)matched=true;"
        "p.classList.toggle('tab-hidden',!on);"
        "});"
        "if(!matched){"
        "panels.forEach(function(p){p.classList.toggle('tab-hidden',"
        "p.getAttribute('data-tab-panel')!==DEFAULT_TAB);});"
        "tabName=DEFAULT_TAB;"
        "}"
        "buttons.forEach(function(b){"
        "var on=b.getAttribute('data-tab')===tabName;"
        "b.classList.toggle('active',on);"
        "b.setAttribute('aria-selected',on?'true':'false');"
        "});"
        "}"
        "document.addEventListener('DOMContentLoaded',function(){"
        "var bar=document.getElementById('tabBar');"
        "if(!bar)return;"
        "bar.addEventListener('click',function(e){"
        "var btn=e.target.closest('.tab-btn');"
        "if(!btn)return;"
        "var nm=btn.getAttribute('data-tab');activate(nm);"
        "try{sessionStorage.setItem('oniya_active_tab',nm);}catch(e){}"
        "});"
        "var saved=null;try{saved=sessionStorage.getItem('oniya_active_tab');}catch(e){}"
        "activate(saved||DEFAULT_TAB);"
        "});"
        "})();</script>"
    )


def visit_counter_js(url):
    """★2026-08-24追加(ユーザー依頼「画像版でも閲覧者数を計上したい」)。
    generate_static_dashboard.py実行時(=1分間隔のサーバー側サイクル)にhitを
    打つと『閲覧者数』でなく『生成回数』を数えてしまうバグになるため、真の
    閲覧数計上はブラウザ側で行う。ページを開いた瞬間に1回だけhit(このタブで
    未送信ならhit・送信済みならread)し、成功すればformat_visit_count_html()の
    プレースホルダーへ実際の値を書き込む。

    GAS Web App(apps_script_visit_counter.gs)は実測で一時的に404/タイムアウトを
    返すことがある既知の不安定さがあるため、fetchに5秒のタイムアウトを設け、
    失敗時は例外を握りつぶしてサーバー側取得値(フォールバック表示)をそのまま
    残す(コンソールにwarnだけ出す・ページの他の表示には一切影響させない)。
    sessionStorageで「このタブは既にhit送信済みか」を判定し、60秒毎の自動
    リロード(index.html自身のautorefresh_js)のたびに二重加算しないようにする
    (public_dashboard.py._visit_counter_badgeのst.session_state相当)。
    """
    if not url:
        return ""
    url_js = json.dumps(url)
    return (
        "<script>(function(){"
        f"var url={url_js};"
        "var KEY='bbs_visit_counted';"
        "var already=sessionStorage.getItem(KEY);"
        "var action=already?'read':'hit';"
        "var ctrl=(typeof AbortController!=='undefined')?new AbortController():null;"
        "var timer=ctrl?setTimeout(function(){ctrl.abort();},5000):null;"
        "fetch(url+'?action='+action,{signal:ctrl?ctrl.signal:undefined})"
        ".then(function(r){if(timer)clearTimeout(timer);return r.json();})"
        ".then(function(data){"
        "if(!already)sessionStorage.setItem(KEY,'1');"
        "if(data&&typeof data.count==='number'){"
        "var el=document.getElementById('visit-count-badge');"
        "if(el){el.textContent='\\u{1F440} \\u7d2f\\u8a08\\u95b2\\u89a7\\u6570: '+data.count.toLocaleString();"
        "el.style.display='';}"
        "}"
        "})"
        ".catch(function(e){if(timer)clearTimeout(timer);console.warn('visit counter fetch failed (fail-soft):',e);});"
        "})();</script>"
    )


def autorefresh_js(seconds=None):
    """★2026-08-24追加(コーデ経由のユーザー指摘対応・同日中に2回再修正): サーバー側
    (タスクスケジューラ・BBS_StaticDashboard_285A)が1分間隔でこのファイルを再生成
    していても、閲覧者のブラウザタブは手動リロードしない限り古い内容のまま
    表示され続けてしまう問題への対応。<meta http-equiv="refresh">はGitHub Pages側の
    CDNキャッシュにより毎回同じ古いHTMLが返され続ける実害を確認済みのため不採用
    (詳細はこの関数がrender_html()にインライン実装されていた頃の旧コメント参照)。
    JavaScriptで毎回異なるクエリ文字列(現在時刻ベース)を付けて再ナビゲーションする
    ことでキャッシュキーを都度変え、CDN/ブラウザキャッシュを確実に回避する。
    初回訪問(クエリ無し)はlocation.replace()で即座に飛び直し、以降はsetTimeoutで
    seconds秒毎にlocation.hrefで再読込する。

    ★2026-09-06追加(ユーザー指示「自動更新時にトップに戻らないようにできるなら」)。
    従来の実装は定期リロードのたびに完全なページナビゲーションが発生し、ブラウザの
    標準動作としてスクロール位置が失われページ最上部に戻っていた。リロード直前に
    現在のスクロール位置(window.scrollY)をsessionStorageへ保存し、リロード後の
    load時に復元することで、閲覧中の位置を保ったまま自動更新できるようにする
    (visit_counter_js等と同じ「タブ単位で状態を持つ」sessionStorageの使い方)。
    初回訪問時(クエリ無し→即時replace)は保存値が無いため何もしない(スクロール
    位置0のままで問題ない=まだ何も見ていない)。
    """
    sec = seconds if seconds is not None else STATIC_DASHBOARD_AUTOREFRESH_SEC
    return (
        "<script>(function(){"
        "if(location.search.indexOf('_=')===-1){"
        "location.replace(location.pathname + '?_=' + Date.now());"
        "}else{"
        "var _y=sessionStorage.getItem('bbs_scroll_y');"
        "if(_y!==null){"
        "sessionStorage.removeItem('bbs_scroll_y');"
        "window.addEventListener('load',function(){window.scrollTo(0,parseInt(_y,10)||0);});"
        "}"
        "setTimeout(function(){"
        "sessionStorage.setItem('bbs_scroll_y', String(window.scrollY));"
        "location.href = location.pathname + '?_=' + Date.now();"
        f"}}, {sec * 1000});"
        "}"
        "})();</script>"
    )


def signal_cards_daily_series(history_rows, days=14, today=None):
    """★2026-08-25追加、同日中に public_export.py へ移設(ユーザー指摘「公開用
    ダッシュボード(streamlit版)は直ってないのでは」=streamlit版にも同じシグナル推移
    スパークラインを追加する際、board_score_daily_series()と同じく読み取り専用
    モジュールへ集約する設計に揃えた)。本体は public_export.signal_cards_daily_series()
    に一本化し、ここは画像版の既存呼び出し元(load_signal_cards_history_14d等)を壊さない
    ための薄い委譲のみ(ロジックの二重管理を避ける・[[共有ロジック規律]])。"""
    return public_export.signal_cards_daily_series(history_rows, days=days, today=today)


def regime_caption_html(regime):
    """ボラ・レジーム帯の下に出す注記。dashboard.py.regime_band()と同じ分岐
    (BVP=vol_regime_score未確立時は較正中の説明ボックス、確立済みならBVP値付き
    キャプション)を再現する。"""
    regime = regime or {}
    vrs = regime.get("vol_regime_score")
    name = regime.get("vol_regime", "calibrating")
    label = html.escape(_REGIME_LABEL_JA.get(name, name))
    if vrs is None:
        return ("<div class='calib'>レジーム: <b>較正中</b> "
                "(BVP未確立・dense session蓄積待ち)</div>")
    return f"<p class='caption'>現在レジーム: {label}　(BVP={vrs})</p>"


# ============================================================================
# ローカルデータ読み込み(latest.json + 板CSV)
# ============================================================================
def load_record():
    rec = public_export.load_public_latest()
    if not rec:
        raise SystemExit(
            "data/public_export/latest.json が見つかりません。"
            "run_once.py を先に一度実行してエクスポートを生成してください。"
            f"(参照パス: {config.PUBLIC_EXPORT_LATEST_PATH})")
    return rec


def _today_board_csv_path(today):
    return (_p2.board_csv_path("285A", today, config.KABU_PROTO1_285A_BOARD_DIR) if _p2 is not None
            else os.path.join(config.KABU_PROTO1_285A_BOARD_DIR, f"board_285A_{today}.csv"))


def load_board_totals_series(today=None):
    """board_totals_bridge.py と同じソース(プロト1の板CSV・読み取り専用)から、
    public_export.board_totals_60s_series() で当日ぶんの60秒足買い/売り総計系列を
    組み立てる。ファイルが無い/読めない場合は空リスト(fail-soft・チャート自体を省略)。"""
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                      + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    path = _today_board_csv_path(today)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"[WARN] board CSV read failed ({path}): {e!r}")
        return []
    return public_export.board_totals_60s_series(rows, today=today)


def load_signal_cards_history_14d(rec, days=14, today=None):
    """★2026-08-25追加(ユーザー依頼「シグナル発火状況(9指標)の各行にミニグラフを」)。

    public_export.load_public_history()(history.jsonl・読み取り専用。同関数自体が
    ファイル欠損/破損行に対してfail-soft[]を返す設計)でhistory.jsonlを読み、純関数
    signal_cards_daily_series()で9指標それぞれの日次value推移(過去days日ぶん)を組み立て、
    末尾にrec(latest.json由来・今回このrunで計算済みの最新値)の現在値を1点追加する。
    board_history_14d(public_export._build_from_live_data内のboard_score_daily_series()
    呼び出し)と全く同じ「過去日分はhistory.jsonlから・今日ぶんは呼び手の現在値を追加」
    という組み立て順序を踏襲する。"""
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                      + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    # ★2026-10-18(おにや・メモリ低減案A・コーデ承認): history.jsonl全行(260MB超)は読まず、
    # 日付ごとの最後のsignal_cards付き行だけを末尾から逆読みする(結果は全行版と同一・失敗時は従来の全行読みへfallback)。
    try:
        history_rows = public_export.load_public_history_last_per_day(days=days, today=today)
        if not history_rows:
            history_rows = public_export.load_public_history()
    except Exception:
        history_rows = public_export.load_public_history()
    series = signal_cards_daily_series(history_rows, days=days, today=today)
    today_entry = {"date": today}
    for c in rec.get("signal_cards") or []:
        name = (c or {}).get("name")
        if name:
            today_entry[name] = c.get("value")
    series.append(today_entry)
    return series


# ============================================================================
# ★2026-08-24追加(ユーザー依頼「株価・PTS・ADRを真の1分更新にしたい」)。
#
# 背景: rec["price"] / rec["extended_hours"] は元々 data/public_export/latest.json
# (=上流BBSパイプライン run_once.py のcatchupサイクル・10分間隔でしか更新されない)
# 由来のため、この画像版を1分間隔で再生成しても中身は最大10分古いままだった
# (board_totals_bridge.py と同じ考え方=板CSVを直接読む方式に合わせる必要があった)。
#
# 対策: board_totals_series と同じ「latest.jsonを経由せず、生成のたびに元データを
# 直接取得する」方式を、株価(kabuティックCSV直接読み・live_price_bridge.pyと同一
# ロジック)とPTS/ADR(nikkei225jp.comへ直接HTTPリクエスト)にも適用する。
# いずれも取得できなければNoneを返し(fail-soft)、呼び手(_apply_live_overrides)は
# latest.json由来の既存値をそのまま使う(黙って空白にしない・失敗時はログに出す)。
# ============================================================================
def _today_ticks_csv_path(today):
    return (_p2.ticks_csv_path("285A", today, config.KABU_PROTO1_285A_TICKS_DIR) if _p2 is not None
            else os.path.join(config.KABU_PROTO1_285A_TICKS_DIR, f"ticks_285A_{today}.csv"))


def load_live_kabu_price(today=None):
    """live_price_bridge.py と同じロジックで、当日のkabuティックCSV
    (config.KABU_PROTO1_285A_TICKS_DIR)を直接読み、public_export.kabu_tick_today_summary()
    で現在値・騰落率・当日10分足系列を組み立てる(latest.jsonを経由しない)。
    前日終値は price_fetch.load_price(config.PRICE_DAILY_PATH) +
    public_export._previous_close_price() から求める。

    取得できない(ファイル無し/読込失敗/本日分の行が無い)場合はNone(fail-soft・
    呼び手は既存のlatest.json由来の値をそのまま使う)。"""
    today = today or (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                      + dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    path = _today_ticks_csv_path(today)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"[WARN] kabu tick CSV read failed ({path}): {e!r}")
        return None

    summary = public_export.kabu_tick_today_summary(rows, today=today)
    if summary.get("last") is None:
        return None

    prev_close = None
    try:
        import price_fetch
        price_daily = price_fetch.load_price(config.PRICE_DAILY_PATH)
        prev_close = public_export._previous_close_price(price_daily, today=today)
    except Exception as e:
        print(f"[WARN] could not derive prev_close for live kabu price: {e!r}")

    chg = (round((summary["last"] - prev_close) / prev_close * 100.0, 2)
          if prev_close else None)
    _tick_trend = None
    try:  # ★2026-11-23: 本日のティックから東証(285A)の直近変化(5分/15分/1時間/3時間)
        import trend_stats as _ts
        _tick_trend = _ts.trend_stats(_ts.tick_points(rows, today))
    except Exception as e:
        print(f"[WARN] tse tick trend failed: {e!r}")
    return {"price": {"last": summary["last"], "change_pct": chg},
           "price_pts": summary["price_pts"], "trend": _tick_trend}


def fetch_live_extended_hours(symbol=None, timeout=8):
    """画像生成のたびに config.ADR_PTS_URL (nikkei225jp.com) へ直接HTTPリクエストし
    (タイムアウト timeout 秒)、price_fetch.parse_adr_pts_js() で最新行を取得、
    public_export.extended_hours_summary() でPTS・米国ADRの最新値サマリーを組み立てる
    (latest.jsonを経由しない)。

    ネットワーク取得は必ずtry/exceptで囲み、失敗時はNoneを返す(fail-soft・呼び手は
    既存のlatest.json由来の値をそのまま使う。黙って空白にはしない=ログにWARNを出す)。
    単純なGETリクエスト1本のみでリトライは行わない(常識的な頻度を維持する方針)。"""
    try:
        import requests
        import price_fetch
    except Exception as e:
        print(f"[WARN] extended_hours live fetch unavailable (import failed): {e!r}")
        return None
    symbol = symbol or config.SYMBOL
    try:
        url = config.ADR_PTS_URL.format(symbol=symbol)
        r = requests.get(url, headers={"User-Agent": config.CHROME_UA,
                                       "Referer": config.ADR_PTS_REFERER.format(symbol=symbol)},
                         timeout=timeout)
        if r.status_code != 200:
            print(f"[WARN] adr_pts live fetch http {r.status_code}")
            return None
        rows = price_fetch.parse_adr_pts_js(r.text)
    except Exception as e:
        print(f"[WARN] adr_pts live fetch failed: {e!r}")
        return None
    if not rows:
        return None
    eh = public_export.extended_hours_summary({"rows": rows})
    try:  # ★2026-11-23: 東証・PTS・米国ADRの直近変化(フィードの時系列から)
        import trend_stats as _ts
        if eh is not None:
            eh["trend"] = _ts.rows_trends(rows)
    except Exception as e:
        print(f"[WARN] adr_pts trend failed: {e!r}")
    return eh


def apply_live_overrides(rec, today=None):
    """rec(latest.json由来)の株価(rec["price"]・rec["intraday_today"]["price"])と
    PTS/ADR(rec["extended_hours"])を、生成のたびに直接取得した最新値で差し替える
    (取得できなければ元の値のまま=fail-soft)。

    差し替え後は念のため public_export.validate_no_leak() を再実行し、万一リーク
    疑いキーが検出された場合は差し替え自体を取り消してrecを元に戻す(このモジュールが
    追加するのは数値/時刻の集計値のみで通常は発生しない想定だが、多重防御として
    validate_no_leak()を通過済みという既存の非公開原則をここでも維持する)。"""
    original_price = rec.get("price")
    original_intraday = rec.get("intraday_today")
    original_extended_hours = rec.get("extended_hours")

    live_price = load_live_kabu_price(today=today)
    if live_price:
        rec["price"] = live_price["price"]
        intraday = dict(rec.get("intraday_today") or {})
        intraday["price"] = live_price["price_pts"]
        rec["intraday_today"] = intraday
        print(f"[INFO] live kabu price applied: last={live_price['price']['last']} "
             f"chg={live_price['price']['change_pct']} n_pts={len(live_price['price_pts'])}")
    else:
        print("[INFO] live kabu price unavailable; keeping latest.json value (fail-soft)")

    live_eh = fetch_live_extended_hours()
    if live_eh:
        try:  # ★2026-10-16: DEX は記録済みスナップショット由来(live_eh は ADR/PTSフィード由来で dex を持たない)＝元の値を引き継ぐ
            if not live_eh.get("dex") and (original_extended_hours or {}).get("dex"):
                live_eh["dex"] = original_extended_hours.get("dex")
        except Exception:
            pass
        rec["extended_hours"] = live_eh
        print(f"[INFO] live extended_hours applied: "
             f"pts={(live_eh.get('pts') or {}).get('price')} "
             f"adr={(live_eh.get('adr') or {}).get('price_yen')}")
    else:
        print("[INFO] live extended_hours unavailable; keeping latest.json value (fail-soft)")

    try:  # ★2026-11-23(ユーザー指示『東証もPTSもADRもDEXも数時間から数分の変化も分析』): 直近変化を合成
        import trend_stats as _ts
        _eh = rec.get("extended_hours")
        if isinstance(_eh, dict):
            _tr = dict(_eh.get("trend") or {})
            if live_price and live_price.get("trend"):
                _tr["tse"] = live_price["trend"]        # 本日のティック(分単位)があれば東証はそちらを優先
            _dp = _ts.trend_stats(_ts.dex_points(config.DEX_SERIES_PATH))
            if _dp:
                _tr["dex"] = _dp
            if _tr:
                _eh["trend"] = _tr
    except Exception as e:
        print(f"[WARN] trend merge failed: {e!r}")

    original_screen_extras = rec.get("screen_extras")
    _ui5.fill_prev_session_change(rec, (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)).strftime("%Y-%m-%d"))  # 日付またぎの騰落率(2026-11-26)
    try:  # ★2026-11-25(ユーザー承認): 昼休み非表示・日付跨ぎの前営業日確定表示を毎分反映するため、板・tick由来の screen_extras を直接再計算
        #   (latest.jsonは10分毎更新で日付跨ぎ/昼休みに追従できない)。機能が上流で有効(latest.jsonにscreen_extrasがある/環境変数ON)の時だけ。moomoo画面CSVは開かない。
        if original_screen_extras is not None or os.environ.get("ONIYA_PUBLIC_SCREEN_EXTRAS", "").strip() == "1":
            import screen_flow_summary as _sf2
            _d = _sf2.load_today_public((dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                                         + dt.timedelta(hours=9)).strftime("%Y-%m-%d"))
            _ex = _sf2.public_safe_extras(_d.get("board"), _d.get("price"), _d.get("grades"), _d.get("selloff"))
            if _ex:
                rec["screen_extras"] = _ex
                print(f"[INFO] screen_extras refreshed live (selloff prev_date={(_ex.get('selloff') or {}).get('prev_date')})")
    except Exception as e:
        print(f"[WARN] screen_extras live refresh failed (keeping latest.json value): {e!r}")

    errs = public_export.validate_no_leak(rec)
    if errs:
        print(f"[WARN] live override introduced leak-risk keys, reverting: {errs}")
        rec["price"] = original_price
        rec["intraday_today"] = original_intraday
        rec["extended_hours"] = original_extended_hours
        rec["screen_extras"] = original_screen_extras
    return rec


# ============================================================================
# 画像生成(plotly+kaleido)
# ============================================================================
_FIG_HASH_PATH = os.path.join(config.DATA_DIR, "static_fig_hash_cache.json")   # 公開先(images/)へ複製されないようdata側(同期対象外)に置く


def _load_fig_hashes():
    try:
        with open(_FIG_HASH_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_fig_hashes(d):
    try:
        tmp = _FIG_HASH_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _FIG_HASH_PATH)
    except Exception:
        pass


def _save_fig(fig, filename, width=1040, height=None, scale=2):
    """★2026-09-24変更(O6・毎分タスクが1回70〜100秒かかり次の起動と重なって0x800710E0で拒否される件の恒久対策):
    図の内容(fig.to_json()+width/height/scale)が前回と同一で画像ファイルも存在すれば、重い画像出力(kaleido=Chromium)を
    省略する。内容が変われば従来どおり出力する。環境変数 STATIC_DASHBOARD_FORCE_IMAGES=1 で常に出力(検証用)。
    判定に失敗した時は従来どおり出力(fail-open)。"""
    os.makedirs(IMG_DIR, exist_ok=True)
    path = os.path.join(IMG_DIR, filename)
    kwargs = {"width": width, "scale": scale}
    if height:
        kwargs["height"] = height
    h = None
    try:
        import hashlib
        h = hashlib.sha1((fig.to_json() + "|%s|%s|%s" % (width, height, scale)).encode("utf-8")).hexdigest()
    except Exception:
        h = None
    hashes = _load_fig_hashes() if h else {}
    if (h and hashes.get(filename) == h and os.path.exists(path)
            and os.environ.get("STATIC_DASHBOARD_FORCE_IMAGES") != "1"):
        return filename
    fig.write_image(path, **kwargs)
    if h:
        hashes[filename] = h
        _save_fig_hashes(hashes)
    return filename


def gauge_zone_color(value, fire_th, warn_th, colors):
    """現在値がOK/警戒/発火のどのゾーンかに応じてcolors[0]/[1]/[2]を返す。

    ★2026-09-14追加(おにやGO・22:36エンジニア発見の是正③): 従来はbarの色が
    常にcolors[-1](=発火色)固定で、値が低くても危険色に見える不整合があった。
    warn_th省略時は既存のsteps境界(min(50,fire_th))をそのまま警戒開始点として使う
    (呼び出し元を増やさない後方互換・dashboard.py._gauge()と同型)。
    """
    warn_bound = warn_th if warn_th is not None else min(50, fire_th)
    if value >= fire_th:
        return colors[2]
    if value >= warn_bound:
        return colors[1]
    return colors[0]


def build_gauge_figure(value, title, fire_th, colors, warn_th=None):
    """dashboard.py._gauge()のFigure構築部分を再利用(st.plotly_chart呼び出しのみ除去)。"""
    value = value or 0
    warn_bound = warn_th if warn_th is not None else min(50, fire_th)
    f = go.Figure(go.Indicator(
        mode="gauge+number", value=value,
        number={"suffix": " /100", "font": {"size": 22, "color": COL["text"]}},
        title={"text": title, "font": {"size": 16, "color": COL["text"]}},
        gauge={"axis": {"range": [0, 100], "tickcolor": COL["muted"]},
               "bar": {"color": gauge_zone_color(value, fire_th, warn_th, colors)},
               "bgcolor": COL["bg"], "borderwidth": 0,
               "steps": [{"range": [0, warn_bound], "color": rgba(colors[0], 0.5)},
                         {"range": [warn_bound, fire_th], "color": rgba(colors[1], 0.62)},
                         {"range": [fire_th, 100], "color": rgba(colors[2], 0.78)}],
               "threshold": {"line": {"color": COL["text"], "width": 3}, "value": fire_th}}))
    f.update_layout(paper_bgcolor=COL["panel"], height=230,
                    margin=dict(l=34, r=34, t=54, b=8), font=dict(color=COL["text"]))
    return f


def build_sparkline_figure(history_14d, score_key, line_color, ticksuffix="/100"):
    """dashboard.py(public_dashboard.py)._meter_sparkline()のFigure構築部分を再利用。
    有効な点が2未満ならNoneを返す(呼び手は「データ蓄積中」キャプションを出す)。

    ticksuffix: y軸の単位表示。2大メーター(overheat_score/capitulation_score、
    常に0-100スケール)は既定の"/100"のまま。★2026-08-25追加(9指標シグナルカードの
    スパークライン化)で、value がスコアではなく指標ごとに単位がバラバラ(比率・z値・
    件数等)な場合に呼び手が空文字列を渡せるよう引数化した(既存呼び出し側は無変更で
    従来どおり"/100"のまま動く)。"""
    pts = [p for p in (history_14d or []) if p.get(score_key) is not None]
    if len(pts) < 2:
        return None
    date_labels = [mmdd(p.get("date")) for p in pts]
    values = [p.get(score_key) for p in pts]
    f = go.Figure(go.Scatter(x=date_labels, y=values, mode="lines+markers",
                             line=dict(color=line_color, width=3), marker=dict(size=5)))
    f.update_layout(height=90, margin=dict(l=38, r=8, t=4, b=22),
                    paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                    xaxis=dict(type="category", showgrid=False,
                              tickfont=dict(size=10, color=COL["muted"])),
                    yaxis=dict(showgrid=False, showticklabels=True, nticks=3,
                              ticksuffix=ticksuffix, tickfont=dict(size=9, color=COL["muted"])))
    return f


def build_regime_band_figure(regime):
    """dashboard.py.regime_band()のFigure構築部分を再利用。"""
    vrs = (regime or {}).get("vol_regime_score")
    f = go.Figure()
    for name, color in REGIME_BANDS:
        label = _REGIME_LABEL_JA.get(name, name)
        f.add_trace(go.Bar(x=[25], y=["regime"], orientation="h", marker_color=color,
                           name=label, text=label, textposition="inside",
                           insidetextanchor="middle", hoverinfo="name"))
    if vrs is not None:
        f.add_vline(x=vrs * 100, line=dict(color=COL["text"], width=3))
    f.update_layout(barmode="stack", paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                    height=100, showlegend=False, margin=dict(l=6, r=6, t=6, b=6),
                    xaxis=dict(range=[0, 100], visible=False),
                    yaxis=dict(visible=False), font=dict(color=COL["text"]))
    return f


def build_candle_volume_figure(x_labels, opens, highs, lows, closes, vols,
                                category_order=None, x_range=None):
    """ローソク足+出来高の2段組(14日/当日どちらの価格チャートにも使う共通ビルダー)。
    public_dashboard.py._price_and_sentiment_charts / _intraday_today_charts の
    ローソク足部分と同じ配色・構成。"""
    vol_colors = [COL["grey"] if (o is None or c is None) else
                 (COL["red"] if c >= o else COL["blue"])
                 for o, c in zip(opens, closes)]
    f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                      row_heights=[0.72, 0.28], vertical_spacing=0.035)
    f.add_trace(go.Candlestick(
        x=x_labels, open=opens, high=highs, low=lows, close=closes,
        increasing_line_color=COL["red"], decreasing_line_color=COL["blue"]), row=1, col=1)
    f.add_trace(go.Bar(x=x_labels, y=vols, marker_color=vol_colors, showlegend=False),
               row=2, col=1)
    f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                    height=340, margin=dict(l=12, r=12, t=10, b=10),
                    font=dict(color=COL["text"], size=13),
                    xaxis_rangeslider_visible=False, showlegend=False)
    for row in (1, 2):
        kw = dict(type="category", row=row, col=1)
        if category_order is not None:
            kw["categoryorder"] = "array"
            kw["categoryarray"] = category_order
        if x_range is not None:
            kw["autorange"] = False
            kw["range"] = x_range
        f.update_xaxes(**kw)
    f.update_yaxes(gridcolor=COL["border"], tickformat=",", row=1, col=1)
    f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
    return f


def build_sentiment_figure(x_labels, bulls, bears, neutrals, posts,
                           category_order=None, x_range=None):
    """強気/弱気(/中立)比率 + 投稿量の2段組(14日/当日/24時間いずれのセンチメント
    チャートにも使う共通ビルダー)。neutralsがNoneなら中立線は描かない。"""
    f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                      row_heights=[0.72, 0.28], vertical_spacing=0.04)
    f.add_trace(go.Scatter(x=x_labels, y=bulls, name="強気", mode="lines",
                           connectgaps=True, line=dict(color=COL["red"], width=3)),
               row=1, col=1)
    f.add_trace(go.Scatter(x=x_labels, y=bears, name="弱気", mode="lines",
                           connectgaps=True, line=dict(color=COL["blue"], width=3)),
               row=1, col=1)
    if neutrals is not None:
        f.add_trace(go.Scatter(x=x_labels, y=neutrals, name="中立", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["grey"], width=3, dash="dot")),
                   row=1, col=1)
    f.add_trace(go.Bar(x=x_labels, y=posts, marker_color=COL["muted"], showlegend=False),
               row=2, col=1)
    f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                    height=340, margin=dict(l=12, r=12, t=40, b=10),
                    font=dict(color=COL["text"], size=13),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                               xanchor="left", x=0))
    for row in (1, 2):
        kw = dict(type="category", row=row, col=1)
        if category_order is not None:
            kw["categoryorder"] = "array"
            kw["categoryarray"] = category_order
        if x_range is not None:
            kw["autorange"] = False
            kw["range"] = x_range
        f.update_xaxes(**kw)
    f.update_yaxes(tickformat=".0%", gridcolor=COL["border"], row=1, col=1)
    f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
    return f


def build_board_totals_figure(series):
    """板の買い/売り総計(60秒平均)折れ線。public_dashboard.py._board_totals_chartと同構成。"""
    series = [p for p in series if p.get("time") in _TODAY_TIME_BUCKETS_60S_SET]
    if not series:
        return None
    times = [p.get("time") for p in series]
    buys = [p.get("buy_total") for p in series]
    sells = [p.get("sell_total") for p in series]
    _eff = effective_buckets(_TODAY_TIME_BUCKETS_60S, times)
    f = go.Figure()
    f.add_trace(go.Scatter(x=times, y=buys, name="買い総計", mode="lines",
                           line=dict(color=COL["red"], width=3)))
    f.add_trace(go.Scatter(x=times, y=sells, name="売り総計", mode="lines",
                           line=dict(color=COL["blue"], width=3)))
    f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                    height=340, margin=dict(l=12, r=12, t=40, b=10),
                    font=dict(color=COL["text"], size=13),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                               xanchor="left", x=0))
    f.update_xaxes(type="category", categoryorder="array", categoryarray=_eff,
                   autorange=False, range=[-0.5, len(_eff) - 0.5],
                   tickmode="array", tickvals=_TODAY_TIME_BUCKETS, ticktext=_TODAY_TIME_BUCKETS)
    f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s")
    return f


# ============================================================================
# 画像生成(matplotlibフォールバック・kaleidoが使えない場合のみ)
# ============================================================================
def _mpl_candle_volume(x_labels, opens, highs, lows, closes, vols, path, title=""):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.4, 3.6), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]},
                                   facecolor=COL["panel"])
    xs = range(len(x_labels))
    for x, o, h, low, c in zip(xs, opens, highs, lows, closes):
        if None in (o, h, low, c):
            continue
        color = COL["red"] if c >= o else COL["blue"]
        ax1.plot([x, x], [low, h], color=color, linewidth=1)
        ax1.plot([x, x], [min(o, c), max(o, c)], color=color, linewidth=5, solid_capstyle="butt")
    vol_colors = [COL["grey"] if (o is None or c is None) else
                 (COL["red"] if c >= o else COL["blue"]) for o, c in zip(opens, closes)]
    ax2.bar(xs, [v or 0 for v in vols], color=vol_colors, width=0.8)
    for ax in (ax1, ax2):
        ax.set_facecolor(COL["panel"])
        ax.tick_params(colors=COL["muted"], labelsize=7)
        for spine in ax.spines.values():
            spine.set_color(COL["border"])
    step = max(1, len(x_labels) // 12)
    ax2.set_xticks(list(xs)[::step])
    ax2.set_xticklabels([x_labels[i] for i in xs][::step], rotation=45, ha="right", fontsize=7,
                        color=COL["muted"])
    if title:
        ax1.set_title(title, fontsize=10, color=COL["text"])
    fig.tight_layout()
    fig.savefig(path, dpi=170, facecolor=COL["panel"])
    plt.close(fig)


def _mpl_lines(x_labels, series, path, title="", pct=False):
    fig, ax = plt.subplots(figsize=(10.4, 3.0), facecolor=COL["panel"])
    xs = range(len(x_labels))
    for name, values, color in series:
        ax.plot(xs, values, label=name, color=color, linewidth=2)
    ax.set_facecolor(COL["panel"])
    ax.tick_params(colors=COL["muted"], labelsize=7)
    for spine in ax.spines.values():
        spine.set_color(COL["border"])
    step = max(1, len(x_labels) // 12)
    ax.set_xticks(list(xs)[::step])
    ax.set_xticklabels([x_labels[i] for i in xs][::step], rotation=45, ha="right", fontsize=7,
                       color=COL["muted"])
    if pct:
        ax.yaxis.set_major_formatter(lambda v, pos: f"{v:.0%}")
    ax.legend(fontsize=8, facecolor=COL["panel"], edgecolor=COL["border"])
    if title:
        ax.set_title(title, fontsize=10, color=COL["text"])
    fig.tight_layout()
    fig.savefig(path, dpi=170, facecolor=COL["panel"])
    plt.close(fig)


# ============================================================================
# 画像一式の生成(rec + board_totals_series から images/*.png を書き出す)
# ============================================================================
def generate_all_images(rec, board_totals_series):
    os.makedirs(IMG_DIR, exist_ok=True)
    images = {}
    board = rec.get("board") or {}
    board_history_14d = rec.get("board_history_14d") or []
    regime = rec.get("regime") or {}

    # ---- 共通データ準備(画像バックエンドの有無に関わらず使う。従来はkaleido/
    # matplotlibの各分岐内でそれぞれ個別に読み直していたが、下のextras計算
    # (高値安値・外れ値注記)でも同じ系列が要るため、ここで1回だけ組み立てて
    # 両分岐+extrasで共用する形へ整理する) ----
    intraday = rec.get("intraday_today") or {}
    price_pts = intraday.get("price") or []
    board_series_today = [p for p in (board_totals_series or [])
                          if p.get("time") in _TODAY_TIME_BUCKETS_60S_SET]
    sent_24h = rec.get("sentiment_last_24h") or []
    today_sent_pts = public_export.sentiment_today_from_last_24h(sent_24h)
    today_sent_pts = [p for p in today_sent_pts if p.get("time") in _TODAY_TIME_BUCKETS_SET]
    pss = list(rec.get("price_sentiment_series") or [])

    # ---- extras: 高値/安値・外れ値自動検出注記(★2026-08-24追加・Streamlit版
    # public_dashboard.pyとのパリティ監査で発見した抜け漏れの是正)。画像生成
    # 自体の成否(kaleido/matplotlib不在)に関わらず常に計算する軽量な付帯情報。 ----
    extras = {
        "day_high_low": public_export.intraday_today_high_low(price_pts),
        "outliers": {
            "today_sentiment": outlier_caption_html(
                [p.get("time") for p in today_sent_pts],
                [p.get("post_count") for p in today_sent_pts]),
            "sentiment_24h": outlier_caption_html(
                [p.get("time") for p in sent_24h],
                [p.get("post_count") for p in sent_24h]),
            "trend14_sentiment": outlier_caption_html(
                [mmdd(p.get("date")) for p in pss],
                [p.get("post_count") for p in pss]),
            "board_totals_buy": outlier_caption_html(
                [p.get("time") for p in board_series_today],
                [p.get("buy_total") for p in board_series_today], "買い総計"),
            "board_totals_sell": outlier_caption_html(
                [p.get("time") for p in board_series_today],
                [p.get("sell_total") for p in board_series_today], "売り総計"),
        },
    }

    if HAS_KALEIDO:
        # --- ゲージ2枚 + スパークライン2枚 ---
        _save_fig(build_gauge_figure(board.get("overheat_score") or 0, "🔥 灼熱メーター(過熱)",
                                     config.SIG_OVERHEAT_TH, [COL["green"], COL["yellow"], COL["orange"]],
                                     warn_th=config.SIG_OVERHEAT_WARN),
                  "gauge_overheat.png", width=420, height=230)
        images["gauge_overheat"] = "gauge_overheat.png"
        _save_fig(build_gauge_figure(board.get("capitulation_score") or 0, "😱 阿鼻叫喚メーター(セリクラ)",
                                     config.SIG_CAPITULATION_FIRE, [COL["green"], COL["orange"], COL["red"]],
                                     warn_th=config.SIG_CAPITULATION_WARN),
                  "gauge_capitulation.png", width=420, height=230)
        images["gauge_capitulation"] = "gauge_capitulation.png"

        spark1 = build_sparkline_figure(board_history_14d, "overheat_score", COL["orange"])
        if spark1 is not None:
            _save_fig(spark1, "spark_overheat.png", width=420, height=90)
            images["spark_overheat"] = "spark_overheat.png"
        spark2 = build_sparkline_figure(board_history_14d, "capitulation_score", COL["red"])
        if spark2 is not None:
            _save_fig(spark2, "spark_capitulation.png", width=420, height=90)
            images["spark_capitulation"] = "spark_capitulation.png"

        # --- ★2026-08-25追加(ユーザー依頼「シグナル発火状況(9指標)の各行にミニグラフを」):
        # 9指標それぞれの日次value推移スパークライン(2大メーターのスパークラインと同じ
        # build_sparkline_figure()を再利用・配色は各指標の現在の発火状態バッジと揃える・
        # value自体は指標ごとに単位がバラバラ[比率/z値/件数等]なので"/100"サフィックスは
        # 付けない)。テーブル行に収める用途のため2大メーターより小さいサイズで書き出す
        # (width=420→260・height=90→64。9枚追加時の生成時間への影響を抑える狙い)。
        # 有効点が2未満の指標(運用開始直後等)はbuild_sparkline_figure()がNoneを返し、
        # 画像を書き出さない=呼び手(_img_tag)側で「データ蓄積中です」表示になる
        # (既存のspark1/spark2と同じfail-soft設計)。 ---
        signal_cards = rec.get("signal_cards") or []
        if signal_cards:
            signal_history_14d = load_signal_cards_history_14d(rec)
            for i, c in enumerate(signal_cards):
                name = (c or {}).get("name")
                if not name:
                    continue
                line_color = _STATE_COLOR.get(c.get("state"), COL["muted"])
                spark_sig = build_sparkline_figure(signal_history_14d, name, line_color, ticksuffix="")
                if spark_sig is not None:
                    fname = f"spark_signal_{i}.png"
                    _save_fig(spark_sig, fname, width=260, height=64)
                    images[f"spark_signal_{i}"] = fname

        # --- レジーム帯 ---
        _save_fig(build_regime_band_figure(regime), "regime_band.png", width=460, height=100)
        images["regime_band"] = "regime_band.png"

        # --- 本日の価格推移(10分足) ---
        if price_pts:
            times = [p.get("time") for p in price_pts]
            eff = effective_buckets(_TODAY_TIME_BUCKETS, times)
            fig = build_candle_volume_figure(
                times, [p.get("price_open") for p in price_pts], [p.get("price_high") for p in price_pts],
                [p.get("price_low") for p in price_pts], [p.get("price_close") for p in price_pts],
                [p.get("price_volume") for p in price_pts],
                category_order=eff, x_range=[-0.5, len(eff) - 0.5])
            _save_fig(fig, "today_price.png")
            images["today_price"] = "today_price.png"

        # --- 板の買い/売り総計 ---
        board_fig = build_board_totals_figure(board_totals_series)
        if board_fig is not None:
            _save_fig(board_fig, "board_totals.png")
            images["board_totals"] = "board_totals.png"

        # --- 本日のセンチメント推移(10分毎・立会時間軸) ---
        if today_sent_pts:
            times = [p.get("time") for p in today_sent_pts]
            eff = effective_buckets(_TODAY_TIME_BUCKETS, [p.get("time") for p in price_pts])
            fig = build_sentiment_figure(
                times, [p.get("bull_ratio") for p in today_sent_pts],
                [p.get("bear_ratio") for p in today_sent_pts], None,
                [p.get("post_count") for p in today_sent_pts],
                category_order=eff, x_range=[-0.5, len(eff) - 0.5])
            _save_fig(fig, "today_sentiment.png")
            images["today_sentiment"] = "today_sentiment.png"

        # --- 過去24時間のセンチメント推移(ローリング窓) ---
        if sent_24h:
            times = [p.get("time") for p in sent_24h]
            fig = build_sentiment_figure(
                times, [p.get("bull_ratio") for p in sent_24h], [p.get("bear_ratio") for p in sent_24h],
                None, [p.get("post_count") for p in sent_24h])
            _save_fig(fig, "sentiment_24h.png")
            images["sentiment_24h"] = "sentiment_24h.png"

        # --- 過去14日間の価格推移(日足) ---
        if pss:
            date_labels = [mmdd(p.get("date")) for p in pss]
            fig = build_candle_volume_figure(
                date_labels, [p.get("price_open") for p in pss], [p.get("price_high") for p in pss],
                [p.get("price_low") for p in pss], [p.get("price_close") for p in pss],
                [p.get("price_volume") for p in pss])
            _save_fig(fig, "trend14_price.png")
            images["trend14_price"] = "trend14_price.png"

            bulls = [p.get("bull_ratio") for p in pss]
            bears = [p.get("bear_ratio") for p in pss]
            neutrals = [None if (b is None or r is None) else max(0.0, 1.0 - b - r)
                       for b, r in zip(bulls, bears)]
            fig = build_sentiment_figure(date_labels, bulls, bears, neutrals,
                                         [p.get("post_count") for p in pss])
            _save_fig(fig, "trend14_sentiment.png")
            images["trend14_sentiment"] = "trend14_sentiment.png"

    elif HAS_MATPLOTLIB:
        # matplotlibフォールバック(ゲージ/レジーム帯/スパークラインは簡易化のため省略し、
        # 主要な時系列チャート=当日価格・板総計・当日/24hセンチメント・14日価格/センチメント
        # のみを生成する。report_summaryにフォールバック使用の旨を明記する)。
        if price_pts:
            times = [p.get("time") for p in price_pts]
            _mpl_candle_volume(times, [p.get("price_open") for p in price_pts],
                              [p.get("price_high") for p in price_pts],
                              [p.get("price_low") for p in price_pts],
                              [p.get("price_close") for p in price_pts],
                              [p.get("price_volume") for p in price_pts],
                              os.path.join(IMG_DIR, "today_price.png"), "本日の価格推移")
            images["today_price"] = "today_price.png"

        if board_series_today:
            _mpl_lines([p.get("time") for p in board_series_today],
                      [("買い総計", [p.get("buy_total") for p in board_series_today], COL["red"]),
                       ("売り総計", [p.get("sell_total") for p in board_series_today], COL["blue"])],
                      os.path.join(IMG_DIR, "board_totals.png"), "板の買い・売り総計")
            images["board_totals"] = "board_totals.png"

        if today_sent_pts:
            _mpl_lines([p.get("time") for p in today_sent_pts],
                      [("強気", [p.get("bull_ratio") for p in today_sent_pts], COL["red"]),
                       ("弱気", [p.get("bear_ratio") for p in today_sent_pts], COL["blue"])],
                      os.path.join(IMG_DIR, "today_sentiment.png"), "本日のセンチメント推移", pct=True)
            images["today_sentiment"] = "today_sentiment.png"

        if sent_24h:
            _mpl_lines([p.get("time") for p in sent_24h],
                      [("強気", [p.get("bull_ratio") for p in sent_24h], COL["red"]),
                       ("弱気", [p.get("bear_ratio") for p in sent_24h], COL["blue"])],
                      os.path.join(IMG_DIR, "sentiment_24h.png"), "過去24時間のセンチメント推移", pct=True)
            images["sentiment_24h"] = "sentiment_24h.png"

        if pss:
            date_labels = [mmdd(p.get("date")) for p in pss]
            _mpl_candle_volume(date_labels, [p.get("price_open") for p in pss],
                              [p.get("price_high") for p in pss], [p.get("price_low") for p in pss],
                              [p.get("price_close") for p in pss], [p.get("price_volume") for p in pss],
                              os.path.join(IMG_DIR, "trend14_price.png"), "過去14日間の価格推移")
            images["trend14_price"] = "trend14_price.png"
            _mpl_lines(date_labels,
                      [("強気", [p.get("bull_ratio") for p in pss], COL["red"]),
                       ("弱気", [p.get("bear_ratio") for p in pss], COL["blue"])],
                      os.path.join(IMG_DIR, "trend14_sentiment.png"), "過去14日間のセンチメント推移", pct=True)
            images["trend14_sentiment"] = "trend14_sentiment.png"
    else:
        print("[WARN] plotly+kaleido / matplotlib どちらも使えないため画像生成をスキップします。")

    return images, extras


# ============================================================================
# HTML組み立て
# ============================================================================
def _img_tag(images, key, alt, caption=None):
    fname = images.get(key)
    if not fname:
        return f"<p class='muted'>{html.escape(alt)}: データ蓄積中です。</p>"
    cap = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
    return (f"<figure><img src='images/{fname}' alt='{html.escape(alt)}' loading='lazy'>"
           f"{cap}</figure>")


def _signal_list_html(rec, images):
    cards = rec.get("signal_cards") or []
    if not cards:
        return "<p class='muted'>シグナルデータ蓄積中です。</p>"
    rows = []
    for i, c in enumerate(cards):
        state = c.get("state", "OK")
        value = c.get("value")
        value_text = "—" if value is None else (f"{value:.2f}" if isinstance(value, float) else str(value))
        name = html.escape(c.get("name", ""))
        desc = html.escape(_SIGNAL_DESC.get(c.get("name", ""), ""))
        note = html.escape(str(c.get("note", "")))
        chip = state_badge_html(state, f"(現在値{value_text} / 閾値{c.get('threshold', '')})")
        # ★2026-08-25追加: 指標名ごとの推移スパークライン(images内のキー"spark_signal_{i}"は
        # generate_all_images()側でcardsと同じ並び順で書き出したもの・
        # _img_tag()が画像未生成時のfail-soft表示("データ蓄積中です。")も担う)。
        # ★2026-08-25修正(ユーザー依頼「グラフは行の右側にしましょう」): note/chip
        # (state_badge_html)より後ろ=行の最右列へ配置する(flex並びのためDOM順=表示順)。
        # ★2026-08-25再修正(ユーザー指摘「1番下の行が長すぎて、ずれてます」の
        # 追加是正): note列をflex:1 1 160pxへ変えるだけでは、noteが他行より長い
        # 行(「投稿サージ」「他銘柄混入率」等)でなお必要幅がsignal-rowの利用可能幅を
        # 超え、spark列だけが単独でflex-wrapにより次の行へ孤立して落ちる現象が
        # 実機で再現した。note/chip/sparkを'signal-meta'という1つの内側flexグループに
        # まとめることで、外側のsignal-rowから見た折り返しの単位を
        # 「name」と「note+chip+sparkのグループ全体」の2つだけにする
        # (=sparkだけが孤立して落ちることがなくなり、収まらない時はグループごと
        # 次の行へ整列して落ちる・内側のsignal-meta自体もflex-wrapを持つため
        # グループ内で足りない時はnote/chip/sparkがさらに折り返される)。
        spark_html = _img_tag(images, f"spark_signal_{i}", f"{c.get('name', '')}の推移")
        rows.append(
            f"<div class='signal-row'><div class='signal-name'>{name}"
            f"<div class='signal-desc'>{desc}</div></div>"
            f"<div class='signal-meta'>"
            f"<div class='signal-note'>{note}</div><div>{chip}</div>"
            f"<div class='signal-spark'>{spark_html}</div></div></div>")
    return "\n".join(rows)


def _signal_changes_html(rec):
    changes = rec.get("signal_state_changes") or []
    if not changes:
        return ""
    lines = "、".join(f"「{html.escape(c.get('name',''))}」{html.escape(str(c.get('from','')))}"
                    f"→{html.escape(str(c.get('to','')))}" for c in changes)
    compared = html.escape(str(changes[0].get("compared_date", "")))
    return (f"<div class='info-box'>📌 前回の取引日({compared})からの状態変化: {lines}</div>")


# ============================================================================
# 関連ニュース(★2026-08-27追加・ユーザー依頼「24時間以内のキオクシアに関係し
# そうなニュースの要約を公開ダッシュボードに」「検索頻度は10分毎・新ニュースが
# 出たら更新とリンクを」)。データ源はrec['news']
# (news_fetch.collect_news()→public_export.build_public_record()が組み立て済み・
# 画像版は1分毎の生成サイクルなので、latest.json由来のこのフィールドも既存の
# board_history_14d等と同じ鮮度=直近のcatchup(10分毎)実行時点の値になる)。
# ============================================================================
def _fmt_news_published(utc_iso):
    """news_fetch内部表現(UTC・'YYYY-MM-DDTHH:MM:SS')をJST表示('M/D HH:MM')へ
    変換する純関数。パース不能/Noneなら空文字(fail-soft)。"""
    if not utc_iso:
        return ""
    try:
        t = dt.datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%S") + dt.timedelta(hours=9)
        return f"{t.month}/{t.day} {t.strftime('%H:%M')}"
    except (ValueError, TypeError):
        return ""


def _news_summary_html(rec):
    """rec['news']自体が無い(RSS取得失敗・機能追加前のhistory等)場合、または
    直近24時間の該当記事が無い場合は、その旨だけを示す一言を返す(セクション見出し
    自体は常に出す=「機能が無効」と誤解されないようにする・fail-soft)。"""
    news = rec.get("news")
    if not news:
        return "<p class='muted'>ニュース取得は準備中です。</p>"
    items = news.get("items") or []
    summary_text = news.get("summary_text")
    if not items:
        return "<p class='muted'>直近24時間以内の関連ニュースは見つかりませんでした。</p>"
    parts = []
    if summary_text:
        parts.append(f"<div class='news-summary'>{html_escape_multiline(summary_text)}</div>")
    for it in items[:10]:
        title = html.escape(it.get("title") or "")
        link = html.escape(it.get("article_link") or "", quote=True)
        source = html.escape(it.get("source") or "出所不明")
        time_label = _fmt_news_published(it.get("published"))
        time_part = f"・{time_label}" if time_label else ""
        parts.append(
            f"<div class='news-row'><a href='{link}' target='_blank' "
            f"rel='noopener noreferrer'>{title}</a>"
            f"<span class='caption'>（{source}{time_part}）</span></div>")
    parts.append("<p class='caption'>"   # 2026-11-26: 免責文言はページ末尾に集約(ui5.DISCLAIMER_ITEMS)
                "各見出しから元記事(Google News経由)へ移動できます。</p>")
    return "\n".join(parts)


def _extended_hours_html(rec):
    eh = rec.get("extended_hours") or {}
    pts = eh.get("pts")
    adr = eh.get("adr")
    dex = eh.get("dex")
    if not pts and not adr and not dex:
        return ""
    parts = []
    if pts:
        chg = pts.get("change_pct")
        chg_text = fmt_pct(chg) if chg is not None else "—"
        parts.append(f"<div class='metric-card'><div class='metric-label'>PTS（{html.escape(str(pts.get('time','')))}）</div>"
                    f"<div class='metric-value'>{fmt_price(pts.get('price'))}</div>"
                    f"<div class='metric-delta' style='color:{chg_color(chg)}'>{chg_text}</div></div>")
    if adr:
        chg = adr.get("change_pct")
        chg_text = fmt_pct(chg) if chg is not None else "—"
        parts.append(f"<div class='metric-card'><div class='metric-label'>米国ADR（{html.escape(str(adr.get('time','')))}）</div>"
                    f"<div class='metric-value'>{fmt_price(adr.get('price_yen'))}</div>"
                    f"<div class='metric-delta' style='color:{chg_color(chg)}'>{chg_text}</div></div>")
    if dex:
        chg = dex.get("change_pct")
        chg_text = fmt_pct(chg) if chg is not None else "—"
        parts.append(f"<div class='metric-card'><div class='metric-label'>DEX（{html.escape(str(dex.get('time','')))}）</div>"
                    f"<div class='metric-value'>{fmt_price(dex.get('price'))}</div>"
                    f"<div class='metric-delta' style='color:{chg_color(chg)}'>{chg_text}</div></div>")
    return (f"<div class='metric-row'>{''.join(parts)}</div>"
           f"<p class='caption'>PTS=東証取引時間外の私設取引システム／ADR=米国預託証券／"
           f"DEX=分散型取引所(Hyperliquid)の24時間取引・円換算(東証終値比)。"
           f"翌営業日の値動きの参考情報です(それ自体が売買シグナルではありません)。</p>")


def _previous_deltas_html(rec):
    d = public_export.previous_deltas(rec)
    if not d.get("previous_generated_at"):
        return ""
    price = rec.get("price") or {}
    board = rec.get("board") or {}
    last = price.get("last")
    b = board.get("bull_ratio")
    r = board.get("bear_ratio")
    price_delta = f"{d['price_last']:+.0f}円" if d["price_last"] is not None else "—"
    bull_delta = f"{d['bull_ratio']*100:+.1f}pt" if d["bull_ratio"] is not None else "—"
    bear_delta = f"{d['bear_ratio']*100:+.1f}pt" if d["bear_ratio"] is not None else "—"
    return (
        "<div class='metric-row'>"
        f"<div class='metric-card'><div class='metric-label'>株価</div>"
        f"<div class='metric-value'>{fmt_price(last)}</div><div class='metric-delta'>{price_delta}</div></div>"
        f"<div class='metric-card'><div class='metric-label'>強気比率</div>"
        f"<div class='metric-value'>{fmt_ratio_pct(b)}</div><div class='metric-delta'>{bull_delta}</div></div>"
        f"<div class='metric-card'><div class='metric-label'>弱気比率</div>"
        f"<div class='metric-value'>{fmt_ratio_pct(r)}</div><div class='metric-delta'>{bear_delta}</div></div>"
        "</div>"
        f"<p class='caption'>前回集計({html.escape(str(d.get('previous_generated_at') or ''))})との比較</p>")


# ============================================================================
# 値動きの目安・海外要因パネル(★2026-09-06追加)
# ============================================================================
# 経緯: ユーザー指示「公開ダッシュボードのさらなる改善策を立案・全て反映」への
# 対応。いずれも公開レコード(rec)に既に含まれる集計値(price_sentiment_series・
# peer・execution_cost・ml_regime)だけを使い、新しい特別なデータ取得は行わない。
# 表示専用マッピングはpublic_insight.pyのプロンプト側と同じ日本語ラベルに揃える
# (プロジェクト間でクロスimportはしない・小さな表示名の重複として許容)。
_VR_REGIME_JA_STATIC = {"trending": "トレンド型", "reverting": "レンジ(往復)型",
                        "efficient": "方向感の乏しい型"}
_WIDTH_CLASS_JA_STATIC = {"wide": "普段より広め", "narrow": "普段より狭め", "typical": "普段並み"}
_US_PEER_NAME_JA_STATIC = {"micron": "マイクロン", "sandisk": "サンディスク",
                           "nvidia": "エヌビディア", "amd": "AMD",
                           "broadcom": "ブロードコム", "tsmc": "TSMC"}


def _support_levels_html(rec):
    """price_sentiment_series(既存の公開集計値)だけから、SD単位の参考価格水準・
    直近営業日高値からの乖離率を組み立てる(新しい統計的手法・予測ロジックの追加
    ではなく、既存データの見せ方を変えるだけ)。データ不足(日次リターン2点未満等)
    はfail-softで「データ蓄積中です」を表示する(既存パネルと同じ空表示の作法)。
    """
    price = rec.get("price") or {}
    last = price.get("last")
    pss = rec.get("price_sentiment_series") or []
    sd_pct = public_export.daily_return_sd_pct(pss)
    if last is None or sd_pct is None:
        return "<p class='muted'>データ蓄積中です(値幅の目安を出すには数営業日分の価格推移が必要です)。</p>"
    levels = public_export.sd_based_price_levels(last, sd_pct)
    rows = "".join(
        f"<tr><td>{lv['multiple']:.1f}SD（±{lv['pct']:.2f}%）</td>"
        f"<td>{fmt_price(lv['price_up'])}</td><td>{fmt_price(lv['price_down'])}</td></tr>"
        for lv in levels
    )
    dd = public_export.drawdown_from_recent_high_pct(last, pss)
    hi, _lo = public_export.recent_high_low(pss)
    dd_html = ""
    if dd is not None and hi:
        dd_html = (f"<p class='caption'>直近{len(pss)}日間の最高値"
                  f"（{html.escape(str(hi.get('date') or ''))}・{fmt_price(hi.get('price_close'))}）"
                  f"からの乖離: {fmt_pct(dd)}</p>")
    return (
        "<table style='width:100%;border-collapse:collapse;font-size:.92em'>"
        "<thead><tr><th style='text-align:left'>値幅の目安</th>"
        "<th style='text-align:left'>上側の水準</th><th style='text-align:left'>下側の水準</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>{dd_html}"
        f"<p class='caption'>SDは直近{len(pss)}日間の日次終値リターンの標準偏差"
        f"（{sd_pct:.2f}%/日）から算出した値幅の目安であり、到達を予測するものでは"
        "ありません。</p>"
    )


def _market_context_html(rec):
    """海外半導体ピア(peer)・本日の値動きの型(ml_regime)・執行コスト比較
    (execution_cost)を、AI考察の文章だけでなく視覚的なバッジ/カードとしても
    表示する(public_insight.pyのプロンプト設計と同じデータ源・同じ安全設計=
    判定/相関値/内部統計指標は一切表示しない)。いずれの項目もデータが無ければ
    個別にfail-softで非表示、全て無ければ「データ蓄積中です」を表示する。
    """
    cards = []
    peer = rec.get("peer")
    if peer and peer.get("us_d0_pct") is not None:
        name = _US_PEER_NAME_JA_STATIC.get(peer.get("us_key"), peer.get("us_ticker") or "海外ピア")
        cards.append(
            f"<div class='metric-card'><div class='metric-label'>{html.escape(name)}"
            f"（{html.escape(str(peer.get('us_ticker') or ''))}・前日）</div>"
            f"<div class='metric-value' style='color:{chg_color(peer.get('us_d0_pct'))}'>"
            f"{fmt_pct(peer.get('us_d0_pct'))}</div></div>")
    mlr = rec.get("ml_regime")
    if mlr and mlr.get("vr_regime"):
        vr_ja = _VR_REGIME_JA_STATIC.get(mlr.get("vr_regime"), mlr.get("vr_regime"))
        width_ja = _WIDTH_CLASS_JA_STATIC.get(mlr.get("width_class"), mlr.get("width_class") or "—")
        cards.append(
            f"<div class='metric-card'><div class='metric-label'>本日の値動きの型</div>"
            f"<div class='metric-value' style='font-size:1.05rem'>{html.escape(str(vr_ja))}</div>"
            f"<div class='metric-delta'>不確実性: {html.escape(str(width_ja))}</div></div>")
    ec = rec.get("execution_cost")
    price = rec.get("price") or {}
    chg = price.get("change_pct")
    if ec and ec.get("cost_bp") and chg is not None:
        move_bp = abs(chg) * 100.0
        ratio = move_bp / ec["cost_bp"]
        cards.append(
            f"<div class='metric-card'><div class='metric-label'>値動き ÷ 執行コスト</div>"
            f"<div class='metric-value'>約{ratio:.1f}倍</div>"
            f"<div class='metric-delta'>費用実測{ec['cost_bp']:.2f}bp</div></div>")
    if not cards:
        return "<p class='muted'>データ蓄積中です。</p>"
    return (f"<div class='metric-row'>{''.join(cards)}</div>"
           "<p class='caption'>海外ピアの値動き・本日の値動きの型はいずれも事実の記述であり、"
           "翌営業日の値動きを予測するものではありません。値動き÷執行コストは値幅の"
           "大きさの目安であり、実際に利益を確保できることを意味しません。</p>")


def _execution_cost_history_html(rec):
    """過去の日次値動き(price_sentiment_series由来)と、銘柄の往復実効費用
    (execution_cost.cost_bp・銘柄固有の実測固定値)から、日毎の「値動き÷執行コスト」
    倍率を一覧表示する(kaleido画像でなく軽量なHTML表・2026-09-06のkaleido多重起動
    是正を踏まえ、新規パネルで画像生成を増やさない設計判断)。データ不足時は
    fail-softで「データ蓄積中です」を表示する。

    ★2026-09-06追加(ユーザー指示「本日の値動き÷執行コストの推移は、市場が開いて
    いる日だけにしましょう」): filter_trading_day_rows()で土日の行を除外する
    (_is_trading_hours()と同じ「祝日カレンダーまでは見ない簡易判定」)。
    """
    ec = rec.get("execution_cost")
    pss = rec.get("price_sentiment_series") or []
    if not ec or not ec.get("cost_bp"):
        return "<p class='muted'>データ蓄積中です(執行コストの実測値がまだありません)。</p>"
    rets = public_export.filter_trading_day_rows(public_export.daily_return_pct_series(pss))
    if not rets:
        return "<p class='muted'>データ蓄積中です(日次の値動き推移が必要です)。</p>"
    cost_bp = ec["cost_bp"]
    rows = "".join(
        f"<tr><td>{html.escape(str(r['date']))}</td><td>{fmt_pct(r['pct'])}</td>"
        f"<td>約{(abs(r['pct']) * 100.0 / cost_bp):.1f}倍</td></tr>"
        for r in rets[-10:]
    )
    return (
        "<table style='width:100%;border-collapse:collapse;font-size:.9em'>"
        "<thead><tr><th style='text-align:left'>日付</th><th style='text-align:left'>前日比</th>"
        "<th style='text-align:left'>値動き÷執行コスト</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class='caption'>執行コスト(往復実効費用・実測{cost_bp:.2f}bp・"
        f"{html.escape(str(ec.get('days') or ''))}日分の{html.escape(str(ec.get('src') or ''))})"
        "は日によらず一定の値として比較しています。倍率が大きいほど値動きが執行"
        "コストに対して相対的に大きかったことを示す目安であり、実際に利益を"
        "確保できることを意味しません。</p>"
    )


def _comprehensive_stats_rows(path=None):
    """comprehensive_stats_latest.csv(正本=research/comprehensive_stats.py が書出す
    機械可読CSV)を読み取り専用で読み、数値列をfloatへ変換して返す。
    ファイル無し/空/壊れていればNone(呼び出し元は「準備中」表示ではなくセクション
    自体を無言で省略する)。★統計計算はここで再実装しない・CSVをそのまま表示するだけ。"""
    if path is None:
        path = os.path.join(config.RESEARCH_DIR, "comprehensive_stats_latest.csv")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    if not rows:
        return None
    for r in rows:
        for k in ("r", "diff_pt", "t"):
            # ★2026-09-07 selftest追加時に発見・是正: r[k]だと列名が想定と異なる
            # (壊れたCSV・正本側の列変更等)場合にKeyErrorでクラッシュしていた。
            # r.get(k)でNone扱いにしてから変換し、fail-soft(呼び出し元は
            # 「値なし」として—表示するだけ)を保つ。
            try:
                r[k] = float(r.get(k))
            except (TypeError, ValueError):
                r[k] = None
    return rows


def _comprehensive_stats_html(path=None):
    """★2026-11-25改修(ユーザー承認・『統計評価が難解』): 冒頭に『このタブで分かること』・指標ごとの結論1行・
    用語の読み方凡例・注意を出し、従来の詳細表(r・差・t値マトリクス+注記)は <details> に折りたたむ(情報は削らない)。
    数値は変えない(表示と説明のみ)。CSVが無ければ空文字(セクションごと省略)。"""
    detail = _comprehensive_stats_detail_html(path)
    if not detail:
        return ""
    rows = _comprehensive_stats_rows(path) or []
    try:
        import stats_explain as SE
        vs = SE.verdicts(rows)
        tone = {"green": COL["green"], "yellow": COL["yellow"], "muted": COL["muted"]}
        intro = ("<div class='panel' style='margin:0 0 10px'><b>このタブで分かること</b><ul style='margin:4px 0;padding-left:1.2em'>"
                 + "".join("<li>" + html.escape(x) + "</li>" for x in SE.INTRO) + "</ul></div>")
        cards = ("<p><b>指標ごとの結論</b>（" + html.escape(SE.summary_line(vs)) + "）</p>"
                 + "".join("<div style='border-left:4px solid " + tone[v["tone"]] + ";padding:4px 10px;margin:4px 0'><b>"
                           + html.escape(str(v["metric"])) + "</b>　" + html.escape(v["line"]) + "</div>" for v in vs))
        gloss = ("<details style='margin:10px 0'><summary><b>📖 用語の意味と目安の読み方（t値・相関係数・p値・サンプル数）</b></summary>"
                 "<table style='width:100%;border-collapse:collapse;font-size:.9em;margin-top:6px'>"
                 "<thead><tr><th style='text-align:left'>用語</th><th style='text-align:left'>意味</th><th style='text-align:left'>目安の読み方</th></tr></thead><tbody>"
                 + "".join("<tr><td style='vertical-align:top'><b>" + html.escape(a) + "</b></td><td>" + html.escape(b) + "</td><td>" + html.escape(c) + "</td></tr>"
                           for a, b, c in SE.GLOSSARY)
                 + "</tbody></table><p class='caption'>結論の判定基準: ✅=件数30以上・rとグループ差の向きが一致・rの絶対値0.3以上・tの絶対値3以上／"
                   "➖=全期間でrの絶対値0.1未満かつtの絶対値2未満／🟡=それ以外。</p></details>")
        warn = ("<p class='caption' style='border:1px solid " + COL["yellow"] + ";padding:6px 10px;border-radius:6px'>⚠️ ご注意: "
                + html.escape(" ".join(SE.NOTES)) + "</p>")
    except Exception:
        return detail
    return (intro + cards + gloss + warn
            + "<details style='margin:10px 0'><summary><b>📋 詳細表（相関r・高群−低群の差・t値）を開く</b></summary>" + detail + "</details>")


def _comprehensive_stats_detail_html(path=None):
    """掲示板センチメント5指標(売り煽り度/買い煽り度/悲鳴・投げ売り度/強気比率/投稿数)
    ×3ホライズン(翌1/3/5日)の相関r・中央値分割の高群-低群差(pt)・t値マトリクスHTML。
    正本(research/comprehensive_stats.py)が書き出すCSVを読むだけ・再計算しない。
    CSVが無ければ空文字(呼び出し元セクションごと無言で省略)。"""
    rows = _comprehensive_stats_rows(path)
    if not rows:
        return ""
    metrics, horizons = [], []
    for r in rows:
        if r["metric_name"] not in metrics:
            metrics.append(r["metric_name"])
        if r["horizon_name"] not in horizons:
            horizons.append(r["horizon_name"])
    by_key = {(r["metric_name"], r["horizon_name"]): r for r in rows}
    thead = "<th style='text-align:left'>指標</th>" + "".join(
        f"<th style='text-align:center'>{html.escape(h)}</th>" for h in horizons)
    body_rows = []
    for m in metrics:
        cells = []
        for h in horizons:
            c = by_key.get((m, h))
            if not c or c.get("r") is None:
                cells.append("<td style='text-align:center'>—</td>")
                continue
            t = c.get("t")
            r = c["r"]
            # ★2026-09-07追加(ユーザー指示「ポジティブ:緑、ネガティブ:赤で色付け」)。
            # dashboard.py(内部版)と同じロジック=r(相関係数)の符号で色を決める。
            # |t|>=2は従来どおり太字で強調するが、色そのものは常に符号で塗る。
            tone = COL["green"] if r > 0 else (COL["red"] if r < 0 else COL["muted"])
            strong = t is not None and abs(t) >= 2
            style = f"font-weight:{'700' if strong else '500'};color:{tone}"
            diff_txt = f"{c['diff_pt']:+.2f}pt" if c.get("diff_pt") is not None else "—"
            t_txt = f"t={t:.2f}" if t is not None else "t=—"
            cells.append(f"<td style='text-align:center;{style}'>r={c['r']:.2f}<br>"
                        f"{diff_txt} / {t_txt}</td>")
        body_rows.append(f"<tr><td>{html.escape(m)}</td>" + "".join(cells) + "</tr>")
    period = html.escape(str(rows[0].get("data_period", "—")))
    calc = html.escape(str(rows[0].get("calc_date", "—")))
    return (
        "<table style='width:100%;border-collapse:collapse;font-size:.9em'>"
        f"<thead><tr>{thead}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
        "<p class='caption'>売り煽り度/買い煽り度/悲鳴・投げ売り度/強気比率/投稿数(出来高)の"
        "各指標と、翌1日/3日/5日リターンの相関係数r・中央値分割(高群-低群)の差(pt)・t値。"
        "<b>探索的分析であり確定した予測ではありません</b>。指標×ホライズンの多重比較"
        "(計15セル)であり、|t|が大きいセルも偶然の可能性を排除できません。"
        "色は相関係数rの符号(高群ほど翌日リターンが高い=緑・低い=赤)、"
        f"|t|≥2程度を目安に太字で強調していますが、統計的に確定した優位性では"
        f"ありません。データ期間: {period} / 算出日: {calc}。"
        "正本の計算ロジック: research/comprehensive_stats.py。</p>"
    )


def _commentary_asof_label(generated_at):
    """★2026-10-13: AI考察の表示に『MM-DD HH:MM 時点』を付ける(夜間・休場日は前回の考察を引き継ぐため、
    いつの時点の考察かを明示する)。失敗しても例外を出さない。"""
    try:
        t = dt.datetime.fromisoformat(str(generated_at)[:19])
        return t.strftime("%m-%d %H:%M") + " 時点の考察"
    except Exception:
        return "考察の時点: 不明"


def render_html(rec, images, extras, generated_at_str):
    symbol = html.escape(str(rec.get("symbol") or config.SYMBOL))
    name = html.escape(str(rec.get("company_name") or ""))
    price = rec.get("price") or {}
    last = price.get("last")
    chg = price.get("change_pct")
    gen_at = html.escape(str(rec.get("generated_at") or "不明"))
    board = rec.get("board") or {}

    ac = rec.get("ai_commentary") or {}
    ac_html = (f"<div class='ai-box'>{html_escape_multiline(ac.get('text'))}</div>"
              f"<p class='caption'>{html.escape(_commentary_asof_label(ac.get('generated_at')))}"
              f"（生成時刻: {html.escape(str(ac.get('generated_at', '不明')))}）</p>"
              if ac.get("text") else "<p class='muted'>AI考察は準備中です。</p>")

    _cs_table_html = _comprehensive_stats_html()
    comprehensive_stats_section_html = (
        "<div class=\"panel\"><h3>掲示板センチメント×株価 統計評価（研究）</h3>"
        f"{_cs_table_html}</div>" if _cs_table_html else ""
    )

    disclaimer = html.escape(str(rec.get("disclaimer") or public_export.DISCLAIMER))

    # ★2026-08-24追加(Streamlit版public_dashboard.py._header()とのパリティ確保):
    # データの更新が取引時間中に止まっている可能性がある場合の警告と、累計閲覧数
    # バッジ。どちらもfail-soft(条件に合わなければ空文字列=非表示)。
    now_jst = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9))
    stale_html = staleness_warning_html(
        rec.get("generated_at"), now_jst, config.LIVE_PRICE_STALE_MINUTES,
        public_export._is_trading_hours(now=now_jst))
    # ★2026-10-13追加(ユーザー指示「AI考察の予定を、ダッシュボードとワールドモニターに書く」):
    # 時刻・間の扱いは ai_insight_schedule_config.json から読む(手書き固定値にしない)。
    # 失敗しても空文字列(=非表示)で本体を止めない(fail-soft)。
    try:
        import ai_insight_schedule as _ais
        _ais_b = _ais.build_block(
            now_jst, _ais.parse_generated_at((rec.get("ai_commentary") or {}).get("generated_at")))
        ai_schedule_html = (
            "<div class='panel' id='ai-schedule'><h3>🗓 " + html.escape(_ais_b["heading"]) + "</h3><ul>"
            + "".join("<li>" + html.escape(x) + "</li>" for x in _ais_b["lines"]) + "</ul></div>")
    except Exception:
        ai_schedule_html = ""
    # ★2026-10-13追加(ユーザー決定『2段構成＝数値要約＋時点名つきClaude考察』): 数値要約(機械的テンプレート・LM不使用)。fail-soft。
    try:
        import numeric_summary as _ns
        _ns_b = _ns.build_numeric_summary(rec)
        _rt = _ui5.recent_table_html(_ui5.recent_rows(rec))   # 2026-11-26: 直近変化は表で表示(表がある時は文の行を重複させない)
        numeric_summary_html = (
            "<div class='panel' id='numeric-summary'><h3>📊 " + html.escape(_ns_b["heading"]) + "｜" + html.escape(_ns_b["asof"]) + " 更新</h3><ul>"
            + "".join("<li>" + html.escape(x) + "</li>" for x in _ns_b["lines"]
                      if not (_rt and (x.startswith("直近変化:") or x.startswith("立会時間外の市場") or x.startswith("夜間PTS・米国ADR・DEX")))) + "</ul>" + _rt
            + "<p class='caption'>" + html.escape(_ns_b["note"]) + "</p></div>")
    except Exception:
        numeric_summary_html = ""
    # ★2026-10-16追加(既定では出ない): rec['screen_extras'](板の増減・tick価格まわり・見立ての機械採点。moomoo由来は含まない)がある時だけ表示。
    try:
        import screen_flow_summary as _sf
        _xl = _sf.public_extras_lines(rec.get("screen_extras"), with_disclaimer=False)
        screen_extras_html = (
            "<div class='panel' id='screen-extras'><h3>🧭 板・価格まわり・見立ての採点（参考）</h3><ul>"
            + "".join("<li>" + html.escape(x) + "</li>" for x in _xl) + "</ul>"
            + "<p class='caption'>板情報・約定ティック(遅延あり)から作成。売買代金は試算(VWAP×出来高)。</p></div>") if _xl else ""
    except Exception:
        screen_extras_html = ""
    # ★2026-11-25追加(ユーザー承認・Gemini助言⑥): セリクラ成立チェック(3点ランプ)。screen_extras['selloff']がある時だけ。
    try:
        import selloff_check as _sc
        _sel = (rec.get("screen_extras") or {}).get("selloff")
        if _sel:
            _lp = _sc.lamps(_sel, rec.get("signal_cards"))
            _mk = {"on": "🟢", "off": "⚪", "na": "－"}
            _vw = _lp.get("vwap") or {}
            selloff_check_html = (
                "<div id='selloff-check' style='margin-top:8px'><b>🚦 " + html.escape(_lp["title"]) + "</b>"
                + ("<span class='caption'>（" + html.escape(_lp["asof_label"]) + "）</span>" if (_lp.get("asof_label") and not _lp.get("final")) else "")
                + "<ul style='margin:4px 0;padding-left:1.2em'>"
                + "".join("<li>" + _mk[i["state"]] + " " + html.escape(i["label"]) + "</li>" for i in _lp["items"]) + "</ul>"
                + ("<div class='caption'>別表示(3点に数えない) VWAP回復: " + ("はい" if _vw.get("recovered") else "いいえ")
                   + "（" + html.escape(_vw.get("detail", "")) + "）</div>" if _vw.get("recovered") is not None else "")
                + "<div class='caption'>" + html.escape(_lp["caution"]) + " 板・約定ティックは遅延あり。</div></div>")
        else:
            selloff_check_html = ""
    except Exception:
        selloff_check_html = ""
    visit_count = (public_export.record_visit(config.PUBLIC_VISIT_COUNTER_URL, action="read")
                  if config.PUBLIC_VISIT_COUNTER_URL else None)
    visit_html = format_visit_count_html(visit_count)
    # ★2026-08-27追加(ユーザー依頼「ダッシュボードに、いいねボタンを足せますか」):
    # like_counter版の同じGAS Web Appから現在値だけ読む(action=read相当のlike_read。
    # このサーバー側取得はJS無効時のフォールバック表示用で、実際の加算は
    # like_button_js()がブラウザ側でクリック時のみ行う=visit_countと同じ設計)。
    like_count = (public_export.record_visit(config.PUBLIC_VISIT_COUNTER_URL, action="like_read")
                 if config.PUBLIC_VISIT_COUNTER_URL else None)
    like_html = format_like_button_html(like_count)

    # ★2026-08-24追加(Streamlit版public_dashboard.py._intraday_today_charts()との
    # パリティ確保・当初のユーザー指摘そのもの): 本日の価格推移の見出し直下に
    # 高値/安値キャプション。extras["day_high_low"]はgenerate_all_images()側で
    # public_export.intraday_today_high_low()を使い既に計算済み(捏造しない=
    # データが無ければ何も表示しない)。
    _day_high, _day_low = (extras or {}).get("day_high_low", (None, None))
    high_low_html = (f"<p class='caption'>本日の高値 {_day_high:,.0f}円 ／ 安値 {_day_low:,.0f}円</p>"
                     if _day_high is not None and _day_low is not None else "")

    css = f"""
    {_SWITCH_BAR_CSS}
    {_ui5.CSS}
    :root {{
      --bg:{COL['bg']}; --panel:{COL['panel']}; --border:{COL['border']}; --muted:{COL['muted']};
      --text:{COL['text']}; --orange:{COL['orange']}; --red:{COL['red']}; --green:{COL['green']};
      --blue:{COL['blue']};
    }}
    * {{ box-sizing:border-box; }}
    body {{ background:var(--bg); color:var(--text); font-family:"Hiragino Sans","Yu Gothic",
      "Segoe UI",sans-serif; margin:0; padding:0 0 3rem; }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:1.5rem 1rem; }}
    h1 {{ font-size:1.3rem; margin:0 0 1rem; }}
    h2 {{ font-size:1.05rem; margin:1.6rem 0 .6rem; border-left:5px solid var(--orange); padding-left:8px; }}
    h3 {{ font-size:.95rem; margin:1rem 0 .4rem; color:var(--text); }}
    .header {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }}
    .symbol {{ font-size:1.6em; font-weight:700; }}
    .company {{ font-size:1.05em; color:var(--muted); }}
    .price {{ font-size:1.5em; font-weight:700; }}
    .chg {{ font-size:1.15em; font-weight:600; }}
    .caption {{ font-size:.82em; color:var(--muted); margin:.3rem 0; }}
    .muted {{ color:var(--muted); font-size:.9em; }}
    .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
      padding:14px 16px; margin-bottom:14px; box-shadow:0 1px 3px rgba(15,23,42,.06); }}
    .grid3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
    .grid2 {{ display:grid; grid-template-columns:repeat(2,1fr); gap:14px; }}
    @media (max-width:820px) {{ .grid3, .grid2 {{ grid-template-columns:1fr; }} }}
    figure {{ margin:0; }}
    figure img {{ width:100%; height:auto; display:block; border-radius:8px; }}
    figcaption {{ font-size:.8em; color:var(--muted); margin-top:4px; }}
    .signal-row {{ display:flex; justify-content:space-between; align-items:center;
      border-bottom:1px solid var(--border); padding:8px 2px; gap:10px; flex-wrap:wrap; }}
    .signal-name {{ flex:1 1 40%; }}
    .signal-meta {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap;
      justify-content:flex-end; flex:1 1 300px; }}
    .signal-desc {{ color:var(--muted); font-size:.8em; }}
    .signal-spark {{ flex:0 0 140px; min-width:110px; }}
    .signal-spark figure img {{ border-radius:4px; }}
    .signal-spark p.muted {{ font-size:.78em; margin:0; }}
    .news-summary {{ margin-bottom:.6rem; }}
    .news-row {{ padding:4px 2px; border-bottom:1px solid var(--border); font-size:.92em; }}
    .news-row a {{ color:var(--text); text-decoration:none; }}
    .news-row a:hover {{ text-decoration:underline; }}
    .news-row .caption {{ display:inline; margin-left:.3em; }}
    .channel-bar {{ margin-bottom:10px; }}
    .channel-bar a {{ display:inline-flex; align-items:center; gap:8px; color:var(--text);
      text-decoration:none; font-size:.92em; }}
    .channel-bar a:hover {{ text-decoration:underline; }}
    .channel-avatar {{ width:32px; height:32px; border-radius:50%; object-fit:cover;
      display:block; }}
    .cross-link {{ display:inline-block; margin-bottom:10px; padding:4px 10px;
      border:1px solid var(--border); border-radius:6px; color:var(--text);
      text-decoration:none; font-size:.85em; font-weight:700; }}
    .cross-link:hover {{ text-decoration:underline; }}
    .sister-card {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap;
      margin:0 0 12px; padding:10px 14px; border:2px solid #4c9aff; border-radius:10px;
      background:rgba(76,154,255,.10); color:var(--text); text-decoration:none; }}
    .sister-card:hover {{ background:rgba(76,154,255,.20); }}
    .sister-card .sc-main {{ flex:1 1 260px; min-width:0; }}
    .sister-card .sc-title {{ font-weight:700; font-size:.98em; }}
    .sister-card .sc-desc {{ font-size:.82em; opacity:.85; margin-top:2px; line-height:1.5; }}
    .sister-card .sc-btn {{ flex:0 0 auto; padding:7px 14px; border-radius:6px;
      background:#4c9aff; color:#fff; font-weight:700; font-size:.88em; white-space:nowrap; }}
    # ★2026-08-25修正(ユーザー指摘「1番下の行が長すぎて、ずれてます」): noteの
    # テキストが他行より長い場合(例:「投稿サージ」の「量→ボラ予測が文献の主結果」)、
    # flex:0 0 auto(伸縮しない・中身のサイズをそのまま専有)のままだと行全体の
    # 必要幅が signal-row の幅を超え、flex-wrap:wrap により後続のspark/chip列が
    # 次の行へ押し出されていた。flex:1 1 160pxへ変更しnoteをshrink可能にすることで、
    # 長いnoteはnote自身の内側で折り返す(=spark/chip列の位置は行内で保たれる)。
    # min-width:0はflexアイテムの既定min-width:auto(中身の最小幅=改行不可)を
    # 打ち消すために必須(これが無いと縮小指定自体が効かず同じ症状が再発する)。
    .signal-note {{ color:var(--muted); font-size:.88em; flex:1 1 160px; min-width:0; }}
    .info-box {{ background:#EAF2FB; border:1px solid #C9DDF3; border-radius:8px;
      padding:10px 12px; margin:.6rem 0; font-size:.9em; }}
    .ai-box {{ background:var(--panel); border:1px solid var(--border); border-radius:8px;
      padding:14px; line-height:1.7; }}
    .metric-row {{ display:flex; gap:14px; flex-wrap:wrap; margin:.6rem 0; }}
    .metric-card {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
      padding:10px 14px; flex:1 1 160px; box-shadow:0 2px 8px rgba(15,23,42,.08); }}
    .metric-label {{ font-size:.78rem; color:var(--muted); }}
    .metric-value {{ font-variant-numeric:tabular-nums; font-weight:700; font-size:1.3rem; }}
    .metric-delta {{ font-size:.85rem; color:var(--muted); }}
    .calib {{ background:#FBF3E0; color:var(--text); border:1px solid #F0E4C8;
      border-left:4px solid var(--orange); border-radius:8px; padding:9px 13px;
      font-size:.9em; margin:.3rem 0; }}
    footer {{ text-align:center; color:var(--muted); font-size:.78em; margin-top:2rem; }}
    /* ★2026-09-08追加(ユーザー指示「AIセクターワールドモニターと同様に、各表示を
       グルーピングしてタブで選択できるように」)。モニタ(ai_sector_monitor)の
       static/css/style.cssのタブバーと同じ発想(素のCSSクラス切替・外部ライブラリ
       不使用)をこのダッシュボードの色トークンで再現する。 */
    .tab-bar {{ display:flex; gap:6px; padding:2px 0 12px; overflow-x:auto;
      -webkit-overflow-scrolling:touch; }}
    .tab-btn {{ flex:0 0 auto; padding:8px 14px; border-radius:8px;
      border:1px solid var(--border); background:var(--panel);
      color:var(--muted); font-size:.88em; font-weight:700; cursor:pointer;
      white-space:nowrap; }}
    .tab-btn.active {{ background:var(--orange); color:#fff; border-color:var(--orange); }}
    .tab-hidden {{ display:none !important; }}
    /* ★2026-09-14追加(おにやGO④・375pxモバイル実機確認=エンジニア): タブバーが
       画面幅に収まらず3〜4番目のタブが横スクロール外に隠れ、続きがあることに
       気づきにくかった。右端に背景色へのグラデーションフェードを重ね「まだ続きが
       ある」ことを視覚的に示す(タブ数を減らす・横スクロール自体を廃止する等の
       構成変更はせず、CSSのみの軽微な追加に留める)。 */
    .tab-bar-wrap {{ position:relative; }}
    .tab-bar-wrap::after {{ content:""; position:absolute; top:0; right:0; bottom:12px;
      width:26px; pointer-events:none;
      background:linear-gradient(to right, rgba(0,0,0,0), var(--bg)); }}
    """

    # ★2026-09-08全面再構成(ユーザー指示「AI考察はトップに移動」「AIセクター
    # ワールドモニターと同様に、各表示をグルーピングしてタブで選択できるように」):
    # AI考察がタブ外・最上部固定になり、残りが4タブへグルーピングされた新レイアウト
    # に合わせて全面的に書き直す(旧①〜⑨の番号体系は廃止・タブ構成に沿った見出しへ・
    # Streamlit版public_dashboard.py._reading_guide()と同じ内容)。
    guide = f"""
    <details class="panel">
      <summary style="cursor:pointer;font-weight:600">📖 このダッシュボードの読み方（初めての方向け）</summary>
      <div style="margin-top:10px;font-size:.92em;line-height:1.7">
        <p><b>① 株価ヘッダー</b> — 現在値・騰落率と最終更新時刻です。裏側のデータは
        1分間隔のタスクで自動生成され、このページ自体も{STATIC_DASHBOARD_AUTOREFRESH_SEC}秒毎に
        ブラウザが自動的に再読み込みして最新のスナップショットを表示します（手動更新は不要です）。
        取引時間中に更新が止まっている可能性がある場合は「⚠️データの更新が…」という注意書きが
        表示されます。</p>
        <p><b>② タブ「🤖 AI考察」（先頭・最初に開くタブ）</b> — 他のタブに表示されている集計値
        (株価・掲示板の強弱比率・9指標の発火状況・ボラレジーム・PTS/ADR等)だけをもとにAIが
        生成した文章です。個別の投稿内容やユーザー名は一切含まれません。</p>
        <p><b>③ タブ「🔥 メーター・シグナル」</b> — 灼熱メーター／阿鼻叫喚メーターは掲示板の
        投稿内容から算出した「過熱度」「セリングクライマックス度」の合成指標(売買のシグナル
        ではありません)。ボラ・レジーム帯は現在の値動きの荒さが「平穏〜急変」のどの水準にあるか
        の目安で、データ蓄積初期は「較正中」と表示されます(異常ではなく閾値を学習している途中)。
        PTS・米国ADR(表示される場合のみ)は東証の取引時間外の値動きの参考情報です。シグナル発火
        状況(9指標)は投稿の偏り・語彙・投稿量などを統計的にチェックした一覧で、🟢OK=平常範囲内
        ／🟠警戒=やや偏りが大きい／🔴発火=閾値超過、を示す記述的なラベルです。</p>
        <p><b>④ タブ「📅 本日の推移」</b> — 本日の推移(価格推移10分足・板の買い/売り総計60秒
        平均・本日のセンチメント推移)を東証立会時間の同じ横軸で並べています。価格推移の見出し下
        には本日の高値・安値も表示します。過去24時間のセンチメント推移(暦日をまたぐローリング窓)も
        このタブにあります。</p>
        <p><b>⑤ タブ「📐 値動きの目安」</b> — 直近の日次終値リターンの標準偏差(SD)
        から算出した値幅の目安(支持線・抵抗線)と、日次の「値動き÷執行コスト」倍率の推移を表示します。
        いずれも到達を予測するものではなく、値幅の大きさの目安です。</p>
        <p><b>⑥ タブ「🌐 市場・ニュース」</b> — 海外半導体ピア・当日の値動きの型・執行コスト比較
        といった市場コンテキストと、AI半導体に関する関連ニュース(直近24時間)を表示します。</p>
        <p><b>⑦ タブ「📈 過去14日」</b> — 過去14日間の推移(価格推移の日足ローソク足・出来高、
        センチメント推移の強気/弱気/中立の3本の比率と投稿量)を表示します。</p>
        <p><b>⑧ タブ「📊 統計評価」</b> — 掲示板センチメント5指標と翌1日/3日/5日リターンの
        相関係数r・中央値分割の差(pt)・t値のマトリクスです。緑=相関がプラス・赤=マイナス、
        太字は|t|≥2程度の目安です。探索的分析であり確定した予測ではありません。</p>
        <p>件数・数量系のグラフ(投稿量・板の買い/売り総計等)に統計的な外れ値が自動検出された場合は、
        グラフ直下に「⚠️自動検出された外れ値」として注記が表示されます(値そのものは書き換えません)。</p>
        <p>本ダッシュボードは研究・エンタメ用途の情報提供であり、投資助言ではありません。</p>
      </div>
    </details>
    """

    body = f"""
    <div class="wrap">
      <!-- 2026-10-02(ユーザー依頼): 上部のYouTubeリンクとアバター画像を削除。 -->
      <!-- 2026-09-06追加(ユーザー依頼「モニターとダッシュボードの相互リンクを入れて」):
           姉妹プロジェクト「AIセクター ワールドモニター」(ai_sector_monitor・キオクシア285A
           売買判断支援)の公開静的サイトへの相互リンク。あちら側にも本サイトへの逆リンクを
           追加済み(ai_sector_monitor/templates/index.html・static_export.py)。 -->
      <!-- 2026-11-23(ユーザー指示「姉妹サイトがあまり見られていない・誘導を改善」): 小さなリンクをカード型バナーに変更し、
           見て得られる内容(事実のみ)と押せるボタンを明示。AI考察タブ末尾にも同じ誘導を再掲。 -->
      <!-- 2026-11-26(ユーザー承認): 最上部の大きな姉妹サイトバナーは削除し、タイトル直下のスイッチバーに一本化 -->
      <h1>📊 掲示板投稿の詳細分析による投資情報（静的版）</h1>
      {_switch_bar_html()}
      {guide}
      <div class="panel header">
        <span class="symbol">{symbol}</span>
        <span class="company">{name}</span>
        <span class="price">{fmt_price(last)}</span>
        <span class="chg" style="color:{chg_color(chg)}">{fmt_pct(chg)}</span>{("<span class='caption'> " + html.escape(str(price.get("change_basis"))) + "</span>") if price.get("change_basis") else ""}
      </div>
      <p class="caption">更新時刻: {gen_at}（データは1分間隔で自動生成され、このページも
      {STATIC_DASHBOARD_AUTOREFRESH_SEC}秒毎に自動再読み込みされます）</p>
      {stale_html}
      {visit_html}
      {like_html}

      <!-- ★2026-11-23(ユーザー指示「おにやの公開ダッシュボードもタブの再整理・分割」): AI考察を独立タブ(先頭・既定)にし、
           概況を「メーター・シグナル」「市場・ニュース」に、推移を「本日の推移」「過去14日」に分け、見る順に並べた。
           各パネルの計算・描画は変更しない(表示の出し分けのみ)。 -->
      <div class="tab-bar-wrap">
        <nav class="tab-bar" id="tabBar" role="tablist" aria-label="表示グループ切替">
          <button class="tab-btn active" data-tab="aiinsight" role="tab" aria-selected="true">🤖 AI考察</button>
          <button class="tab-btn" data-tab="overview" role="tab" aria-selected="false">🔥 メーター・シグナル</button>
          <button class="tab-btn" data-tab="today" role="tab" aria-selected="false">📅 本日の推移</button>
          <button class="tab-btn" data-tab="levels" role="tab" aria-selected="false">📐 値動きの目安</button>
          <button class="tab-btn" data-tab="market" role="tab" aria-selected="false">🌐 市場・ニュース</button>
          <button class="tab-btn" data-tab="trend" role="tab" aria-selected="false">📈 過去14日</button>
          <button class="tab-btn" data-tab="stats" role="tab" aria-selected="false">📊 統計評価</button>
        </nav>
      </div>

      <section data-tab-panel="aiinsight">
        <h2>🤖 AI考察</h2>
        {_ui5.summary3_html(rec)}
        {ai_schedule_html}
        {numeric_summary_html}
        {screen_extras_html}
        {_previous_deltas_html(rec)}
        {ac_html}
        <a class="sister-card" href="https://ethanhuntstock.github.io/ai-sector-monitor/" target="_blank" rel="noopener noreferrer" style="margin-top:12px">
          <span class="sc-main">
            <div class="sc-title">次は「285Aは今どの値位置にいるか」を見る</div>
            <div class="sc-desc">掲示板の空気に加えて、支持線・抵抗線、米国・韓国ピア、信用倍率を姉妹サイトで確認できます(営業日に更新)</div>
          </span>
          <span class="sc-btn">ワールドモニターへ ▶</span>
        </a>
      </section>

      <section data-tab-panel="overview" class="tab-hidden">
        <h2>🔥😱 2大メーター・ボラレジーム</h2>
        <div class="grid3">
          <div class="panel">
            {_img_tag(images, "gauge_overheat", "灼熱メーター(過熱)")}
            {_img_tag(images, "spark_overheat", "灼熱メーター推移")}
          </div>
          <div class="panel">
            {_img_tag(images, "gauge_capitulation", "阿鼻叫喚メーター(セリクラ)")}
            {_img_tag(images, "spark_capitulation", "阿鼻叫喚メーター推移")}
            {selloff_check_html}
          </div>
          <div class="panel">
            <h3>ボラ・レジーム帯</h3>
            {_img_tag(images, "regime_band", "ボラ・レジーム帯")}
            {regime_caption_html(rec.get('regime'))}
            {_extended_hours_html(rec)}
          </div>
        </div>

        <h2>🎯 シグナル発火状況（9指標）</h2>
        <details class="fold"><summary>📖 この一覧の読み方（用語）</summary>
        <p class="caption">掲示板の投稿を集計した9つの指標が、統計的なしきい値を超えたかどうかを示す一覧です。
        「発火」は売買のシグナルではなく、「統計的に見て平常時より偏りが大きい状態」を示す記述的な警告ラベルです。</p></details>
        {_signal_changes_html(rec)}
        <div class="panel">{_signal_list_html(rec, images)}</div>
        <p class="caption">掲示板は方向よりボラを予測する傾向が文献で報告されています(Antweiler &amp; Frank, 2004)。</p>
      </section>

      <section data-tab-panel="today" class="tab-hidden">
        <h2>📅 本日の推移（イントラデイ）</h2>
        <h3>本日の価格推移（10分足ローソク足・出来高）</h3>
        {high_low_html}
        <div class="panel">{_img_tag(images, "today_price", "本日の価格推移")}</div>
        <h3>板の買い・売り総計（成行込み全価格帯・60秒平均）</h3>
        <details class="fold"><summary>📖 板の買い・売り総計の読み方</summary>
        <p class="caption">表示10本の気配だけでなく、外側の気配(OVER/UNDER)と成行注文も含めた板全体の数量合計です。
        値は直近60秒間の平均。需給の偏りを直接示す指標ではありません。</p></details>
        <div class="panel">{_img_tag(images, "board_totals", "板の買い・売り総計")}</div>
        {extras.get("outliers", {}).get("board_totals_buy", "")}
        {extras.get("outliers", {}).get("board_totals_sell", "")}
        <h3>本日のセンチメント推移（10分毎・強気/弱気比率・投稿量）</h3>
        <div class="panel">{_img_tag(images, "today_sentiment", "本日のセンチメント推移")}</div>
        {extras.get("outliers", {}).get("today_sentiment", "")}
        <h3>過去24時間のセンチメント推移（10分毎・強気/弱気比率・投稿量）</h3>
        <div class="panel">{_img_tag(images, "sentiment_24h", "過去24時間のセンチメント推移")}</div>
        {extras.get("outliers", {}).get("sentiment_24h", "")}
      </section>

      <section data-tab-panel="levels" class="tab-hidden">
        <h2>📐 値動きの目安</h2>
        <div class="panel">
          <h3>支持線・抵抗線の目安（SD単位）</h3>
          {_support_levels_html(rec)}
        </div>
        <div class="panel">
          <h3>本日の値動き ÷ 執行コストの推移</h3>
          {_execution_cost_history_html(rec)}
        </div>
      </section>

      <section data-tab-panel="market" class="tab-hidden">
        <h2>🌐 市場コンテキスト・海外要因</h2>
        <div class="panel">
          <h3>市場コンテキスト</h3>
          {_market_context_html(rec)}
        </div>

        <h2>📰 関連ニュース（直近24時間）</h2>
        <div class="panel">{_news_summary_html(rec)}</div>
      </section>

      <section data-tab-panel="trend" class="tab-hidden">
        <h2>📈 過去14日間の推移</h2>
        <h3>過去14日間の価格推移（日足ローソク足・出来高）</h3>
        <div class="panel">{_img_tag(images, "trend14_price", "過去14日間の価格推移")}</div>
        <h3>過去14日間のセンチメント推移（強気/弱気/中立比率・投稿量）</h3>
        <div class="panel">{_img_tag(images, "trend14_sentiment", "過去14日間のセンチメント推移")}</div>
        {extras.get("outliers", {}).get("trend14_sentiment", "")}
      </section>

      <section data-tab-panel="stats" class="tab-hidden">
        {comprehensive_stats_section_html}
      </section>

      <hr style="border:none;border-top:1px solid var(--border);margin:2rem 0 1rem">
      <p class="caption">⚠️ {disclaimer}</p>
      <div id="disclaimer-all">{_ui5.disclaimer_extra_html()}</div>
      <footer>生成: generate_static_dashboard.py（{html.escape(generated_at_str)}） /
      画像バックエンド: {html.escape(IMAGE_BACKEND or "なし")}</footer>
    </div>
    """

    # ★2026-08-24追加・2026-09-06追記(スクロール位置の保存/復元): 詳細は
    # autorefresh_js()関数のdocstring参照(自動更新時にトップへ戻らないための対策)。
    autorefresh_js_html = autorefresh_js()
    # ★2026-08-24追加(ユーザー依頼「画像版でも閲覧者数を計上したい」):
    # visit_counter_js()はDOM要素(id='visit-count-badge')を書き換えるため、
    # bodyの中身(=その要素自体)がまだ構築されていないhead内ではなく、
    # </body>直前(=DOM構築完了後)に置く。
    visit_js = visit_counter_js(config.PUBLIC_VISIT_COUNTER_URL)
    # ★2026-08-27追加(ユーザー依頼「ダッシュボードに、いいねボタンを足せますか」):
    # like_button_js()もvisit_js同様、DOM要素(id='like-button'等)を書き換える/
    # イベント登録するため</body>直前に置く。
    like_js = like_button_js(config.PUBLIC_VISIT_COUNTER_URL)
    # ★2026-09-08追加: tab_nav_js()もvisit_js/like_js同様、DOM要素
    # (.tab-bar/.tab-btn/[data-tab-panel])を操作するため</body>直前に置く。
    tab_js = tab_nav_js()
    html_doc = (
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{symbol} 掲示板投稿の詳細分析による投資情報（静的版）</title>"
        f"<style>{css}</style>{autorefresh_js_html}</head><body>{body}{visit_js}{like_js}{tab_js}</body></html>"
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html_doc)


# ============================================================================
# main
# ============================================================================
# ============================================================================
# 自己修復: 起動時に自分自身の停滞した兄弟プロセスを片付ける(★2026-09-06追加)
# ============================================================================
# 経緯: 実機で2026-09-05 21:07〜2026-09-06 20:04の間、本スクリプトの旧インスタンス
# が最大約23時間居座り続け、平日1分毎タスク`BBS_StaticDashboard_285A`のIgnoreNew
# ポリシーと相まって公開静的サイトの更新が長時間止まる実害を発見(kaleidoの
# ヘッドレスブラウザ終了処理が稀に2分超ハングする既知の未解決問題・
# _システム開発_構造化ノート_マスター.md §④に記載済み)。タスクスケジューラの
# ExecutionTimeLimit(2分)は起動元のwscript.exeラッパープロセスを止めるだけで、
# そこから起動された本体のpython.exe(および更にその子のヘッドレスブラウザ)まで
# プロセスツリーとして正しく道連れに終了できておらず、1分毎のトリガーのたびに
# 孤児プロセスが積み上がっていた。
# ⇒ ラッパー/タスク設定そのものを直すのでなく、本体スクリプト自身が起動直後に
# 「自分と同じスクリプトを実行している、自分より十分古い(=正常なら完了している
# はずの)兄弟プロセス」を能動的に検知・強制終了する自己修復ガードを設ける
# (lm_studio_keeper.py・ai_sector_monitorのkeeper.pyと同じ「検知したら自己修復
# する」設計方針)。取得・終了いずれの失敗も握りつぶし本処理は止めない(fail-soft)。
STALE_SIBLING_AGE_SEC = 150  # 想定所要時間+ExecutionTimeLimit(2分)より十分長い閾値


def kill_stale_sibling_processes(script_name=None, self_pid=None, age_sec=None, run_fn=None):
    """自分自身(__file__)と同じコマンドラインを持つpython.exe/pythonw.exeのうち、
    自プロセスより age_sec 秒以上古いものを列挙し強制終了する(PowerShellの
    Win32_Processを1回呼ぶだけの軽量実装・age計算・kill判定ともPowerShell側で
    完結させ、Python側での日時パースを避ける)。

    script_name/self_pid/age_sec/run_fn は selftest 用の差し替え口(既定はそれぞれ
    __file__のbasename・os.getpid()・STALE_SIBLING_AGE_SEC・subprocess.run)。

    戻り値: 実際に強制終了できたPIDのリスト(取得/実行いずれかが失敗すれば空リスト・
    例外を外へ投げない)。
    """
    script_name = script_name or os.path.basename(__file__)
    self_pid = self_pid if self_pid is not None else os.getpid()
    age_sec = age_sec if age_sec is not None else STALE_SIBLING_AGE_SEC
    if run_fn is None:
        import subprocess
        run_fn = subprocess.run
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" "
            f"| Where-Object {{ $_.CommandLine -match [regex]::Escape('{script_name}') "
            f"-and $_.ProcessId -ne {self_pid} }} "
            f"| Where-Object {{ ((Get-Date) - $_.CreationDate).TotalSeconds -ge {age_sec} }} "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
            "$_.ProcessId }"
        )
        p = run_fn(["powershell", "-NoProfile", "-Command", ps_cmd],
                   capture_output=True, text=True, timeout=20)
        stdout = getattr(p, "stdout", "") or ""
        return [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def kill_stale_kaleido_chrome_processes(age_sec=None, run_fn=None):
    """kaleido(choreographer)がplotly画像出力のたびに起動するヘッドレスchrome.exe
    (`--headless`かつ一時プロファイル`AppData\\Local\\Temp\\tmpXXXXXXXX`を使う)のうち、
    age_sec 秒以上古いものを列挙し強制終了する。

    ★2026-09-07追加(「画像版公開ダッシュボードが更新されていないのでは」調査で発見)。
    kill_stale_sibling_processes()はpython.exe/pythonw.exeしか見ておらず、以下の
    経路で生まれる孤児chrome.exeは対象外だった：本体スクリプトが外部要因(手動実行の
    タイムアウト強制終了・タスクスケジューラのExecutionTimeLimit等)で異常終了すると、
    choreographer([browser_async.py]の"Resorting to unclean kill browser."経路)が
    最後まで走りきれず、起動済みのheadless chrome(親プロセスは既に消滅)が生き残る。
    1分毎のトリガーのたびにこれが積み上がり、CPU/メモリを奪い合って次回以降の画像
    生成がさらに遅くなり、"Resorting to unclean kill browser."の連鎖・最終的な
    ハングを引き起こしていた(実測：2026-09-07、孤児chrome.exeを実プロセス一覧から
    確認)。孤児側を狙って直接掃除する、kill_stale_sibling_processesと対になる
    ガード。取得・終了いずれの失敗も握りつぶし本処理は止めない(fail-soft)。

    age_sec/run_fn は selftest 用の差し替え口(既定はそれぞれ
    STALE_SIBLING_AGE_SEC・subprocess.run)。

    戻り値: 実際に強制終了できたPIDのリスト(取得/実行いずれかが失敗すれば空リスト・
    例外を外へ投げない)。
    """
    age_sec = age_sec if age_sec is not None else STALE_SIBLING_AGE_SEC
    if run_fn is None:
        import subprocess
        run_fn = subprocess.run
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
            "| Where-Object { $_.CommandLine -match '--headless' "
            "-and $_.CommandLine -match [regex]::Escape('AppData\\Local\\Temp\\tmp') } "
            f"| Where-Object {{ ((Get-Date) - $_.CreationDate).TotalSeconds -ge {age_sec} }} "
            "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
            "$_.ProcessId }"
        )
        p = run_fn(["powershell", "-NoProfile", "-Command", ps_cmd],
                   capture_output=True, text=True, timeout=20)
        stdout = getattr(p, "stdout", "") or ""
        return [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    except Exception:
        return []


LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".generate_static_dashboard.lock")


def _pid_is_alive(pid):
    """指定PIDのプロセスが現在も生存しているかを判定する(tasklistを1回呼ぶだけの
    軽量実装)。判定不能(取得失敗)な場合は安全側=生存扱い(True)にする。誤って
    ロックを奪って多重起動するより、判定できない今回はスキップする方向に倒す
    (fail-safe。他の自己修復ガードのfail-soft=「失敗したら空リスト」とは逆に、
    ここは「失敗したら保守的に振る舞う」方を安全側とする)。"""
    import subprocess
    try:
        p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, text=True, timeout=10)
        return str(pid) in (p.stdout or "")
    except Exception:
        return True


def acquire_run_lock(lock_path=None, self_pid=None, pid_alive_fn=None):
    """本体スクリプトの多重起動をロックファイル(PID記録)で直接防ぐ。

    ★2026-09-07追加(「画像版公開ダッシュボードが更新されていないのでは」調査で発見)。
    kill_stale_sibling_processes/kill_stale_kaleido_chrome_processes(いずれも
    age_sec=150秒の事後清掃)だけでは防げない多重起動があった：タスクの
    ExecutionTimeLimit(2分)はラッパー(wscript.exe)だけを殺しpython.exe以下の
    実プロセス木は殺さないため、1回の生成が2分を超えた瞬間にTask Schedulerは
    「終了した」と誤認し、MultipleInstances=IgnoreNewをすり抜けて次のトリガー
    (1分毎)が新しいpython.exe+kaleido chrome家族を起動してしまう。実際には
    複数世代のpython.exe/chromeが並走してCPU/メモリを奪い合い、各々の画像生成が
    さらに遅くなって"Resorting to unclean kill browser."(choreographer)の連鎖→
    事実上の無限ハングに陥っていた(実測:2026-09-07 08:38台の生成のまま1時間以上
    進まず、手動実行では同ログが14連続で出て180秒タイムアウンド)。
    事後の年齢しきい値清掃でなく、開始時点で「今まさに生きている前回実行」を
    検知し即座に諦めることで、積み上がりそのものを起こさせない一次防御。

    ★2026-09-07 追記(実機で発見・即修正)＝最初の実装は「存在確認→読取→(生存判定)→
    書込」を別々のopen呼び出しで行っており、ほぼ同時に2プロセスが起動すると両方が
    「ロック無し」を見て両方書込に進むTOCTOU(check-then-act)競合があった。実際に
    本番で2世代のpython.exe(前トリガー由来・新トリガー由来)がこの窓で共に通過し、
    一瞬だけ両方がkaleido chromeを起動した実例を観測(いずれも数十秒以内に片方が
    自然完走し実害は無かったが、設計として直す)。⇒`os.open(..., O_CREAT|O_EXCL)`
    による排他的新規作成(存在すれば即座にFileExistsErrorで失敗する、この2手順が
    OS側でアトミックに実行される)に置き換えた。既存ロックが生きていれば素直に
    諦め(False)、死んでいれば1回だけ奪って(削除→再度O_EXCLで作成)取得する。

    lock_path/self_pid/pid_alive_fn は selftest 用の差し替え口。
    戻り値: True=ロック取得成功(このプロセスが実行してよい)/False=既に別の実行が
    生存中のため今回はスキップすべき。ロックファイルの読み書き失敗もFalse側に倒す
    (fail-safe)。"""
    lock_path = lock_path or LOCK_PATH
    self_pid = self_pid if self_pid is not None else os.getpid()
    pid_alive_fn = pid_alive_fn or _pid_is_alive

    def _try_create():
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(self_pid).encode("utf-8"))
        finally:
            os.close(fd)

    try:
        try:
            _try_create()
            return True
        except FileExistsError:
            pass
        with open(lock_path, "r", encoding="utf-8") as f:
            prev_pid_str = f.read().strip()
        if prev_pid_str.isdigit() and int(prev_pid_str) != self_pid \
                and pid_alive_fn(int(prev_pid_str)):
            return False
        # 前回のロックは自分自身、または既に死んでいるプロセスのもの -> 奪って取得
        os.remove(lock_path)
        _try_create()
        return True
    except Exception:
        return False


def release_run_lock(lock_path=None, self_pid=None):
    """acquire_run_lock()で取得したロックを解放する(自分のPIDが書かれている場合のみ
    削除し、他プロセスが既に上書きしていた場合は誤って消さない)。失敗は握りつぶす
    (fail-soft・解放できなくても次回acquire側のpid_alive_fnが「もう生きていない」と
    判定すれば自然に回復する)。"""
    lock_path = lock_path or LOCK_PATH
    self_pid = self_pid if self_pid is not None else os.getpid()
    try:
        if os.path.exists(lock_path):
            with open(lock_path, "r", encoding="utf-8") as f:
                cur = f.read().strip()
            if cur == str(self_pid):
                os.remove(lock_path)
    except Exception:
        pass


def main():
    t0 = time.time()
    killed = kill_stale_sibling_processes()
    if killed:
        print(f"[SELFHEAL] killed {len(killed)} stale sibling process(es): {killed}")
    killed_chrome = kill_stale_kaleido_chrome_processes()
    if killed_chrome:
        print(f"[SELFHEAL] killed {len(killed_chrome)} stale orphaned kaleido chrome process(es): {killed_chrome}")
    if not acquire_run_lock():
        print("[SELFHEAL] a previous run is still alive -> skipping this cycle (lock held)")
        return
    try:
        print(f"[INFO] image backend: {IMAGE_BACKEND}")
        rec = load_record()
        rec = apply_live_overrides(rec)
        board_totals_series = load_board_totals_series()
        print(f"[INFO] board_totals points: {len(board_totals_series)}")
        images, extras = generate_all_images(rec, board_totals_series)
        print(f"[INFO] images generated: {len(images)} -> {IMG_DIR}")
        generated_at_str = dt.datetime.now().isoformat(timespec="seconds")
        render_html(rec, images, extras, generated_at_str)
        try:  # 2026-11-26(ユーザー承認): 公開ルート直下に latest.txt(同一スナップショット)。失敗してもindex.htmlの公開は止めない
            import re as _re
            _lt = _ui5.latest_txt(rec, generated_at_str)
            if _lt and not _re.search("(?i)moomoo|ocr", _lt):   # 公開用リーク防止(moomoo/OCR由来は載せない)
                _tmp = os.path.join(OUT_DIR, "latest.txt.tmp")
                with open(_tmp, "w", encoding="utf-8", newline="") as _f:
                    _f.write(_lt)
                os.replace(_tmp, os.path.join(OUT_DIR, "latest.txt"))
                print(f"[INFO] latest.txt written ({len(_lt.encode('utf-8')):,} bytes)")
        except Exception as _e:
            print(f"[WARN] latest.txt failed (index.html is unaffected): {_e!r}")
        elapsed = time.time() - t0
        index_size = os.path.getsize(INDEX_PATH)
        print(f"[DONE] index.html written ({index_size:,} bytes) in {elapsed:.1f}s -> {INDEX_PATH}")
    finally:
        release_run_lock()


if __name__ == "__main__":
    sys.exit(main())
