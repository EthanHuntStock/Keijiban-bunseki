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
    now_jst = dt.datetime.utcnow() + dt.timedelta(hours=9)
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


def _signal_list(rec):
    cards = rec.get("signal_cards") or []
    if not cards:
        st.caption("シグナルデータ蓄積中です。")
        return
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
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"border-bottom:1px solid {COL['border']};padding:6px 2px'>"
            f"<span style='color:{COL['text']}'>{name}"
            f"<br><span style='color:{COL['muted']};font-size:.8em;font-weight:normal'>{desc}</span>"
            f"</span>"
            f"<span style='color:{COL['muted']};font-size:.88em'>{c.get('note', '')}</span>"
            f"<span>{chip_html}</span>"
            f"</div>", unsafe_allow_html=True)


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
    def _mmdd(d):
        if not d or "-" not in d:
            return d
        parts = d.split("-")
        if len(parts) != 3:
            return d
        return f"{int(parts[1])}/{int(parts[2])}"
    date_labels = [_mmdd(d) for d in dates]
    closes = [p.get("price_close") for p in pss]
    bulls = [p.get("bull_ratio") for p in pss]
    bears = [p.get("bear_ratio") for p in pss]
    neutrals = [None if (b is None or r is None) else max(0.0, 1.0 - b - r)
               for b, r in zip(bulls, bears)]

    col1, col2 = st.columns(2)
    with col1:
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
                            height=310, margin=dict(l=8, r=8, t=8, b=8),
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
    with col2:
        st.markdown("**過去14日間のセンチメント推移（強気/弱気/中立比率・投稿量）**")
        if HAS_PLOTLY:
            # ★2026-08-20: ユーザー依頼「センチメント推移のグラフに、投稿量の棒
            # グラフを足せますか」対応。価格チャート(ローソク足+出来高)と同じ設計
            # (make_subplotsで2段・shared_xaxes)で、下段に投稿量の棒グラフを追加する。
            posts = [p.get("post_count") for p in pss]
            f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                              row_heights=[0.72, 0.28], vertical_spacing=0.03)
            # ★2026-08-20: ユーザー依頼「グラフの線を太く」「凡例が横軸表記に
            # かぶらないように」を受け、線幅を2→3(中立は1→1.5)へ太くし、
            # 凡例をプロット領域の"上"に明示配置(yanchor="bottom", y=1.02)して
            # 横軸ラベルとの重なりを避ける。上部余白(margin.t)もその分広げる。
            f.add_trace(go.Scatter(x=date_labels, y=bulls, name="強気", mode="lines",
                                   line=dict(color=COL["red"], width=5)), row=1, col=1)
            f.add_trace(go.Scatter(x=date_labels, y=bears, name="弱気", mode="lines",
                                   line=dict(color=COL["blue"], width=5)), row=1, col=1)
            f.add_trace(go.Scatter(x=date_labels, y=neutrals, name="中立", mode="lines",
                                   line=dict(color=COL["grey"], width=4, dash="dot")), row=1, col=1)
            f.add_trace(go.Bar(x=date_labels, y=posts, marker_color=COL["muted"],
                               showlegend=False), row=2, col=1)
            f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                            height=310, margin=dict(l=8, r=8, t=36, b=8),
                            font=dict(color=COL["text"]),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                       xanchor="left", x=0))
            f.update_xaxes(type="category", row=1, col=1)
            f.update_xaxes(type="category", row=2, col=1)
            f.update_yaxes(tickformat=".0%", gridcolor=COL["border"], row=1, col=1)
            f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
            st.plotly_chart(f, width="stretch")
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
    """
    labels = []
    for start_min, end_min in ((9 * 60, 11 * 60 + 30), (12 * 60 + 30, 15 * 60 + 30)):
        for m in range(start_min, end_min + 1, 10):
            labels.append(f"{m // 60:02d}:{m % 60:02d}")
    return labels


_TODAY_TIME_BUCKETS = _today_time_buckets()


# ============================================================================
# ★2026-08-19追加(ユーザー依頼): 当日の価格推移とセンチメント推移(イントラデイ)
# ============================================================================
def _intraday_today_charts(rec, live_price=None):
    intraday = rec.get("intraday_today") or {}
    price_pts = intraday.get("price") or []
    sent_pts = intraday.get("sentiment") or []
    # ★2026-08-20追加(ユーザー指示「本日の推移の株価も60秒毎に最新値に」)。
    # live_price(kabuティックから60秒毎に生成)に本日の10分足系列があれば、
    # Sheets由来(最大10分古い)のものより優先して丸ごと差し替える。センチメント
    # 系列はkabu側に無いのでrec由来のまま(価格だけを新鮮に保つ)。
    if live_price and live_price.get("intraday_today_price"):
        price_pts = live_price["intraday_today_price"]
    if not price_pts and not sent_pts:
        return   # データ蓄積中(場が始まったばかり等)は静かに省略・エラーにしない

    st.markdown("#### 📅 本日の推移（イントラデイ）")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**本日の価格推移（10分足ローソク足・出来高）**")
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
            _n_buckets = len(_TODAY_TIME_BUCKETS)
            f.update_xaxes(type="category", categoryorder="array",
                           categoryarray=_TODAY_TIME_BUCKETS,
                           autorange=False, range=[-0.5, _n_buckets - 0.5], row=1, col=1)
            f.update_xaxes(type="category", categoryorder="array",
                           categoryarray=_TODAY_TIME_BUCKETS,
                           autorange=False, range=[-0.5, _n_buckets - 0.5], row=2, col=1)
            f.update_yaxes(gridcolor=COL["border"], tickformat=",", row=1, col=1)
            f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
            st.plotly_chart(f, width="stretch")
        else:
            st.line_chart({"終値": [p.get("price_close") for p in price_pts]})
    with col2:
        st.markdown("**本日のセンチメント推移（強気/弱気比率・投稿量）**")
        if not sent_pts:
            st.caption("本日のセンチメントデータ蓄積中です。")
        elif HAS_PLOTLY:
            times = [p.get("time") for p in sent_pts]
            bulls = [p.get("bull_ratio") for p in sent_pts]
            bears = [p.get("bear_ratio") for p in sent_pts]
            # ★2026-08-20: ユーザー依頼「センチメント推移のグラフに、投稿量の棒
            # グラフを足せますか」対応(14日チャートの本日版と同じ2段組design)。
            posts = [p.get("post_count") for p in sent_pts]
            f = make_subplots(rows=2, cols=1, shared_xaxes=True,
                              row_heights=[0.72, 0.28], vertical_spacing=0.04)
            # ★2026-08-20: ユーザー依頼「線を太く」「凡例が横軸表記にかぶらない
            # ように」に対応(14日チャートの本日版と同じ設計)。
            f.add_trace(go.Scatter(x=times, y=bulls, name="強気", mode="lines",
                                   line=dict(color=COL["red"], width=5)), row=1, col=1)
            f.add_trace(go.Scatter(x=times, y=bears, name="弱気", mode="lines",
                                   line=dict(color=COL["blue"], width=5)), row=1, col=1)
            f.add_trace(go.Bar(x=times, y=posts, marker_color=COL["muted"],
                               showlegend=False), row=2, col=1)
            f.update_layout(paper_bgcolor=COL["panel"], plot_bgcolor=COL["panel"],
                            height=270, margin=dict(l=8, r=8, t=36, b=8),
                            font=dict(color=COL["text"]),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                       xanchor="left", x=0))
            f.update_yaxes(tickformat=".0%", gridcolor=COL["border"], row=1, col=1)
            f.update_yaxes(gridcolor=COL["border"], tickformat=",.2s", row=2, col=1)
            st.plotly_chart(f, width="stretch")
        else:
            st.line_chart({"強気": [p.get("bull_ratio") for p in sent_pts],
                          "弱気": [p.get("bear_ratio") for p in sent_pts]})


# ============================================================================
# (g) AI考察
# ============================================================================
def _ai_commentary(rec):
    ac = rec.get("ai_commentary")
    st.markdown("#### 🤖 AI考察")
    if not ac or not ac.get("text"):
        st.caption("AI考察は準備中です。")
        return
    st.markdown(
        f"<div style='background:{COL['panel']};border:1px solid {COL['border']};"
        f"border-radius:8px;padding:14px;color:{COL['text']};line-height:1.7'>"
        f"{ac['text']}</div>", unsafe_allow_html=True)
    st.caption(f"生成時刻: {ac.get('generated_at', '不明')}")


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

    # ★2026-08-19追加(ユーザー依頼「タイトルを最上部に書く」): ブラウザタブの
    # page_titleとは別に、ページ本文の最上部にも見出しとして明示する。
    st.markdown("### 📊 掲示板投稿の詳細分析による投資情報")

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

    _header(rec, live_price)
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    top = st.columns([1.1, 1.1, 1.6])
    board = rec.get("board") or {}
    with top[0]:
        _gauge(board.get("overheat_score") or 0, "🔥 灼熱メーター(過熱)",
              config.SIG_OVERHEAT_TH, [COL["green"], COL["yellow"], COL["orange"]])
    with top[1]:
        _gauge(board.get("capitulation_score") or 0, "😱 阿鼻叫喚(セリクラ)",
              config.SIG_CAPITULATION_FIRE, [COL["green"], COL["orange"], COL["red"]])
    with top[2]:
        regime = rec.get("regime") or {}
        # dashboard.py の regime_band() は {"vol_regime_score":..., "vol_regime":...} を
        # 持つ dict を期待する(内部ダッシュボードでは signal_export/latest.json 相当)。
        # public_export側でも同じキー名に揃えてあるためそのまま渡せる。
        regime_band(regime)

    st.markdown("#### 🎯 シグナル発火状況（9指標）")
    # ★2026-08-19追加(ユーザー依頼): 「発火」が何を意味するか一目でわかるよう説明を追加。
    st.caption(
        "掲示板の投稿を集計した9つの指標(過熱度・投稿量の偏り等)が、あらかじめ決めた"
        "統計的なしきい値を超えたかどうかを示す一覧です。「発火」は"
        "**売買のシグナルではなく**、「統計的に見て平常時より偏りが大きい状態」を"
        "示す記述的な警告ラベルです。🟢OK=平常範囲内　🟠警戒=やや偏りが大きい　"
        "🔴発火=しきい値超過(過熱・悲観が強い)。")
    _signal_list(rec)
    st.caption("※研究・エンタメ用途・未検証。売買シグナルではありません。"
               "掲示板は方向よりボラを予測する傾向が文献で報告されています"
               "(Antweiler & Frank, 2004)。")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _intraday_today_charts(rec, live_price)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _price_and_sentiment_charts(rec, live_price)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    _ai_commentary(rec)

    _disclaimer(rec)


if __name__ == "__main__":
    main()
