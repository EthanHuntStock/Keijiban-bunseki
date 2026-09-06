# -*- coding: utf-8 -*-
"""
public_dashboard.py - 一般公開用(Googleサイト「掲示板の分析による投資情報」)ダッシュボード。
★2026-08-19作成(おにや09:00投稿)。既存の内部ダッシュボード(dashboard.py・9タブ・研究/
エンタメ用の端末UI)とは別の、一般公開向けの読取専用ページ。

【最重要・構造的な漏洩防止】このページは data/public_export/latest.json (=
public_export.build_public_record() が組み立て、validate_no_leak() を通過済みの
集計値のみのレコード)だけを読む。dashboard.py が直接参照している生データ
(analyzed.jsonl/raw_comments.jsonl 等)には一切触れない。個別投稿の抜粋・
著者情報・URL・投稿ID等は latest.json 自体に含まれない設計(public_export.py側の
ホワイトリスト方式+validate_no_leak()の多重防御)なので、このページ側で追加の
フィルタリングを行う必要はない(そもそも渡ってこない)。

含める要素(おにや09:00投稿①):
  (a) ヘッダー(銘柄名・現在値・騰落率・更新時刻)
  (b) 2大メーター: 🔥灼熱メーター/😱阿鼻叫喚メーター(dashboard.py._gauge()を再利用)
  (c) ボラ・レジーム帯(dashboard.py.regime_band()を再利用・latest.json['regime']由来)
  (d) 9シグナルの発火状況一覧(what-ifしきい値調整UIは含めない・現在値+閾値+バッジのみ)
  (e) 価格チャート(price_sentiment_series由来の直近推移)
  (f) センチメント推移(強気/弱気/中立比率の時系列)
  (g) AI考察(latest.json['ai_commentary']・public_insight.generate_public_insight()生成)
  (h) 免責事項

含めないもの(おにや09:00投稿②): コメント抜粋・moomooタブ・北極星(研究)タブ・
what-ifしきい値調整UI・板(L2)詳細 -- いずれもこのページのデータソース
(集計値のみのlatest.json)には最初から存在しないため、構造的に表示不可能。

起動: streamlit run public_dashboard.py
"""
import datetime as dt
import html

import streamlit as st

import config
import public_export
from dashboard import (
    COL, REGIME_BANDS, HAS_PLOTLY, chip, inject_css, _gauge, regime_band, _rgba,
)

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except Exception:
    pass  # HAS_PLOTLY(dashboard.py側で判定済み)で分岐するため、ここでの失敗は無視してよい

# ★2026-08-19追加: 公開ページなので自動更新は既定ON(dashboard.py側のような手動トグルは
# 置かない=一般公開向けの読取専用ページで、内部端末UIと違って毎回自分で操作する
# 想定読者ではないため)。catchup(既定10分間隔)でlatest.jsonが更新されるのに合わせ、
# 60秒毎にページ全体を再実行してファイルを読み直す(dashboard.py と同じ
# streamlit_autorefresh 経由・未導入環境でもimportエラーにならないようgraceful fallback)。
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False
PUBLIC_DASHBOARD_AUTOREFRESH_SEC = 60


# ============================================================================
# ヘッダー
# ============================================================================
def _header(rec, live_price=None):
    symbol = rec.get("symbol") or config.SYMBOL
    name = rec.get("company_name") or ""
    price = rec.get("price") or {}
    last = price.get("last")
    chg = price.get("change_pct")
    gen_at = rec.get("generated_at")
    price_as_of = None

    # ★2026-08-20追加(ユーザー依頼「株価の更新が遅すぎる。60秒ごとの更新時に、
    # その時の株価になるようにしてください」→その後「公開版ではYahooでなく
    # 自己取得しているkabuのデータを使うように」)。優先順位は
    # ①live_price(株取引API_プロト1のkabuティックから60秒毎に生成される
    #   live_priceタブ・main()側で一度だけ取得しここへ渡される。最も正確)
    # ②fetch_live_price_header(公開ダッシュボード自身からのYahoo直接取得。
    #   ①のブリッジが未設定/一時的に失敗している時の保険)
    # ③rec['price'](Sheets由来・最大10分古い。①②とも失敗した時の最終手段)
    # のどれかで「今この瞬間」に近い値を表示する(全滅時のみ既存rec['price']の
    # まま=後退にしかならない設計)。
    # ★2026-08-20: dt.datetime.now()はStreamlit Cloudのサーバーローカル時刻
    # (=多くの場合UTC)を返すため、そのまま表示すると実際の日本時間と9時間
    # ズレる(実測: JST 11:26のはずが「02:26時点」と表示される不具合を発見)。
    # 家PC1(JST)でのローカル実行時は問題が起きなかったため見落としていた。
    # 明示的にJST(UTC+9)へ変換する(このプロジェクトの他箇所と同じ変換方式)。
    # ★2026-08-20修正: dt.datetime.utcnow()はPython 3.12+で非推奨(3.14でも動作は
    # するが実行のたびDeprecationWarningがStreamlit Cloudのログに大量出力される
    # ことを実測で確認・実害はないが放置しない)。dt.datetime.now(dt.timezone.utc)
    # (tz-aware)を素朴に使うと、後続の+timedelta(hours=9)後もtzinfo=UTCのまま
    # 残り「JSTの値なのにUTCを名乗る」不整合なオブジェクトになる(このモジュール
    # 全体がnaive datetime前提の設計のため)。.replace(tzinfo=None)で
    # utcnow()と全く同じ「naiveなUTC値」に戻してから使う=既存の全比較・
    # strftime呼び出しへの影響を完全にゼロにする変更。
    now_jst = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=9)
    stale_warning = None
    if live_price and live_price.get("price", {}).get("last") is not None:
        last = live_price["price"]["last"]
        chg = live_price["price"].get("change_pct")
        price_as_of = now_jst.strftime("%H:%M:%S")
        # ★2026-08-20追加(ユーザー提案「live_price_bridgeの死活監視」への対応)。
        # live_price_bridge.py(1分毎)が何らかの理由で止まっても、この関数自体は
        # fail-softに動き続けるため、閲覧者にもこちら側にも「古い値のまま更新が
        # 止まっている」ことが気づかれないリスクがあった(2026-08-20にトレPJ側の
        # 記録停止で実際に起きた事象と同型のリスクをこちら自身にも予防的に
        # 入れる)。取引時間中(_is_trading_hours)にだけ判定し、夜間・週末・昼休みの
        # 正常な休止を異常と誤検知しないようにする。
        staleness = public_export.live_price_staleness_minutes(
            live_price.get("generated_at"), now=now_jst)
        if (staleness is not None and staleness > config.LIVE_PRICE_STALE_MINUTES
                and public_export._is_trading_hours(now=now_jst)):
            stale_warning = (f"⚠️ 価格データの更新が約{staleness:.0f}分前から止まっている"
                             f"可能性があります(通常は1分毎に更新)。")
    else:
        live = public_export.fetch_live_price_header(rec.get("price_sentiment_series"))
        if live and live.get("last") is not None:
            last = live["last"]
            chg = live.get("change_pct")
            price_as_of = now_jst.strftime("%H:%M:%S")

    chg_color = COL["grey"]
    chg_text = "—"
    if chg is not None:
        chg_color = COL["red"] if chg > 0 else (COL["blue"] if chg < 0 else COL["grey"])
        chg_text = f"{chg:+.2f}%"
    # ★2026-08-19: dashboard.py(内部ダッシュボード)と同じ表記(カンマ区切り・小数点無し)
    # に統一(例: 50020.0円 -> 50,000円)。ユーザー指摘を受けて修正。
    price_text = f"{last:,.0f}" if last is not None else "—"

    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:14px;flex-wrap:wrap'>"
        f"<span style='font-size:1.6em;font-weight:700;color:{COL['text']}'>{symbol}</span>"
        f"<span style='font-size:1.05em;color:{COL['muted']}'>{name}</span>"
        f"<span style='font-size:1.5em;font-weight:700;color:{COL['text']}'>"
        f"{price_text}円</span>"
        f"<span style='font-size:1.15em;font-weight:600;color:{chg_color}'>{chg_text}</span>"
        f"</div>",
        unsafe_allow_html=True)
    if price_as_of:
        st.caption(f"株価: {price_as_of} 時点でライブ取得（{PUBLIC_DASHBOARD_AUTOREFRESH_SEC}秒毎に再取得） "
                  f"／ その他データの更新時刻: {gen_at or '不明'}")
    else:
        st.caption(f"更新時刻: {gen_at or '不明'}"
                  f"（このページは{PUBLIC_DASHBOARD_AUTOREFRESH_SEC}秒毎に自動更新されます）")
    if stale_warning:
        st.caption(stale_warning)
    _visit_counter_badge()
    _like_button()


# ============================================================================
# ★2026-08-27追加(ユーザー依頼「ダッシュボードに、いいねボタンを足せますか」)。
# 閲覧数カウンター(_visit_counter_badge・直下)と同じGoogle Apps Script Web App
# (apps_script_visit_counter.gs)を流用する。新しいシートタブ(like_counter)へ
# 1加算するaction=like_hit・加算せず現在値だけ読むaction=like_readを追加した
# ので、新しい第三者サービスへの接続や新しいSecretsは一切不要(同じURLを使う)。
# st.session_stateで1ブラウザセッションにつき1回だけ押せるようにし(押した後は
# ボタンをdisabledにして見た目でも分かるようにする)、連打による多重加算を防ぐ。
# URL未設定・通信失敗時はボタン自体を表示しない(fail-soft、閲覧数カウンターと同型)。
# ============================================================================
def _like_button():
    url = config.PUBLIC_VISIT_COUNTER_URL
    if not url:
        return
    if "_like_count" not in st.session_state:
        st.session_state["_like_count"] = public_export.record_visit(url, action="like_read")
    already = st.session_state.get("_liked", False)
    col_btn, col_label = st.columns([1, 12])
    with col_btn:
        clicked = st.button("✅" if already else "👍", key="_like_btn", disabled=already,
                            help="役に立ったら押してください")
    if clicked and not already:
        new_count = public_export.record_visit(url, action="like_hit")
        if new_count is not None:
            st.session_state["_like_count"] = new_count
        st.session_state["_liked"] = True
        already = True
    count = st.session_state.get("_like_count")
    count_text = f"（累計 {count:,} いいね）" if count is not None else ""
    with col_label:
        st.caption(("いいねしました！" if already else "このダッシュボードが役に立ったら押してください")
                  + count_text)


# ============================================================================
# ★2026-08-20追加(ユーザー依頼: 公開サイトの閲覧者数を集計して表示)。
# 既存のGoogle Sheets連携基盤上のGoogle Apps Script Web App(config側で説明)へ
# 問い合わせてバッジ表示する。streamlit-autorefresh(60秒毎)による定期rerunの
# たびに加算すると「閲覧者数」でなく「ページ再読込回数」を数えてしまうため、
# st.session_stateで1ブラウザセッション(タブ)につき1回だけ加算(action=hit)し、
# 以降のrerunはaction=read(加算なし)で現在値だけ取得し直して表示を新鮮に保つ。
# URL未設定・通信失敗時はバッジ自体を表示しない(fail-soft)。
# ============================================================================
def _visit_counter_badge():
    url = config.PUBLIC_VISIT_COUNTER_URL
    if not url:
        return
    action = "read" if st.session_state.get("_visit_counted") else "hit"
    count = public_export.record_visit(url, action=action)
    if action == "hit":
        st.session_state["_visit_counted"] = True
    if count is not None:
        st.session_state["_visit_count"] = count
    else:
        count = st.session_state.get("_visit_count")
    if count is None:
        return
    st.caption(f"👀 累計閲覧数: {count:,}")


# ============================================================================
# ★2026-08-20追加(ユーザー提案「灼熱/阿鼻叫喚メーターに推移スパークラインを」)。
# 「今の値」が歴史的に高いのか低いのか一目で分かるよう、各ゲージの下に軸目盛り
# 無しの小さな折れ線(スパークライン)を添える。データ源はrec['board_history_14d']
# (public_export.board_score_daily_series()が組み立て済み・cloud側も既存の
# json_blob同期だけで受け取れる=新たな通信を増やさない)。
# ============================================================================
def _meter_sparkline(history_14d, score_key, line_color):
    pts = [p for p in (history_14d or []) if p.get(score_key) is not None]
    if len(pts) < 2:
        st.caption("推移データ蓄積中です。")
        return
    date_labels = [_mmdd(p.get("date")) for p in pts]
    values = [p.get(score_key) for p in pts]
    if HAS_PLOTLY:
        # ★2026-08-21(ユーザー依頼「縦軸に単位を付けて・線の太さは3に」): 従来は
        # showticklabels=Falseで目盛りラベル自体を出していなかった(ミニマルな
        # スパークライン設計)が、スコアの単位(0-100点)が分からないとの指摘を受け、
        # ダッシュボード他所(ゲージ・9指標一覧)と同じ"/100"表記に統一して復活させる。
        # nticks=3で最小限(上端・中間・下端程度)に抑え、スパークラインとしての
        # 軽さは維持する。
        f = go.Figure(go.Scatter(x=date_labels, y=values, mode="lines+markers",
                                 line=dict(color=line_color, width=3),
                                 marker=dict(size=4)))
        f.update_layout(height=70, margin=dict(l=32, r=4, t=2, b=18),
                        paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        xaxis=dict(type="category", showgrid=False,
                                  tickfont=dict(size=9, color=COL["muted"])),
                        yaxis=dict(showgrid=False, showticklabels=True, nticks=3,
                                  ticksuffix="/100",
                                  tickfont=dict(size=8, color=COL["muted"])))
        # ★2026-08-21追加(_gauge()と同じ理由=ウィジェット同一性の取り違え防止): この
        # スパークラインも1画面内でscore_key違いで2回呼ばれる(overheat_score/
        # capitulation_score)ため、score_keyから導出した安定キーを明示する。
        st.plotly_chart(f, width="stretch", config={"displayModeBar": False},
                        key=f"meter_sparkline_{score_key}")
    else:
        st.line_chart({score_key: values})


# ============================================================================
# (d) 9シグナル一覧(what-ifしきい値調整UIは含めない・現在値+閾値+バッジのみ)
# ============================================================================
_STATE_COLOR = {"発火": COL["red"], "警戒": COL["orange"], "OK": COL["green"]}


# ★2026-08-19追加(ユーザー依頼「9指標の意味合いも分かるように」): signals.build_signal_cards()
# が返す9つのカード名それぞれについて、何を測っている指標なのかの一言説明。
# カード名の文字列と完全一致させて引く(signals.py側の名称を変更した場合はここも要更新)。
_SIGNAL_DESC = {
    "灼熱メーター(過熱)": "掲示板全体の強気/弱気の偏りと投稿の勢いから算出した「過熱度」の合成指標。",
    "そう思う大量票": "Yahoo!掲示板の「そう思う」共感ボタンが1投稿に集中して大量に押されているか。",
    "イナゴ語彙(euphoria)": "「爆上げ」「握力」等、熱狂・便乗を示す語彙が投稿にどれだけ含まれるか。",
    "ネームド集中": "特定の常連投稿者(ハッシュ化ID)に投稿が偏っていないか(少数の声が目立ちすぎていないか)。",
    "他銘柄混入率": "本銘柄の掲示板で他の銘柄コードへの言及がどれだけ多いか(関心が他へ移りつつあるサイン)。",
    "暴落煽り語彙": "「暴落」「終わった」等、悲観・煽りを示す語彙が投稿にどれだけ含まれるか。",
    "阿鼻叫喚(セリクラ)": "株価が安値圏にある中で悲観語彙が急増しているか(セリングクライマックスの兆候)。",
    "話題枯れ": "過去の平均と比べて本日の投稿数が著しく少ないか(閑散・関心低下のサイン)。",
    "投稿サージ": "過去の平均と比べて本日の投稿数が著しく多いか(急な注目集中のサイン)。",
}


# ★2026-08-20追加(ユーザー提案「9指標の状態変化が分かるように」)。前回の取引日
# から状態(OK/警戒/発火)が変わった指標だけを一覧の直前に強調表示する。データ源は
# rec['signal_state_changes'](public_export.signal_state_changes()が組み立て済み)。
def _signal_changes_note(rec):
    changes = rec.get("signal_state_changes") or []
    if not changes:
        return
    lines = "、".join(f"「{c.get('name')}」{c.get('from')}→{c.get('to')}" for c in changes)
    st.info(f"📌 前回の取引日({changes[0].get('compared_date', '')})からの状態変化: {lines}")


def _signal_sparkline(history_14d, card_name, line_color):
    """★2026-08-25追加(ユーザー指摘「公開用ダッシュボード(streamlit版)は直ってないのでは」
    =画像版のみに実装していたシグナル発火状況(9指標)の推移ミニグラフをstreamlit版にも
    追加する)。_meter_sparkline()と同じ描画パターン(plotly折れ線・軸ラベル最小限)を、
    固定2キーでなくcard_name(9指標それぞれの名前)で汎用化したもの。データ源は
    rec['signal_cards_history_14d'](public_export.signal_cards_daily_series()が
    組み立て済み・board_history_14dと同じくlatest.json経由でcloud側も受け取れる)。"""
    pts = [p for p in (history_14d or []) if p.get(card_name) is not None]
    if len(pts) < 2:
        st.caption("推移データ蓄積中です。")
        return
    date_labels = [_mmdd(p.get("date")) for p in pts]
    values = [p.get(card_name) for p in pts]
    if HAS_PLOTLY:
        f = go.Figure(go.Scatter(x=date_labels, y=values, mode="lines+markers",
                                 line=dict(color=line_color, width=2),
                                 marker=dict(size=3)))
        f.update_layout(height=54, margin=dict(l=2, r=2, t=2, b=14),
                        paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        xaxis=dict(type="category", showgrid=False,
                                  tickfont=dict(size=7, color=COL["muted"])),
                        yaxis=dict(showgrid=False, showticklabels=False))
        # ★_meter_sparkline()と同じ理由: 1画面内でcard_name違いで9回呼ばれるため、
        # card_nameから導出した安定キーを明示する(ウィジェット同一性の取り違え防止)。
        st.plotly_chart(f, width="stretch", config={"displayModeBar": False},
                        key=f"signal_sparkline_{card_name}")
    else:
        st.line_chart({card_name: values})


def _signal_list(rec):
    cards = rec.get("signal_cards") or []
    if not cards:
        st.caption("シグナルデータ蓄積中です。")
        return
    history_14d = rec.get("signal_cards_history_14d") or []
    for c in cards:
        state = c.get("state", "OK")
        color = _STATE_COLOR.get(state, COL["grey"])
        value = c.get("value")
        value_text = "—" if value is None else (
            f"{value:.2f}" if isinstance(value, float) else str(value))
        chip_text = f"{state} (現在値{value_text} / 閾値{c.get('threshold', '')})"
        chip_html = chip(chip_text, color)
        name = c.get("name", "")
        desc = _SIGNAL_DESC.get(name, "")
        # ★2026-08-25追加: 元は1本のflex divでname/note/badgeを横並びにしていたが、
        # ここへスパークラインを足す先として、st.columns(ページレベルのレイアウト・
        # 狭幅では自動的に縦積みになりテキスト長による崩れが起きない)を使う。
        # 既存のname/note/badge表示(HTML文字列)自体は変更しない。
        col_text, col_spark = st.columns([5, 1.4])
        with col_text:
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"border-bottom:1px solid {COL['border']};padding:6px 2px;flex-wrap:wrap;gap:6px'>"
                f"<span style='color:{COL['text']}'>{name}"
                f"<br><span style='color:{COL['muted']};font-size:.8em;font-weight:normal'>{desc}</span>"
                f"</span>"
                f"<span style='color:{COL['muted']};font-size:.88em'>{c.get('note', '')}</span>"
                f"<span>{chip_html}</span>"
                f"</div>", unsafe_allow_html=True)
        with col_spark:
            _signal_sparkline(history_14d, name, color)


# ============================================================================
# 関連ニュース(★2026-08-27追加・ユーザー依頼「24時間以内のキオクシアに関係し
# そうなニュースの要約を公開ダッシュボードに」「検索頻度は10分毎・新ニュースが
# 出たら更新とリンクを」)。データ源はrec['news']
# (news_fetch.collect_news()→public_export.build_public_record()が組み立て済み・
# 10分毎のcatchupサイクルに相乗りして自動更新される)。
# ============================================================================
def _fmt_news_published(utc_iso):
    """純関数寄り(streamlit非依存): news_fetch内部表現(UTC・'YYYY-MM-DDTHH:MM:SS')を
    JST表示('M/D HH:MM')へ変換する。パース不能/Noneなら空文字(fail-soft・
    時刻不明の記事でも一覧自体は表示する)。"""
    if not utc_iso:
        return ""
    try:
        t = dt.datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%S") + dt.timedelta(hours=9)
        # ★Windows実行環境ではstrftimeの"%-m"(ゼロ埋め無し月)非対応のため、
        # 手動でM/D形式を組み立てる(generate_static_dashboard.pyの_mmdd()と同型)。
        return f"{t.month}/{t.day} {t.strftime('%H:%M')}"
    except (ValueError, TypeError):
        return ""


def _news_summary(rec):
    """rec['news']自体が無い(RSS取得失敗・機能追加前のhistory等)場合はセクションを
    丸ごと省く(fail-soft・存在しない情報を捏造しない)。"""
    news = rec.get("news")
    if not news:
        return
    items = news.get("items") or []
    summary_text = news.get("summary_text")
    if not items:
        st.caption("直近24時間以内の関連ニュースは見つかりませんでした。")
        return
    if summary_text:
        st.markdown(html.escape(summary_text))
    for it in items[:10]:
        title = html.escape(it.get("title") or "")
        link = html.escape(it.get("article_link") or "", quote=True)
        source = html.escape(it.get("source") or "出所不明")
        time_label = _fmt_news_published(it.get("published"))
        time_part = f"・{time_label}" if time_label else ""
        st.markdown(
            f"<div style='padding:3px 0;font-size:.92em'>"
            f"<a href='{link}' target='_blank' rel='noopener noreferrer' "
            f"style='color:{COL['text']}'>{title}</a>"
            f"<span style='color:{COL['muted']};font-size:.85em'>"
            f"（{source}{time_part}）</span></div>",
            unsafe_allow_html=True)
    st.caption("※ニュース見出しの要約であり、投資助言ではありません。"
              "各見出しから元記事(Google News経由)へ移動できます。")


# ★2026-08-20: 「YYYY-MM-DD」→「M/D」表記への変換(ユーザー依頼「グラフの日付は
# aug15でなく8/15に」)。元は_price_and_sentiment_charts内のローカル関数だったが、
# メーター推移スパークライン(_meter_sparkline)でも同じ変換が必要になったため
# モジュール直下へ引き上げて共用する。
def _mmdd(d):
    if not d or "-" not in d:
        return d
    parts = d.split("-")
    if len(parts) != 3:
        return d
    return f"{int(parts[1])}/{int(parts[2])}"


def _outlier_caption(times, values, label="投稿量"):
    """★2026-08-21追加(ユーザー依頼「改善提案②=異常値の自動検出」)。件数・
    数量系のグラフ(投稿量・板の買い/売り総計等)の直下に、
    public_export.detect_series_outliers()が検出した外れ値があれば小さな
    注記を出す。値そのものは書き換えない(検出のみ・透明性を保つ=捏造しない
    設計原則どおり)。外れ値が無ければ何も表示しない(平常時は静か)。"""
    pts = [{"time": t, "v": v} for t, v in zip(times or [], values or [])]
    flagged = public_export.detect_series_outliers(pts, "v")
    if flagged:
        shown = "、".join(flagged[:5])
        more = f" ほか{len(flagged) - 5}件" if len(flagged) > 5 else ""
        st.caption(f"⚠️ {label}に自動検出された外れ値: {shown}{more}"
                  "(収集の一括キャッチアップ等が原因の可能性・値は捏造/削除せずそのまま表示しています)")


# ============================================================================
# (e)(f) 価格チャート + センチメント推移(price_sentiment_series由来)
# ============================================================================
def _price_and_sentiment_charts(rec, live_price=None):
    pss = list(rec.get("price_sentiment_series") or [])
    # ★2026-08-20追加(ユーザー指示「過去14日間の推移の株価も60秒毎に最新値に」)。
    # live_price(kabuティックから60秒毎に生成)に本日1本ぶんのOHLCがあれば、
    # 系列の末尾(本日分)をそれで差し替える(センチメント[bull/bear_ratio]は
    # kabu側に無いのでSheets由来のまま=価格だけ新鮮に保つ)。日付が末尾と一致
    # しなければ新しい日として追加する(まだ14日系列に本日分が無い最初の
    # 数分間の場合)。
    if live_price and live_price.get("today_daily_bar"):
        bar = live_price["today_daily_bar"]
        if pss and pss[-1].get("date") == bar.get("date"):
            pss[-1] = {**pss[-1], "price_open": bar["price_open"], "price_high": bar["price_high"],
                      "price_low": bar["price_low"], "price_close": bar["price_close"],
                      "price_volume": bar["price_volume"]}
        else:
            pss.append({"date": bar["date"], "price_open": bar["price_open"],
                       "price_high": bar["price_high"], "price_low": bar["price_low"],
                       "price_close": bar["price_close"], "price_volume": bar["price_volume"],
                       "bull_ratio": None, "bear_ratio": None, "post_count": None})
    st.markdown("#### 📈 過去14日間の推移")
    if not pss:
        st.caption("価格×センチメントの推移データ蓄積中です。")
        return
    dates = [p.get("date") for p in pss]
    # ★2026-08-19: ユーザー依頼「グラフの日付はaug15でなく8/15にしましょう」対応。
    # datesはYYYY-MM-DDのISO文字列のため、Plotlyがx軸を日付型と自動判定し
    # 既定の日付ティック書式(英語省略月名、例:Aug15)で表示してしまっていた。
    # 明示的に「月/日」形式のラベル文字列に変換し、x軸もcategory型にすることで
    # 意図した「8/15」表記・かつ非営業日ぶんの隙間なしの詰め表示にする。
    date_labels = [_mmdd(d) for d in dates]
    closes = [p.get("price_close") for p in pss]
    bulls = [p.get("bull_ratio") for p in pss]
    bears = [p.get("bear_ratio") for p in pss]
    neutrals = [None if (b is None or r is None) else max(0.0, 1.0 - b - r)
               for b, r in zip(bulls, bears)]

    # ★2026-08-21修正(ユーザー依頼「過去14日間の推移の二つのグラフも、上下に
    # 並べましょう」): 従来はcol1(価格推移)/col2(センチメント推移)の左右2列
    # だったが、「本日の推移」セクションと同じ縦一列(全幅)へ揃える。
    st.markdown("**過去14日間の価格推移（日足ローソク足・出来高）**")
    if HAS_PLOTLY:
        # ★2026-08-19: ユーザー依頼「価格推移をローソク足にしてほしい」を受け
        # Scatter(終値の折れ線)からCandlestickへ変更。色は日本の相場慣行
        # (上昇=赤・下降=青)に合わせる(ユーザー指定・ヘッダーの騰落率表示と同じ配色)。
        opens = [p.get("price_open") for p in pss]
        highs = [p.get("price_high") for p in pss]
        lows = [p.get("price_low") for p in pss]
        vols = [p.get("price_volume") for p in pss]
        # ★2026-08-19: ユーザー依頼「価格推移に出来高を足せますか」対応。
        # ローソク足の下に出来高バーを別行(サブプロット)として追加。x軸を共有
        # (shared_xaxes)し、当日の陽線/陰線と同じ配色(上昇=赤・下降=青)で
        # バーを塗ることで、ローソク足と出来高の対応が視覚的に分かるようにする。
        vol_colors = [COL["grey"] if (o is None or c is None) else
                     (COL["red"] if c >= o else COL["blue"])
                     for o, c in zip(opens, closes)]
        f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                          row_heights=[0.72, 0.28], vertical_spacing=0.03)
        f.add_trace(go.Candlestick(
            x=date_labels, open=opens, high=highs, low=lows, close=closes,
            increasing_line_color=COL["red"], decreasing_line_color=COL["blue"]),
            row=1, col=1)
        f.add_trace(go.Bar(x=date_labels, y=vols, marker_color=vol_colors,
                           showlegend=False), row=2, col=1)
        f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        height=270, margin=dict(l=8, r=8, t=8, b=8),
                        font=dict(color=COL["text"]), xaxis_rangeslider_visible=False,
                        showlegend=False)
        # ★2026-08-19: ユーザー依頼「非営業日は非表示にして、表示を
        # 詰めて、営業日14日間を表示」対応。datesは既に営業日のみ
        # (前段のtrading-day-onlyフィルタで非営業日は除外済)だが、
        # xaxisを日付型のままにするとPlotlyが暦日ベースで間隔を
        # 取り、除外した週末/祝日の分だけ視覚的な空白が残る。
        # type="category"にして14個の日付ラベルを隙間なく詰める(両サブプロット共通)。
        f.update_xaxes(type="category", row=1, col=1)
        f.update_xaxes(type="category", row=2, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",", row=1, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
        st.plotly_chart(f, width="stretch")
    else:
        st.line_chart({"終値": closes})

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    st.markdown("**過去14日間のセンチメント推移（強気/弱気/中立比率・投稿量）**")
    if HAS_PLOTLY:
        # ★2026-08-20: ユーザー依頼「センチメント推移のグラフに、投稿量の棒
        # グラフを足せますか」対応。価格チャート(ローソク足+出来高)と同じ設計
        # (make_subplotsで2段・shared_xaxes)で、下段に投稿量の棒グラフを追加する。
        posts = [p.get("post_count") for p in pss]
        f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                          row_heights=[0.72, 0.28], vertical_spacing=0.03)
        # ★2026-08-20: ユーザー依頼「グラフの線を太く」「凡例が横軸表記に
        # かぶらないように」を受け、線幅を太くし、凡例をプロット領域の"上"に
        # 明示配置(yanchor="bottom", y=1.02)して横軸ラベルとの重なりを避ける。
        # 上部余白(margin.t)もその分広げる。★2026-08-21: ユーザー依頼「線の
        # 太さを3に」を受け全トレース幅3へ統一(従来は強気/弱気5・中立4)。
        # ★2026-08-21追加(ユーザー指摘「投稿量はあるのにグラフが切れている」):
        # bull_ratio/bear_ratioはmeaningful(AI分析済み)行のみを対象に計算する
        # ため、投稿(post_count)自体は届いていてもAI分析が追いついていない
        # バケットはNoneになりうる(analyze.pyは1サイクルあたり240秒の予算制で
        # 残りを次回runへ持ち越す設計・システム停止でなくても正常に起こりうる)。
        # connectgaps=Trueで、そうした一時的な欠測点をまたいで前後の実測値
        # 同士を線で結ぶ(値を捏造するのではなく、単に描画上ギャップを飛び越える
        # だけ・該当点はNoneのまま=ホバー等では値が無いことがわかる)。
        f.add_trace(go.Scatter(x=date_labels, y=bulls, name="強気", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["red"], width=3)), row=1, col=1)
        f.add_trace(go.Scatter(x=date_labels, y=bears, name="弱気", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["blue"], width=3)), row=1, col=1)
        f.add_trace(go.Scatter(x=date_labels, y=neutrals, name="中立", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["grey"], width=3, dash="dot")), row=1, col=1)
        f.add_trace(go.Bar(x=date_labels, y=posts, marker_color=COL["muted"],
                           showlegend=False), row=2, col=1)
        f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        height=270, margin=dict(l=8, r=8, t=36, b=8),
                        font=dict(color=COL["text"]),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="left", x=0))
        f.update_xaxes(type="category", row=1, col=1)
        f.update_xaxes(type="category", row=2, col=1)
        f.update_yaxes(tickformat=".0%", gridcolor=COL["border"], row=1, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
        st.plotly_chart(f, width="stretch")
        _outlier_caption(date_labels, posts)
    else:
        st.line_chart({"強気": bulls, "弱気": bears})


def _today_time_buckets():
    """★2026-08-20追加(ユーザー指摘「本日の推移のチャートの幅が市場の始まり時に
    広すぎる」への対応)。本日想定される10分足バケットの全時刻ラベル("HH:MM")を、
    東証立会時間(前場9:00-11:30・後場12:30-15:30、間の昼休みは除外)ぶん固定順で
    生成する。intraday_today_seriesの10分バケットの切り方(分を10で切り捨て)と
    同じ規則。

    背景: Plotlyのcategory軸はデフォルトで「その瞬間に実際に存在するカテゴリ数」
    からバー幅を自動計算するため、寄り直後で点が1〜2個しか無い時間帯はローソク足が
    不自然に太く描画され、データが増えるにつれ細くなっていく(=1日を通して見た目の
    幅が一定しない)。x軸のcategoryarrayをこの「1日分の想定バケット数」で固定して
    渡すことで、寄り直後から終値時点と同じ狭い幅で描画されるようにする(空いている
    右側は単に空白として残る=一般的なリアルタイムチャートと同じ見え方)。

    ★2026-08-20追記(ユーザー指摘「最後の10分のチャートが表示されていない」への
    対応): 後場終了を当初15:20までしか含めていなかった。これは元々Yahoo日中足
    経由(intraday_today_series)の設計を前提にしており、そちらは引け直後の単独
    現在値バーを直前バケットへ吸収する仕組みがあるため実質15:20が最後だった。
    しかし、その後kabuティック経由(live_price_bridge.py・kabu_tick_today_summary)
    に切り替えたことで、この吸収ロジックが無いまま独立した15:30バケット(実出来高
    ありの正当なデータ)がそのまま残るようになり、固定x軸range(この関数の要素数を
    基準)の外側に押し出されて非表示になっていた。後場終了を15:30まで拡張する。

    ★2026-08-20再追記(ユーザー指示「本日の推移のチャートは、昼休みは詰めて表示
    しましょう」): 前場終了を当初11:30まで含めていたが、実データ(2026-08-20の
    ticks_285A_*.csv)で実測したところ、前引け(11:30)ちょうどに乗る独立ティックは
    観測されず、最後の前場バケットは常に11:20だった(後場引け15:30とは非対称=
    後場引けは実際に15:30:00の出来高付きティックが記録される一方、前引けは
    11:29台までで記録が止まる)。そのため固定categoryarrayに"11:30"という
    常に空のカテゴリ枠が1つ挟まり、そのぶんだけ昼休みの隙間が広く見えていた。
    前場終了を11:20までに縮め、この空き枠を無くす。万一将来11:30に実データが
    現れた場合に備え、呼び手側(_effective_today_buckets)で実データにしか無い
    カテゴリも動的に合成するため、この関数自体を万能の正としない(=このテンプレは
    「通常時の想定」であり、実データが優先される設計)。
    """
    labels = []
    for start_min, end_min in ((9 * 60, 11 * 60 + 20), (12 * 60 + 30, 15 * 60 + 30)):
        for m in range(start_min, end_min + 1, 10):
            labels.append(f"{m // 60:02d}:{m % 60:02d}")
    return labels


_TODAY_TIME_BUCKETS = _today_time_buckets()
_TODAY_TIME_BUCKETS_SET = set(_TODAY_TIME_BUCKETS)   # メンバーシップ判定の高速化用


def _effective_today_buckets(actual_times):
    """★2026-08-20追加(ユーザー指示「昼休みは詰めて表示」対応の一部)。
    _TODAY_TIME_BUCKETS(通常想定される固定テンプレ)と、実際に観測された時刻
    (actual_times)の和集合を時刻順で返す。本日のHH:MMラベルは日をまたがない
    範囲(9:00-15:30)にしか存在しないため、通常の文字列ソートがそのまま時刻順に
    一致する。テンプレに無い時刻(例:将来また前引けにティックが乗った日の
    "11:30")が実データ側にあっても、和集合を取ることで自動的にカテゴリへ
    追加され、2026-08-20に一度実際に起きた「固定rangeの外に実データが
    押し出されて非表示になる」事故(後場引け15:30が消えた件)を再発させない。
    """
    return sorted(set(_TODAY_TIME_BUCKETS) | set(actual_times or []))


def _today_time_buckets_60s():
    """★2026-08-21追加(ユーザー依頼「板の買い・売り総計グラフの横軸は、市場が
    開いている時間としてください」)。_today_time_buckets()の1分粒度版。
    board_totals_60s_series()が"HH:MM"形式(60秒平均・分単位バケット)で出すのに
    合わせ、東証立会時間(前場9:00-11:20・後場12:30-15:30、間の昼休みは除外)ぶんを
    1分刻みで固定順生成する。前場終値側の境界(11:20まで)は10分足版と同じ実測
    根拠(_today_time_buckets()のdocstring参照)を踏襲する。
    ★2026-08-21修正(ユーザー指摘「横軸の秒の単位は不要」): ラベル形式を
    "HH:MM:SS"から"HH:MM"へ単純化(60秒バケットは分の境界へ切り捨てる設計のため
    秒の桁は常に"00"で情報量が無かった)。"""
    labels = []
    for start_min, end_min in ((9 * 60, 11 * 60 + 20), (12 * 60 + 30, 15 * 60 + 30)):
        for m in range(start_min, end_min + 1, 1):
            labels.append(f"{m // 60:02d}:{m % 60:02d}")
    return labels


_TODAY_TIME_BUCKETS_60S = _today_time_buckets_60s()
_TODAY_TIME_BUCKETS_60S_SET = set(_TODAY_TIME_BUCKETS_60S)   # メンバーシップ判定の高速化用


def _effective_board_totals_buckets(actual_times):
    """★2026-08-21追加。_effective_today_buckets()の60秒粒度版(_TODAY_TIME_BUCKETS_60S
    との和集合)。同じ理由(固定rangeの外に実データが押し出されて非表示になる事故の
    再発防止)でテンプレと実データを合成する。"""
    return sorted(set(_TODAY_TIME_BUCKETS_60S) | set(actual_times or []))


# ============================================================================
# ★2026-08-19追加(ユーザー依頼): 当日の価格推移とセンチメント推移(イントラデイ)
# ============================================================================
def _intraday_today_charts(rec, live_price=None, sentiment_24h_remote=None,
                           board_totals_remote=None):
    intraday = rec.get("intraday_today") or {}
    price_pts = intraday.get("price") or []
    # ★2026-08-20変更(ユーザー指示「本日のセンチメント推移は、過去24時間の10分毎の
    # センチメントの推移に」): 従来はintraday_today['sentiment'](本日暦日ぶんのみ・
    # snapshot生成の実際の間隔のまま)を使っていたが、sentiment_last_24h
    # (public_export.sentiment_last_24h_10min()・過去24時間を10分刻みにリサンプル
    # 済み)へ切り替える。intraday_today_series()側の'sentiment'計算自体は既存の
    # selftestが対象にしているため削除しない(未使用のまま残す)。
    # ★2026-08-20緊急追加: sentiment_last_24hはセル上限超過を避けるため専用タブ
    # (sentiment_24h)へ分離した(config.PUBLIC_SENTIMENT_24H_SOURCE_URLのdocstring
    # 参照)。取得できていればそちらを優先し、未設定/失敗時のみrec直下の
    # sentiment_last_24h(ローカル直接読み時や旧同期が残っている場合)を使う。
    if sentiment_24h_remote and sentiment_24h_remote.get("sentiment_last_24h"):
        sent_pts = sentiment_24h_remote["sentiment_last_24h"]
    else:
        sent_pts = rec.get("sentiment_last_24h") or []
    # ★2026-08-21追加(ユーザー依頼「過去24時間センチメント推移を『本日の
    # センチメント推移』に変更。本日の価格推移・板の買い・売り総計のグラフと
    # 横軸が合うように」続けて「その下に、過去24時間センチメント推移を残して
    # ください」)。★2026-08-21同日中に是正(ユーザー指摘「投稿量は取得時刻でなく
    # 投稿時刻でならすことにしたはずです」): 上のsent_pts(過去24時間ぶん・
    # sentiment_last_24h_10min()が投稿自身のtsでバケット化済み)から本日ぶんだけを
    # 抜き出す(public_export.sentiment_today_from_last_24h)。初回実装は
    # intraday_today['sentiment'](snapshot実行=取得時刻基準)をリサンプルする方式
    # だったが、収集側の一括バックログ取得(実例=本日12:37のYahoo 19,801件一括
    # 取得)で投稿量が1バケットへ跳ね上がる、2026-08-20に一度是正済みだったのと
    # 同種の不具合を再導入していたため、既に投稿時刻ベースで正しく計算済みの
    # sent_pts を再利用する設計へ変更した。
    today_sent_pts = public_export.sentiment_today_from_last_24h(sent_pts)
    # ★2026-08-21修正(ユーザー指摘「横軸は市場が開いている時間＆昼休みは抜く。
    # 本日の価格推移の市場の空いている時間に合わせる、と書いたでしょ」): sent_pts
    # は暦日24時間ぶん(取引時間外も含む)のため、板総計チャート(_board_totals_chart)
    # と同じ設計で、東証立会時間の固定テンプレ(_TODAY_TIME_BUCKETS)に無い時刻の
    # 点は明示的に除外する。
    today_sent_pts = [p for p in today_sent_pts if p.get("time") in _TODAY_TIME_BUCKETS_SET]
    # ★2026-08-20追加(ユーザー指示「本日の推移の株価も60秒毎に最新値に」)。
    # live_price(kabuティックから60秒毎に生成)に本日の10分足系列があれば、
    # Sheets由来(最大10分古い)のものより優先して丸ごと差し替える。センチメント
    # 系列はkabu側に無いのでrec由来のまま(価格だけを新鮮に保つ)。
    if live_price and live_price.get("intraday_today_price"):
        price_pts = live_price["intraday_today_price"]
    if not price_pts and not sent_pts and not today_sent_pts:
        return   # データ蓄積中(場が始まったばかり等)は静かに省略・エラーにしない

    # ★2026-08-21: 価格チャートと「本日のセンチメント推移」チャートの横軸を
    # 厳密に揃えるため、_effective_today_buckets()を両者共通で1回だけ計算する。
    # 和集合の対象は価格の実データ時刻のみ(price_ptsは元々取引時間中しか存在
    # しないため安全に和集合できる・_effective_today_bucketsのdocstring=固定
    # rangeの外に実データが押し出されて非表示になる事故の再発防止)。
    # today_sent_ptsは含めない——上でTODAY_TIME_BUCKETS_SETへ既に絞り込み済み
    # なのでテンプレの範囲内に収まっているが、万一の取引時間外データ混入で
    # 横軸(昼休み等)が広がってしまうリスクを断つため、あえて和集合の対象にしない。
    _effective_buckets = _effective_today_buckets(
        [p.get("time") for p in price_pts])
    _n_buckets = len(_effective_buckets)

    # ★2026-08-21修正(ユーザー依頼「本日の価格推移と板のグラフを上下に並べる
    # ようにしてください」続けて「センチメントはその下に」): 従来はcol1(価格推移)/
    # col2(センチメント推移)の左右2列だったが、価格推移→板総計→センチメント
    # 推移の縦一列(全幅)へ変更する。板グラフは同じ「本日のイントラデイ」時間軸を
    # 扱う点で価格推移と関連が深く、上下に並べることで見比べやすくなる。
    st.markdown("#### 📅 本日の推移（イントラデイ）")

    st.markdown("**本日の価格推移（10分足ローソク足・出来高）**")
    # ★2026-08-21追加(ユーザー依頼「本日の価格推移のところに、最高値、最安値を
    # 書くようにしましょう」)。price_pts(10分足)のprice_high/price_lowから本日の
    # 高値/安値を拾って見出し直下に表示する(public_export.intraday_today_high_low・
    # データが無ければ何も表示しない=捏造しない)。
    _day_high, _day_low = public_export.intraday_today_high_low(price_pts)
    if _day_high is not None and _day_low is not None:
        st.caption(f"本日の高値 {_day_high:,.0f}円 ／ 安値 {_day_low:,.0f}円")
    if not price_pts:
        st.caption("本日の価格データ蓄積中です。")
    elif HAS_PLOTLY:
        # ★2026-08-19: ユーザー依頼「価格推移をローソク足にしてほしい」を受け
        # Scatter(終値の折れ線)からCandlestickへ変更(14日足チャートと同じ配色)。
        times = [p.get("time") for p in price_pts]
        opens = [p.get("price_open") for p in price_pts]
        closes = [p.get("price_close") for p in price_pts]
        vols = [p.get("price_volume") for p in price_pts]
        # ★2026-08-19: ユーザー依頼「価格推移に出来高を足せますか」対応。
        # 14日チャートと同じ設計(candlestick+volume bar・shared_xaxes・
        # 陽線/陰線と同じ配色)をイントラデイにも適用。
        vol_colors = [COL["grey"] if (o is None or c is None) else
                     (COL["red"] if c >= o else COL["blue"])
                     for o, c in zip(opens, closes)]
        f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                          row_heights=[0.7, 0.3], vertical_spacing=0.04)
        f.add_trace(go.Candlestick(
            x=times, open=opens,
            high=[p.get("price_high") for p in price_pts],
            low=[p.get("price_low") for p in price_pts],
            close=closes,
            increasing_line_color=COL["red"], decreasing_line_color=COL["blue"]),
            row=1, col=1)
        f.add_trace(go.Bar(x=times, y=vols, marker_color=vol_colors,
                           showlegend=False), row=2, col=1)
        f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        height=270, margin=dict(l=8, r=8, t=8, b=8),
                        font=dict(color=COL["text"]), xaxis_rangeslider_visible=False,
                        showlegend=False)
        # ★2026-08-20: ユーザー指摘「本日の推移のチャートの幅が市場の始まり時に
        # 広すぎる」対応。categoryarrayだけでは並び順が固定されるだけで、表示範囲
        # (ズーム)は依然として実際に存在するデータ点数へ自動追従してしまう
        # (実測: 寄り直後・1点しか無い時にxaxis.rangeが[-0.5,0.5]=1カテゴリぶんに
        # 自動収縮し、その1本のローソク足がプロット全幅を占めていた)。
        # autorange=False + 1日分の想定カテゴリ数ぶんの固定range を明示することで、
        # データが少ない寄り直後から終値時点と同じ幅で描画されるようにする
        # (右側の空白はデータ蓄積中として自然に残る)。
        # ★2026-08-20: 固定テンプレ(_TODAY_TIME_BUCKETS)だけでなく実データの
        # 時刻も和集合した「有効バケット列」を使う(_effective_today_bucketsの
        # docstring参照・2026-08-20の15:30消失事故の再発防止)。★2026-08-21:
        # _effective_buckets/_n_bucketsは関数冒頭で共通計算済み(本日センチメント
        # チャートと横軸を揃えるため)のためここでは再計算しない。
        f.update_xaxes(type="category", categoryorder="array",
                       categoryarray=_effective_buckets,
                       autorange=False, range=[-0.5, _n_buckets - 0.5], row=1, col=1)
        f.update_xaxes(type="category", categoryorder="array",
                       categoryarray=_effective_buckets,
                       autorange=False, range=[-0.5, _n_buckets - 0.5], row=2, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",", row=1, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
        st.plotly_chart(f, width="stretch")
    else:
        st.line_chart({"終値": [p.get("price_close") for p in price_pts]})

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _board_totals_chart(board_totals_remote)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    # ★2026-08-21追加(ユーザー依頼「過去24時間センチメント推移を『本日の
    # センチメント推移』に変更。本日の価格推移・板の買い・売り総計のグラフと
    # 横軸が合うように」続けて「本日のセンチメント推移の横軸は本日の価格推移の
    # 市場の空いている時間に合わせる」)。★2026-08-21再修正(ユーザー依頼「本日の
    # センチメント推移と板の買い・売り総計の上下を入れ替えてください」): 価格
    # 推移→板の買い・売り総計→本日のセンチメント推移→過去24時間のセンチメント
    # 推移の順へ変更(横軸を揃える設計自体=_effective_buckets共有は無変更)。
    st.markdown("**本日のセンチメント推移（10分毎・強気/弱気比率・投稿量）**")
    if not today_sent_pts:
        st.caption("本日のセンチメントデータ蓄積中です。")
    elif HAS_PLOTLY:
        _t_times = [p.get("time") for p in today_sent_pts]
        _t_bulls = [p.get("bull_ratio") for p in today_sent_pts]
        _t_bears = [p.get("bear_ratio") for p in today_sent_pts]
        _t_posts = [p.get("post_count") for p in today_sent_pts]
        f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                          row_heights=[0.72, 0.28], vertical_spacing=0.04)
        # ★2026-08-21追加(ユーザー指摘「投稿量はあるのにグラフが切れている」):
        # bull_ratio/bear_ratioはmeaningful(AI分析済み)行のみを対象に計算する
        # ため、投稿(post_count)自体は届いていてもAI分析が追いついていない
        # バケットはNoneになりうる(analyze.pyは1サイクルあたり240秒の予算制で
        # 残りを次回runへ持ち越す設計・システム停止でなくても正常に起こりうる)。
        # connectgaps=Trueで一時的な欠測点をまたいで前後の実測値同士を線で結ぶ
        # (値を捏造するのではなく描画上ギャップを飛び越えるだけ)。
        f.add_trace(go.Scatter(x=_t_times, y=_t_bulls, name="強気", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["red"], width=3)), row=1, col=1)
        f.add_trace(go.Scatter(x=_t_times, y=_t_bears, name="弱気", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["blue"], width=3)), row=1, col=1)
        f.add_trace(go.Bar(x=_t_times, y=_t_posts, marker_color=COL["muted"],
                           showlegend=False), row=2, col=1)
        f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        height=270, margin=dict(l=8, r=8, t=36, b=8),
                        font=dict(color=COL["text"]),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="left", x=0))
        # ★価格チャートと厳密に同じcategoryarray/rangeを使い横軸を揃える
        # (関数冒頭で共通計算した_effective_buckets/_n_buckets。ユーザー依頼
        # 「本日の価格推移・板の総計のグラフと横軸が合うように」)。
        f.update_xaxes(type="category", categoryorder="array",
                       categoryarray=_effective_buckets,
                       autorange=False, range=[-0.5, _n_buckets - 0.5], row=1, col=1)
        f.update_xaxes(type="category", categoryorder="array",
                       categoryarray=_effective_buckets,
                       autorange=False, range=[-0.5, _n_buckets - 0.5], row=2, col=1)
        f.update_yaxes(tickformat=".0%", gridcolor=COL["border"], row=1, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
        st.plotly_chart(f, width="stretch")
        _outlier_caption(_t_times, _t_posts)
    else:
        st.line_chart({"強気": [p.get("bull_ratio") for p in today_sent_pts]})

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    # ★2026-08-20変更(ユーザー指示「本日のセンチメント推移は、過去24時間の
    # 10分毎のセンチメントの推移に」)。★2026-08-21追加(ユーザー指示「その下に、
    # 過去24時間センチメント推移を残してください」): 上の「本日の」チャート
    # (価格/板と横軸を揃えた版)とは別に、従来どおりのローリング24時間窓の
    # チャートもこの下に残す(削除ではなく追加・置換ではない)。
    st.markdown("**過去24時間のセンチメント推移（10分毎・強気/弱気比率・投稿量）**")
    if not sent_pts:
        st.caption("センチメントデータ蓄積中です。")
    elif HAS_PLOTLY:
        times = [p.get("time") for p in sent_pts]
        bulls = [p.get("bull_ratio") for p in sent_pts]
        bears = [p.get("bear_ratio") for p in sent_pts]
        # ★2026-08-20: ユーザー依頼「センチメント推移のグラフに、投稿量の棒
        # グラフを足せますか」対応(14日チャートの本日版と同じ2段組design)。
        posts = [p.get("post_count") for p in sent_pts]
        # ★2026-08-21追加(ユーザー指摘「投稿量はあるのにグラフが切れている」・
        # 本日のセンチメント推移と同じ理由=meaningful分析が投稿収集に対して
        # 一時的に遅れているだけでシステム停止ではない): connectgaps=Trueで
        # 一時的な欠測点をまたいで前後の実測値同士を線で結ぶ。
        f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                          row_heights=[0.72, 0.28], vertical_spacing=0.04)
        # ★2026-08-20: ユーザー依頼「線を太く」「凡例が横軸表記にかぶらない
        # ように」に対応(14日チャートの本日版と同じ設計)。★2026-08-21: ユーザー
        # 依頼「線の太さを3に」を受け幅3へ統一(従来は5)。
        f.add_trace(go.Scatter(x=times, y=bulls, name="強気", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["red"], width=3)), row=1, col=1)
        f.add_trace(go.Scatter(x=times, y=bears, name="弱気", mode="lines",
                               connectgaps=True,
                               line=dict(color=COL["blue"], width=3)), row=1, col=1)
        f.add_trace(go.Bar(x=times, y=posts, marker_color=COL["muted"],
                           showlegend=False), row=2, col=1)
        f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        height=270, margin=dict(l=8, r=8, t=36, b=8),
                        font=dict(color=COL["text"]),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="left", x=0))
        # ★2026-08-20追加: timeのラベルが"M/D HH:MM"形式(過去24時間窓は暦日を
        # またぐため日付を含む)になり、"/"と":"を含む文字列をPlotlyが日付型と
        # 誤認識し得る(14日チャートの日付表記で過去に一度実際に発生した誤認識と
        # 同型のリスク)。明示的にcategory型にして誤認識・表記崩れを防ぐ。
        f.update_xaxes(type="category", row=1, col=1)
        f.update_xaxes(type="category", row=2, col=1)
        f.update_yaxes(tickformat=".0%", gridcolor=COL["border"], row=1, col=1)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
        st.plotly_chart(f, width="stretch")
        _outlier_caption(times, posts)
    else:
        st.line_chart({"強気": [p.get("bull_ratio") for p in sent_pts],
                      "弱気": [p.get("bear_ratio") for p in sent_pts]})


# ★2026-08-20追加(ユーザー提案「AI考察の前回比較を視覚的なバッジでも」)。
# AI考察本文は前回比較を文章で触れているが、文中に埋もれて読み飛ばされやすいため、
# 本文の直前にst.metricのdelta表示(▲▼＋色)で価格・強気/弱気比率を並べる。
# データ源はpublic_export.previous_deltas(rec)(前回スナップショットが無ければ
# 全項目Noneで返る=st.metricはdelta=Noneの時、矢印無しの通常表示になるので
# 自然にフォールバックする)。
def _previous_deltas_row(rec):
    d = public_export.previous_deltas(rec)
    if not d.get("previous_generated_at"):
        return
    price = rec.get("price") or {}
    board = rec.get("board") or {}
    c1, c2, c3 = st.columns(3)
    with c1:
        last = price.get("last")
        st.metric("株価", f"{last:,.0f}円" if last is not None else "—",
                  delta=(f"{d['price_last']:+.0f}円" if d["price_last"] is not None else None))
    with c2:
        b = board.get("bull_ratio")
        st.metric("強気比率", f"{b:.1%}" if b is not None else "—",
                  delta=(f"{d['bull_ratio']*100:+.1f}pt" if d["bull_ratio"] is not None else None))
    with c3:
        r = board.get("bear_ratio")
        st.metric("弱気比率", f"{r:.1%}" if r is not None else "—",
                  delta=(f"{d['bear_ratio']*100:+.1f}pt" if d["bear_ratio"] is not None else None),
                  delta_color="inverse")
    st.caption(f"前回集計({d.get('previous_generated_at')})との比較")


# ============================================================================
# (g) AI考察
# ============================================================================
def _ai_commentary(rec):
    ac = rec.get("ai_commentary")
    st.markdown("#### 🤖 AI考察")
    _previous_deltas_row(rec)
    if not ac or not ac.get("text"):
        st.caption("AI考察は準備中です。")
        return
    st.markdown(
        f"<div style='background:{COL['panel']};border:1px solid {COL['border']};"
        f"border-radius:8px;padding:14px;color:{COL['text']};line-height:1.7'>"
        f"{ac['text']}</div>", unsafe_allow_html=True)
    st.caption(f"生成時刻: {ac.get('generated_at', '不明')}")


# ============================================================================
# ★2026-08-20追加(ユーザー提案「PTS/ADR情報の専用カード化」)。従来は
# rec['extended_hours']をAI考察の本文(文章の一部)にしか使っておらず、翌営業日の
# 値動きを気にする閲覧者が見つけにくかった。ヘッダー直下に小さな専用カードとして
# 独立表示する(値が無い項目[休場・未取得等]は自然に非表示・fail-soft)。
# ============================================================================
def _extended_hours_card(rec):
    eh = rec.get("extended_hours") or {}
    pts = eh.get("pts")
    adr = eh.get("adr")
    if not pts and not adr:
        return
    cols = st.columns(2)
    if pts:
        with cols[0]:
            chg = pts.get("change_pct")
            chg_text = f"{chg:+.2f}%" if chg is not None else None
            st.metric(f"PTS（{pts.get('time', '')}）",
                      f"{pts['price']:,.0f}円" if pts.get("price") is not None else "—",
                      delta=chg_text)
    if adr:
        with cols[1]:
            chg = adr.get("change_pct")
            chg_text = f"{chg:+.2f}%" if chg is not None else None
            price_text = (f"{adr['price_yen']:,.0f}円"
                         if adr.get("price_yen") is not None else "—")
            st.metric(f"米国ADR（{adr.get('time', '')}）", price_text, delta=chg_text)
    st.caption("PTS=東証取引時間外の私設取引システム／ADR=米国預託証券。"
               "翌営業日の値動きの参考情報です(それ自体が売買シグナルではありません)。")


# ============================================================================
# ★2026-08-21追加(ユーザー依頼「板の買い・売り総計(成行を含めた全価格帯)の推移を
# 折れ線グラフで」。おにや10:42投稿で仕様確定・トレPJ10:47投稿で記録側に
# over_sell_qty/under_buy_qty/market_sell_qty/market_buy_qtyの4列を追加・
# 2026-08-21 11:30以降反映)。board_totals_bridge.py(1分毎の独立プロセス)が
# public_export.board_totals_60s_series()で組み立てた60秒足系列を、
# board_totalsタブ経由で読む。データ未取得/空(記録拡張の反映前等)は
# チャート自体を静かに省略する(fail-soft)。
# ============================================================================
def _board_totals_chart(board_totals_remote):
    series = (board_totals_remote or {}).get("board_totals_60s") or []
    if not series:
        return   # データ蓄積中(記録拡張の反映前・休場等)は静かに省略

    # ★2026-08-21修正(ユーザー依頼「本日の価格推移と板のグラフを上下に並べる」):
    # _intraday_today_charts()内(価格推移とセンチメント推移の間)へ配置される
    # ようになったため、見出しレベルを独立セクション(####)から兄弟要素と同じ
    # 太字サブ見出しへ揃える。
    st.markdown("**📊 板の買い・売り総計(成行込み全価格帯・60秒平均)**")
    st.caption("表示10本の気配だけでなく、外側の気配(OVER/UNDER)と成行注文も"
               "含めた板全体の数量合計です。値は直近60秒間の平均。"
               "需給の偏りを直接示す指標ではありません。")
    # ★2026-08-21追加(ユーザー依頼「グラフの横軸は、市場が開いている時間として
    # ください」)。板CSVは基本的に取引時間中しか記録されないため通常は混入しないが、
    # 念のため東証立会時間の固定テンプレ(_TODAY_TIME_BUCKETS_60S)に無い時刻の点は
    # 明示的に除外する(万一の寄り前後データ混入を横軸に含めない)。
    series = [p for p in series if p.get("time") in _TODAY_TIME_BUCKETS_60S_SET]
    if not series:
        return   # 取引時間中の有効な点が無ければ静かに省略
    times = [p.get("time") for p in series]
    buys = [p.get("buy_total") for p in series]
    sells = [p.get("sell_total") for p in series]
    if HAS_PLOTLY:
        f = go.Figure()
        f.add_trace(go.Scatter(x=times, y=buys, name="買い総計", mode="lines",
                               line=dict(color=COL["red"], width=3)))
        f.add_trace(go.Scatter(x=times, y=sells, name="売り総計", mode="lines",
                               line=dict(color=COL["blue"], width=3)))
        f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                        height=270, margin=dict(l=8, r=8, t=36, b=8),
                        font=dict(color=COL["text"]),
                        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                   xanchor="left", x=0))
        # ★"HH:MM"形式の時刻ラベルは":"を含むためPlotlyが日付型と誤認識し得る
        # (14日/24時間チャートの日付表記で過去に一度実際に発生した誤認識と同型の
        # リスク)。明示的にcategory型にして誤認識・表記崩れを防ぐ。
        # ★2026-08-21: 横軸を東証立会時間の固定テンプレ(前場9:00-11:20・
        # 後場12:30-15:30)に揃える(_today_time_buckets()の10分足版と同じ設計)。
        # 固定categoryarray+autorange=Falseにすることで、データが少ない場中の
        # 早い時間帯でも1日を通じて幅が一定になり、かつ横軸が常に市場の開場時間
        # (寄り付き〜大引け)だけを表す。
        _effective_buckets = _effective_board_totals_buckets(times)
        _n_buckets = len(_effective_buckets)
        # ★2026-08-21追加(ユーザー指摘「板の買い・売り総計の横軸の時刻表記は、
        # 本日のセンチメント推移と合わせましょう」): データ自体は60秒刻み
        # (_effective_buckets=322カテゴリ)のまま変えないが、目盛りラベルは
        # 価格推移・本日のセンチメント推移と同じ10分刻み(_TODAY_TIME_BUCKETS)
        # だけを明示指定する。10分の各ラベルは60秒テンプレの部分集合として
        # 必ず存在するため、tickvalsに渡すだけでラベル位置は正しく揃う。
        f.update_xaxes(type="category", categoryorder="array",
                       categoryarray=_effective_buckets,
                       autorange=False, range=[-0.5, _n_buckets - 0.5],
                       tickmode="array", tickvals=_TODAY_TIME_BUCKETS,
                       ticktext=_TODAY_TIME_BUCKETS)
        f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s")
        st.plotly_chart(f, width="stretch", key="board_totals_chart")
        _outlier_caption(times, buys, "買い総計")
        _outlier_caption(times, sells, "売り総計")
    else:
        st.line_chart({"買い総計": buys, "売り総計": sells})


# ============================================================================
# ★2026-08-20追加(ユーザー提案「初見者向けの読み方ガイド」)。YouTube等からの
# 流入者が専門用語(灼熱メーター・BVP・9指標等)で迷わないよう、ページ冒頭に
# 折りたたみ式の全体像ガイドを置く(各指標個別の説明キャプションは既存だが、
# 「まずどこを見ればいいか」の道案内が無かった)。既定は閉じた状態(collapsed)
# =初見でなくリピーターの多くには不要な情報のため、常に開いて場所を取らない。
# ★2026-08-21拡充(ユーザー依頼「読み方ガイドをもう少し拡充」)。初版(5項目)は
# 実装当日の画面構成しかカバーしておらず、その後追加した機能(メーター推移
# スパークライン・9指標の状態変化通知・前回集計との差分表示・PTS/ADR専用
# カード・過去24時間センチメント推移・AI考察が9指標/レジームにも言及する
# 拡張)が未反映だった。ページ上から下への実際の並び順に沿って再構成し、
# 各要素を漏れなく説明する。
# ★2026-08-21同日中に再更新(ユーザー依頼「このダッシュボードの読み方、を
# 更新しましょう」): 「本日の推移」を4チャート縦一列(価格推移→板の買い・売り
# 総計→本日のセンチメント推移→過去24時間のセンチメント推移)へ再構成し、
# 「過去14日間の推移」も縦一列化した後の実際の並び順・項目数に合わせて
# ⑤〜⑦を再構成(旧⑤の1項目を⑤⑥⑦の3項目へ分割・以降の番号を1つずつ繰り下げ)。
# ============================================================================
def _reading_guide():
    with st.expander("📖 このダッシュボードの読み方（初めての方向け）"):
        st.markdown(
            "**① 株価ヘッダー**\n"
            "- 現在値は株取引プロジェクトが自己収集する285Aのkabu証券APIティック"
            f"データを{PUBLIC_DASHBOARD_AUTOREFRESH_SEC}秒毎に取得して表示しています"
            "(取得できない場合はYahoo Financeやスプレッドシート保存値へ自動的に"
            "切り替わります)。取引時間中に更新が止まっている可能性がある場合は"
            "「⚠️価格データの更新が…」という注意書きが表示されます。\n\n"
            "**② 🔥灼熱メーター／😱阿鼻叫喚メーター**\n"
            "- 掲示板の投稿内容から算出した「過熱度」「セリングクライマックス度」の"
            "合成指標です。値が高いほど投稿の偏り・熱量が大きいことを示しますが、"
            "売買のシグナルではありません。ゲージ下の小さな折れ線は直近日ごとの"
            "スコア推移(蓄積前は非表示)です。\n\n"
            "**③ ボラ・レジーム帯**\n"
            "- 現在の値動きの荒さ(ボラティリティ)が「平穏〜急変」のどの水準に"
            "あるかの目安です。データ蓄積初期は「較正中(calibrating)」と表示され、"
            "これは異常ではなく閾値を学習している途中であることを示します。\n\n"
            "**④ 🎯シグナル発火状況（9指標）**\n"
            "- 投稿の偏り・語彙・投稿量などを9つの観点で統計的にチェックした"
            "一覧です。🟢OK=平常範囲内／🟠警戒=やや偏りが大きい／🔴発火=閾値超過、"
            "を示す記述的なラベルであり、売買の推奨ではありません。前回の取引日から"
            "状態が変わった指標があれば、一覧の直前に「📌前回の取引日からの状態変化」"
            "として自動的に強調表示されます。\n\n"
            "**⑤ 本日の推移（価格推移／板の買い・売り総計／本日のセンチメント推移）**\n"
            "- 3つのチャートを縦に並べており、横軸(時刻)は全て東証立会時間"
            "(前場9:00-11:20・後場12:30-15:30、昼休みは除外)で揃えているため"
            "同じ時刻の動きを上下で見比べられます。**価格推移**は10分足ローソク足"
            "＋出来高で、見出し下に本日の高値・安値も表示します。**板の買い・売り"
            "総計**は表示10本の気配だけでなく外側の気配(OVER/UNDER)・成行注文も"
            "含めた板全体の数量合計(60秒平均)で、需給の偏りを直接示す指標では"
            "ありません。**本日のセンチメント推移**は10分毎の強気/弱気比率・投稿量"
            "で、各投稿が実際に書き込まれた時刻を基準に集計しています(掲示板側から"
            "まとめて取得した時刻ではなく投稿自体のタイムスタンプを使用)。強気/弱気"
            "比率の線がまだAI分析が追いついていない箇所は、投稿量(棒グラフ)は"
            "あっても一時的に前後の値を結んで表示することがあります(システム停止"
            "ではなく分析処理の順番待ちです)。\n\n"
            "**⑥ 過去14日間の推移（価格推移／センチメント推移）**\n"
            "- 直近14営業日ぶんを日足で縦に並べたものです。センチメント推移は"
            "強気/弱気/中立の3本の比率と投稿量(棒グラフ)を表示します。\n\n"
            "**⑦ 過去24時間のセンチメント推移**\n"
            "- 暦日をまたいだ直近24時間ぶんを10分刻みで表示します(⑤の「本日の"
            "センチメント推移」が本日の立会時間だけに絞っているのに対し、こちらは"
            "前日夜間・寄り付き前も含むローリング窓です)。\n\n"
            "**⑧ PTS・米国ADR（表示される場合のみ）**\n"
            "- 東証の取引時間外の値動きです。PTS=私設取引システムでの夜間取引、"
            "ADR=米国預託証券(円換算)。あくまで翌営業日の値動きを見る上での参考"
            "情報であり、それ自体が値上がり/値下がりを予測するものではありません。\n\n"
            "**⑨ 🤖AI考察**\n"
            "- 上記の集計値(株価・掲示板の強弱比率・9指標の発火状況・ボラレジーム・"
            "前回取引日からの状態変化・PTS/ADR等)だけをもとにAIが生成した文章です。"
            "個別の投稿内容やユーザー名は一切含まれません。文章の上にある「前回集計"
            "との比較」の数値は、直近の自動更新1回分(数分程度)の短時間の変化です。\n\n"
            "本ダッシュボードは研究・エンタメ用途の情報提供であり、投資助言では"
            "ありません。最終的な投資判断はご自身の責任で行ってください。")


# ============================================================================
# (h) 免責事項
# ============================================================================
def _disclaimer(rec):
    st.markdown("---")
    st.caption(f"⚠️ {rec.get('disclaimer', public_export.DISCLAIMER)}")


# ============================================================================
# main
# ============================================================================
def main():
    st.set_page_config(page_title="掲示板投稿の詳細分析による投資情報",
                       page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    inject_css()
    # ★2026-08-19: dashboard.py の inject_css() は padding-top:1.1rem(≈17.6px)だが、
    # Streamlit標準ツールバー(「Deploy」ボタン等・高さ60px・固定表示)がその上に重なり、
    # このページの最初の要素である大きな見出し(このページ独自の1.6em/1.5emテキスト)の
    # 上部が隠れて見えてしまうユーザー報告(2026-08-19)を受けて追加。実測(ブラウザの
    # getBoundingClientRect)でヘッダーが33.6px〜74.5pxに描画されツールバー(0-60px)と
    # 重なっていることを確認済み。ツールバー分(60px)を明確に上回る余白を追加する。
    st.markdown("<style>.block-container{padding-top:4.5rem !important}</style>",
               unsafe_allow_html=True)

    if HAS_AUTOREFRESH:
        st_autorefresh(interval=PUBLIC_DASHBOARD_AUTOREFRESH_SEC * 1000, key="public_dash_auto")

    # ★2026-08-27追加(ユーザー依頼「ダッシュボードの一番上に、YouTubeのリンクと
    # アバターのイラストを入れて」)。アバター画像はEthan HuntStock2.jpg(透かし除去
    # 済み・[[reference-avatar-photo-has-grok-watermark]]参照)を`assets/avatar.jpg`
    # としてリポジトリへ同梱(Streamlit Cloudはリポジトリを丸ごとクローンするため、
    # スクリプトからの相対パスで読める)。
    col_avatar, col_link = st.columns([1, 15])
    with col_avatar:
        st.image("assets/avatar.jpg", width=40)
    with col_link:
        st.markdown(
            "<div style='display:flex;align-items:center;height:40px'>"
            "<a href='https://www.youtube.com/@EthanHuntStock' target='_blank' "
            "rel='noopener noreferrer' style='text-decoration:none'>"
            "📺 YouTube: @EthanHuntStock</a></div>",
            unsafe_allow_html=True)

    # ★2026-09-06追加(ユーザー依頼「モニターとダッシュボードの相互リンクを入れて」):
    # 姉妹プロジェクト「AIセクター ワールドモニター」(ai_sector_monitor・キオクシア285A
    # 売買判断支援)の公開静的サイトへの相互リンク。あちら側にも本ダッシュボードへの
    # 逆リンクを追加済み(ai_sector_monitor/templates/index.html・static_export.py)。
    st.markdown(
        "<a href='https://ethanhuntstock.github.io/ai-sector-monitor/' target='_blank' "
        "rel='noopener noreferrer' style='text-decoration:none;font-size:0.85em'>"
        "🔗 姉妹サイト: AIセクター ワールドモニター（キオクシア285A売買判断支援）</a>",
        unsafe_allow_html=True)

    # ★2026-08-19追加(ユーザー依頼「タイトルを最上部に書く」): ブラウザタブの
    # page_titleとは別に、ページ本文の最上部にも見出しとして明示する。
    st.markdown("### 📊 掲示板投稿の詳細分析による投資情報")
    _reading_guide()

    # ★2026-08-19追加(ユーザー依頼: Streamlit Community Cloudへデプロイ)。
    # クラウド環境ではローカルPCのdata/public_export/latest.jsonへ直接アクセス
    # できないため、config.PUBLIC_JSON_SOURCE_URL(env `BBS_PUBLIC_JSON_URL`)が
    # 設定されていればGoogle Sheets経由(public_sheets_sync.pyのjson_blobタブを
    # 「ウェブに公開」したCSV URL)でデータを読む。未設定(ローカル実行の既定)なら
    # 従来通りローカルファイルを直接読む。
    if config.PUBLIC_JSON_SOURCE_URL:
        rec = public_export.load_public_latest_from_url(config.PUBLIC_JSON_SOURCE_URL)
    else:
        rec = public_export.load_public_latest()
    if not rec:
        st.info("データ準備中です。しばらくしてから再度お試しください。")
        return

    # ★2026-08-20追加(ユーザー指示「公開版ではYahooでなく自己取得しているkabuの
    # データを使う」「トップの株価/本日の推移/過去14日間の株価が60秒ごとに最新値に
    # なるように」)。live_price_bridge.py(株取引API_プロト1のkabuティックCSVを
    # 1分毎に読んで生成)が書いたlive_priceタブを、60秒毎のオートリフレッシュの
    # たびに1回だけ取得し、ヘッダー・本日の推移・過去14日間の推移の3箇所へ
    # 使い回す(3箇所それぞれが個別にネットワーク往復しない)。未設定/失敗時は
    # 各関数内のフォールバック(Yahoo直接取得→Sheets由来のrec)へ順に落ちる。
    live_price = None
    if config.PUBLIC_LIVE_PRICE_SOURCE_URL:
        live_price = public_export.load_live_price_from_url(config.PUBLIC_LIVE_PRICE_SOURCE_URL)

    # ★2026-08-20緊急追加(ユーザー報告「過去24時間のセンチメント推移が表示されない」
    # への対応): sentiment_last_24hはセル上限超過を避けるためjson_blobから分離し
    # 専用タブ(sentiment_24h)へ書かれる設計に変更した(config.PUBLIC_SENTIMENT_24H_SOURCE_URL
    # のdocstring参照)。未設定/失敗時はrec['sentiment_last_24h']へフォールバックする
    # (_intraday_today_charts側で処理)。
    sentiment_24h_remote = None
    if config.PUBLIC_SENTIMENT_24H_SOURCE_URL:
        sentiment_24h_remote = public_export.load_sentiment_24h_from_url(
            config.PUBLIC_SENTIMENT_24H_SOURCE_URL)

    # ★2026-08-21追加(ユーザー依頼「板の買い・売り総計(成行込み全価格帯)の推移を
    # 折れ線グラフで」)。board_totals_bridge.pyが1分毎に書くboard_totalsタブを
    # live_price/sentiment_24hと同じCSVブリッジパターンで取得。
    board_totals_remote = None
    if config.PUBLIC_BOARD_TOTALS_SOURCE_URL:
        board_totals_remote = public_export.load_board_totals_from_url(
            config.PUBLIC_BOARD_TOTALS_SOURCE_URL)

    _header(rec, live_price)
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    top = st.columns([1.1, 1.1, 1.6])
    board = rec.get("board") or {}
    board_history_14d = rec.get("board_history_14d") or []
    with top[0]:
        _gauge(board.get("overheat_score") or 0, "🔥 灼熱メーター(過熱)",
              config.SIG_OVERHEAT_TH, [COL["green"], COL["yellow"], COL["orange"]])
        _meter_sparkline(board_history_14d, "overheat_score", COL["orange"])
    with top[1]:
        # ★2026-08-21修正(ユーザー指摘「タイトルが『阿鼻叫喚』になっているので
        # 『メーター』を足して」): 灼熱側は"🔥 灼熱メーター(過熱)"と"メーター"を
        # 含むのに対し、こちらは"メーター"が抜けていた表記不統一を是正。
        _gauge(board.get("capitulation_score") or 0, "😱 阿鼻叫喚メーター(セリクラ)",
              config.SIG_CAPITULATION_FIRE, [COL["green"], COL["orange"], COL["red"]])
        _meter_sparkline(board_history_14d, "capitulation_score", COL["red"])
    with top[2]:
        regime = rec.get("regime") or {}
        # dashboard.py の regime_band() は {"vol_regime_score":..., "vol_regime":...} を
        # 持つ dict を期待する(内部ダッシュボードでは signal_export/latest.json 相当)。
        # public_export側でも同じキー名に揃えてあるためそのまま渡せる。
        regime_band(regime)
        _extended_hours_card(rec)

    st.markdown("#### 📰 関連ニュース（直近24時間）")
    _news_summary(rec)

    st.markdown("#### 🎯 シグナル発火状況（9指標）")
    # ★2026-08-19追加(ユーザー依頼): 「発火」が何を意味するか一目でわかるよう説明を追加。
    st.caption(
        "掲示板の投稿を集計した9つの指標(過熱度・投稿量の偏り等)が、あらかじめ決めた"
        "統計的なしきい値を超えたかどうかを示す一覧です。「発火」は"
        "**売買のシグナルではなく**、「統計的に見て平常時より偏りが大きい状態」を"
        "示す記述的な警告ラベルです。🟢OK=平常範囲内　🟠警戒=やや偏りが大きい　"
        "🔴発火=しきい値超過(過熱・悲観が強い)。")
    _signal_changes_note(rec)
    _signal_list(rec)
    st.caption("※研究・エンタメ用途・未検証。売買シグナルではありません。"
               "掲示板は方向よりボラを予測する傾向が文献で報告されています"
               "(Antweiler & Frank, 2004)。")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    # ★2026-08-21修正(ユーザー依頼「本日の価格推移と板のグラフを上下に並べる」):
    # 板総計チャートは_intraday_today_charts()内部(価格推移とセンチメント推移の
    # 間)へ移動したため、board_totals_remoteを引数として渡す。単独呼び出しは廃止。
    _intraday_today_charts(rec, live_price, sentiment_24h_remote, board_totals_remote)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _price_and_sentiment_charts(rec, live_price)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _ai_commentary(rec)

    _disclaimer(rec)


if __name__ == "__main__":
    main()
