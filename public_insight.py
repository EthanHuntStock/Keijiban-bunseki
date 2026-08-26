# -*- coding: utf-8 -*-
"""
public_insight.py - 一般公開用(Googleサイト「掲示板の分析による投資情報」)AI考察生成。
public_export.py(Phase 1)の公開レコード(集計値のみ)から、一般公開の個人投資家向けの
短い考察文を生成する。既存の insight.py(柱1・おにや風の実況考察・別スキーマ・別台帳・
別読者=内部/研究用)には一切触れない、完全に独立した新規モジュール。

【最重要・絶対厳搭】個別投稿のテキスト・ユーザー名・投稿者ハッシュ・投稿ID等を
一切受け取れない設計にする。
  - build_public_insight_context() は引数を public_record(dict) 1個だけに限定する
    (*args/**kwargsも持たない)ことで、個別コメントの文字列やリストを渡す経路自体を
    構造的に無くす(呼び手の意図に関係なく、関数シグネチャ上そもそも渡せない)。
  - build_public_insight_context() は public_record から既知の集計フィールドだけを
    ホワイトリスト方式で抜き出す(dictをそのまま右から左へ流さない)。万一
    public_record 自体に未知のキーが紛れ込んでいても、ホワイトリストに無ければ
    プロンプトに載らない(public_export.validate_no_leak() と合わせた多重防御)。
  - generate_public_insight() の入力は「public_export.build_public_record() が作り、
    public_export.validate_no_leak() を通過済みの public record」のみを想定する
    (このモジュール自身はvalidate_no_leak()を呼ばない=呼び手[public_export.py]側の
    責務。ここでは構造的に個別データを受け取れない設計そのものが安全網)。

規律:
  - insight.py 自体は一切変更しない(新規追加のみ)。
  - analyze._get_client()(Anthropicクライアント、insight.pyと同じ共有基盤)を使う。
  - LLM 課金は generate_public_insight() を実際に呼んだ時だけ(public_export.py の
    --with-commentary 明示フラグ経由のみ・毎回自動生成はしない)。
  - 例外は fail-soft(呼び手の自動実行を止めない)。generate_public_insight() は
    失敗時に None を返す(insight.generate_insight() と異なり、テンプレ代替は持たない
    =このモジュールはLLM生成専用。非LLM代替が要る場合は呼び手[public_export.py]が
    ai_commentary=None のまま出力すればよい)。
  - ポータブルパス(config 経由)。

検証: `python public_insight.py`(PYTHONDONTWRITEBYTECODE=1)で純関数テスト全緑
(実 LLM 課金しない=モック)。
"""
import os
import inspect
import datetime as dt

import config
import public_export  # ★2026-08-21追加: market_session_label()(市場開場時間帯の判定・
                       # 純関数)を再利用するため。public_export.pyはpublic_insight.pyを
                       # importしない(循環importなし)。個別投稿データを扱わない純粋な
                       # 時刻判定ロジックのみ使うため、このモジュールの安全設計
                       # (個別投稿情報を受け取れない構造)には一切影響しない。


# ============================================================================
# システムプロンプト(安全設計の核心=想定読者・トーン方針を内包)
# ============================================================================
# ★2026-08-21追加(ユーザー依頼「公開ダッシュボードのAI考察では、日本市場の開場
#   時間帯を考慮した考察をするように」): 従来は集計時刻を渡すだけで、寄り付き前・
#   昼休み・取引終了後でも「現在の値動きは」のような現在進行形の書き方になりうる
#   構造的な穴があった。新設点(5)+render_public_prompt()の「市場の状態」セクション・
#   出力指示で対応(以下(6)へ既存(5)を繰り下げ)。
_PUBLIC_INSIGHT_SYS = (
    "あなたは日本株の掲示板センチメント集計データを解説する、一般公開向けの客観的な"
    "アナリストです。想定読者は一般の個人投資家です。次を絶対に守ります。\n"
    "(1) 本文の冒頭または末尾で、これが『掲示板投稿の集計センチメントデータに基づく"
    "分析であり、投資助言ではない』旨を明示する。\n"
    "(2) 個別の投稿内容・ユーザー名・投稿者IDには一切言及しない(そもそも与えられる"
    "入力は集計数値だけであり、個別投稿の情報は渡されていない)。\n"
    "(3) 『買い時/売り時/上がる/下がる/仕込め/利確しろ』等、断定的な将来予測や"
    "煽り表現、売買の推奨・指示は一切書かない。\n"
    "(4) 与えられた集計数値(価格・投稿量・強気/弱気比率・過熱度・9指標のシグナル"
    "発火状況・ボラ・レジーム帯等、ダッシュボードに表示されている集計値全般)に"
    "基づく、事実ベースの客観的な記述に徹する。\n"
    "(4b) ★最重要: 各項目の数値を順番に言い換えるだけの『報告』ではなく、複数の"
    "指標を関連付けて意味を読み解く『分析』を書く。例えば――強気比率が高いのに"
    "過熱度スコアは低い(または逆)のような一見ちぐはぐな組み合わせがあればその"
    "理由を考える、投稿量の急増/急減とシグナル発火(ネームド集中・話題枯れ等)の"
    "背景にある投稿者心理を推測する、ボラ・レジーム帯の水準が現在のセンチメントの"
    "強さと整合しているか論じる、価格の動きと掲示板の反応のタイミングにズレが"
    "ないか見る、前回集計時点や直近の日次推移と比べて『何が変わったか』だけでなく"
    "『なぜ変わったと考えられるか』にまで踏み込む――といった視点で、データ同士の"
    "関係性を解釈する。ただし解釈はあくまで現状データの説明に留め、将来の値動きの"
    "断定的予測にはしない((3)と矛盾しない範囲で行う)。\n"
    "(5) 与えられた『市場の状態』(東証の開場時間帯)を踏まえた表現にする。取引時間内"
    "なら現在進行形で構わないが、寄り付き前・昼休み・取引終了後・休場日は、あたかも"
    "今まさに値動きが進行しているかのような書き方をせず、その時間帯の実態に即した"
    "表現(『前営業日の終値を基準に』『本日の終値は』等)にする。\n"
    "(6) 日本語で400〜600字程度でまとめる。項目の網羅より、(4b)の分析の質を優先する"
    "(全指標に触れる必要はない・意味のある関係性がある部分を掘り下げる)。"
)

# ★2026-08-19: ローカルLLM(lmstudio)経路専用のシステムプロンプト(おにや08:57投稿・
#   試作600字版がベース)。安全設計の(1)〜(4)はClaude版と全く同じ、(5)の目標文字数
#   だけ元は600字程度へ変更(ローカルLLMは無料のためClaude版より詳しめの分量で運用
#   する)。★2026-08-21: ユーザー依頼(「AI考察では、ダッシュボードに記載の情報に
#   対する考察もさせるように。シグナル発火状況とか。文字数は増えても構いません」)を
#   受け、(4)にシグナル発火状況・ボラレジーム帯を追加し(5)を800字程度へ引き上げ。
#   同日追加分: 「日本市場の開場時間帯を考慮した考察を」への対応で新設点(5)を追加、
#   旧(5)は(6)へ繰り下げ(_PUBLIC_INSIGHT_SYS側の変更理由コメント参照)。
_PUBLIC_INSIGHT_SYS_LOCAL = (
    "あなたは日本株の掲示板センチメント集計データを解説する、一般公開向けの客観的な"
    "アナリストです。想定読者は一般の個人投資家です。次を絶対に守ります。\n"
    "(1) 本文の冒頭または末尾で、これが『掲示板投稿の集計センチメントデータに基づく"
    "分析であり、投資助言ではない』旨を明示する。\n"
    "(2) 個別の投稿内容・ユーザー名・投稿者IDには一切言及しない(そもそも与えられる"
    "入力は集計数値だけであり、個別投稿の情報は渡されていない)。\n"
    "(3) 『買い時/売り時/上がる/下がる/仕込め/利確しろ』等、断定的な将来予測や"
    "煽り表現、売買の推奨・指示は一切書かない。\n"
    "(4) 与えられた集計数値(価格動向・投稿量・強気/弱気比率・過熱度スコア・"
    "総悲観度スコア・9指標のシグナル発火状況・ボラ・レジーム帯等、ダッシュボードに"
    "表示されている集計値全般)に基づく、事実ベースの客観的な記述に徹する。\n"
    "(4b) ★最重要: 各項目の数値を順番に言い換えるだけの『報告』ではなく、複数の"
    "指標を関連付けて意味を読み解く『分析』を書く。例えば――強気比率が高いのに"
    "過熱度スコアは低い(または逆)のような一見ちぐはぐな組み合わせがあればその"
    "理由を考える、投稿量の急増/急減とシグナル発火(ネームド集中・話題枯れ等)の"
    "背景にある投稿者心理を推測する、ボラ・レジーム帯の水準が現在のセンチメントの"
    "強さと整合しているか論じる、価格の動きと掲示板の反応のタイミングにズレが"
    "ないか見る、前回集計時点や直近の日次推移と比べて『何が変わったか』だけでなく"
    "『なぜ変わったと考えられるか』にまで踏み込む――といった視点で、データ同士の"
    "関係性を解釈する。ただし解釈はあくまで現状データの説明に留め、将来の値動きの"
    "断定的予測にはしない((3)と矛盾しない範囲で行う)。\n"
    "(5) 与えられた『市場の状態』(東証の開場時間帯)を踏まえた表現にする。取引時間内"
    "なら現在進行形で構わないが、寄り付き前・昼休み・取引終了後・休場日は、あたかも"
    "今まさに値動きが進行しているかのような書き方をせず、その時間帯の実態に即した"
    "表現(『前営業日の終値を基準に』『本日の終値は』等)にする。\n"
    "(6) 日本語で800字程度でまとめる。価格動向/投稿量/強気弱気比率/過熱度・"
    "総悲観度スコア/シグナル発火状況/ボラ・レジーム帯を機械的に全て列挙するのでは"
    "なく、(4b)の分析の質を優先し、意味のある関係性がある部分を掘り下げる。"
)


# ============================================================================
# 純関数: プロンプト用コンテキスト組み立て(引数は public_record 1個のみ)
# ============================================================================
def build_public_insight_context(public_record):
    """
    public_export.build_public_record() が作った公開レコード(dict)から、
    プロンプト用のコンテキストを組み立てる純関数。

    引数は public_record(dict)ただ1個のみ(*args/**kwargsも無い)。個別コメントの
    文字列やリストを受け取る経路が構造的に存在しない(呼び手がどう頑張っても
    個別データを渡せないシグネチャ)。

    既知の集計フィールドだけをホワイトリスト方式で抜き出す(dictをそのまま
    右から左へ流用しない)。public_record に未知のキーが紛れ込んでいても、
    ここで拾わない限りプロンプトには一切現れない。
    """
    r = public_record or {}
    price = r.get("price") or {}
    board = r.get("board") or {}
    pss = r.get("price_sentiment_series") or []
    trend = r.get("trend_14d") or []
    previous = r.get("previous")
    extended_hours = r.get("extended_hours")
    signal_cards = r.get("signal_cards") or []
    regime = r.get("regime")
    signal_state_changes = r.get("signal_state_changes") or []

    # ★2026-08-21追加(ユーザー依頼「公開ダッシュボードのAI考察では、日本市場の
    # 開場時間帯を考慮した考察をするように」)。generated_at(このレコードの集計時刻)
    # から市場の状態(寄り付き前/前場中/昼休み/後場中/取引終了後/休場)を判定する。
    # 「今この瞬間」ではなく「このレコードが集計された時点」を基準にすることで、
    # 実行タイミングに依存しない決定的な(再現可能な)値になる(他の全フィールドが
    # public_record由来の値である設計とも一貫する)。generated_atが無い/壊れている
    # 場合はNone(fail-soft・呼び手[render_public_prompt]側でセクションを省く)。
    generated_at_str = r.get("generated_at")
    market_session = None
    if generated_at_str:
        try:
            market_session = public_export.market_session_label(
                now=dt.datetime.fromisoformat(str(generated_at_str)[:19]))
        except (ValueError, TypeError):
            market_session = None

    return {
        "symbol": r.get("symbol"),
        "company_name": r.get("company_name"),
        "generated_at": r.get("generated_at"),
        "market_session": market_session,
        "price": {
            "last": price.get("last"),
            "change_pct": price.get("change_pct"),
        },
        "board": {
            "post_count_today": board.get("post_count_today"),
            "posts_per_hour": board.get("posts_per_hour"),
            "bull_ratio": board.get("bull_ratio"),
            "bear_ratio": board.get("bear_ratio"),
            "neutral_ratio": board.get("neutral_ratio"),
            "overheat_score": board.get("overheat_score"),
            "capitulation_score": board.get("capitulation_score"),
        },
        "price_sentiment_series": [
            {
                "date": p.get("date"),
                "price_close": p.get("price_close"),
                "bull_ratio": p.get("bull_ratio"),
                "bear_ratio": p.get("bear_ratio"),
            }
            for p in pss if isinstance(p, dict)
        ],
        "trend_14d": [
            {
                "date": t.get("date"),
                "post_count": t.get("post_count"),
                "bull_ratio": t.get("bull_ratio"),
                "bear_ratio": t.get("bear_ratio"),
            }
            for t in trend if isinstance(t, dict)
        ],
        # ★2026-08-19追加(ユーザー依頼「AI考察は前回からの変化に対する考察も入れる」)。
        # public_export.previous_snapshot_for_ai_commentary() が組み立てた、前回の
        # 公開レコード時点での軽量な比較スナップショット(集計値のみ)。無ければNone
        # (=初回生成等・呼び手[render_public_prompt]側で比較セクションを省く)。
        "previous": ({
            "generated_at": previous.get("generated_at"),
            "price_last": previous.get("price_last"),
            "change_pct": previous.get("change_pct"),
            "bull_ratio": previous.get("bull_ratio"),
            "bear_ratio": previous.get("bear_ratio"),
            "post_count_today": previous.get("post_count_today"),
        } if isinstance(previous, dict) else None),
        # ★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を
        # 分析するように。翌日の傾向につながる可能性がある」)。
        # public_export.extended_hours_summary() が組み立てた、TSE正規セッション後の
        # PTS(夜間取引)・米国ADR円換算の直近値(集計値のみ・個別投稿は元々含まれない)。
        # 無ければNone(=フィード未取得・取引が無い時間帯等・呼び手側でセクションを省く)。
        "extended_hours": ({
            "tse_close": (extended_hours.get("tse_close") if isinstance(extended_hours, dict) else None),
            "pts": (extended_hours.get("pts") if isinstance(extended_hours, dict) else None),
            "adr": (extended_hours.get("adr") if isinstance(extended_hours, dict) else None),
        } if isinstance(extended_hours, dict) else None),
        # ★2026-08-21追加(ユーザー依頼「AI考察では、ダッシュボードに記載の情報に対する
        # 考察もさせるように。シグナル発火状況とか」)。9指標カード(public_export.py
        # signal_cards・{name,value,threshold,state,note}の集計値のみ)をそのまま渡す。
        # 個別投稿情報は元々含まれないフィールドのため、他項目と同じホワイトリスト
        # 方式で1件ずつ明示的に抜き出す(dictをそのまま右から左へ流さない設計を維持)。
        "signal_cards": [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "threshold": c.get("threshold"),
                "state": c.get("state"),
                "note": c.get("note"),
            }
            for c in signal_cards if isinstance(c, dict)
        ],
        # ★2026-08-21追加: ボラ・レジーム帯(public_export.py regime・{vol_regime,
        # vol_regime_score,calibration_status}の集計値のみ)。
        "regime": ({
            "vol_regime": regime.get("vol_regime"),
            "vol_regime_score": regime.get("vol_regime_score"),
            "calibration_status": regime.get("calibration_status"),
        } if isinstance(regime, dict) else None),
        # ★2026-08-21追加: 前回取引日から発火状態が変化した指標(public_export.py
        # signal_state_changes・{name,from,to,compared_date}の集計値のみ)。無ければ空。
        "signal_state_changes": [
            {
                "name": c.get("name"),
                "from": c.get("from"),
                "to": c.get("to"),
                "compared_date": c.get("compared_date"),
            }
            for c in signal_state_changes if isinstance(c, dict)
        ],
    }


# 呼び手/テストがシグネチャを機械検証しやすいよう、引数名も固定しておく。
assert list(inspect.signature(build_public_insight_context).parameters) == ["public_record"]


def _fmt(v):
    return "不明" if v is None else v


def _fmt_pct(v):
    return "不明" if v is None else f"{v * 100:.1f}%"


def render_public_prompt(context, target_length="400〜600字程度"):
    """build_public_insight_context() の出力 -> ユーザープロンプト文字列(純関数)。
    system は _PUBLIC_INSIGHT_SYS(またはローカルLLM経路では_PUBLIC_INSIGHT_SYS_LOCAL)。
    個別投稿の情報は入力(context)に存在しないため、出力にも一切現れない。

    target_length: 出力の指示文に埋め込む目標文字数(既定はClaude経路向け)。
    ★2026-08-19: ローカルLLM経路(おにや08:57投稿の600字版プロンプト)向けに
    別の値を渡せるようパラメータ化。★2026-08-21: ユーザー依頼(「AI考察では、
    ダッシュボードに記載の情報に対する考察もさせるように。文字数は増えても
    構いません」)を受け、シグナル発火状況・ボラレジーム帯等のセクションを
    追加した分、既定値を200〜400字→400〜600字(Claude経路)・600字→800字
    (ローカルLLM経路)へ引き上げた。"""
    c = context or {}
    p = c.get("price") or {}
    b = c.get("board") or {}
    pss = c.get("price_sentiment_series") or []
    trend = c.get("trend_14d") or []

    L = []
    L.append(f"銘柄: {_fmt(c.get('symbol'))}({_fmt(c.get('company_name'))})")
    L.append(f"集計時刻: {_fmt(c.get('generated_at'))}")
    # ★2026-08-21追加(ユーザー依頼「日本市場の開場時間帯を考慮した考察をするように」)。
    market_session = c.get("market_session")
    if market_session:
        L.append(f"市場の状態(東証・集計時刻時点): {market_session}")
    L.append("")
    L.append("以下は、ある銘柄についての掲示板投稿を統計的に集計した数値データです。"
             "個別の投稿内容は一切含まれていません。これらの集計数値だけを根拠に、"
             "一般公開向けの客観的な考察文を書いてください。")
    L.append("")

    L.append("■ 価格(集計値)")
    L.append(f"  現在値: {_fmt(p.get('last'))} / 前日比: {_fmt(p.get('change_pct'))}%")
    L.append("")

    L.append("■ 本日の掲示板集計")
    L.append(f"  投稿数: {_fmt(b.get('post_count_today'))} / 投稿速度: {_fmt(b.get('posts_per_hour'))}/時")
    L.append(f"  強気比率: {_fmt_pct(b.get('bull_ratio'))} / 弱気比率: {_fmt_pct(b.get('bear_ratio'))}"
             f" / 中立比率: {_fmt_pct(b.get('neutral_ratio'))}")
    L.append(f"  過熱度スコア: {_fmt(b.get('overheat_score'))} / 総悲観度スコア: {_fmt(b.get('capitulation_score'))}")
    L.append("")

    # ★2026-08-21追加(ユーザー依頼「AI考察では、ダッシュボードに記載の情報に対する
    # 考察もさせるように。シグナル発火状況とか」)。ダッシュボードに既に表示されている
    # ボラ・レジーム帯/9指標カード/前回取引日からの状態変化を、AI考察の題材にも使う。
    regime = c.get("regime")
    if regime:
        L.append("■ ボラ・レジーム帯")
        if regime.get("calibration_status") == "calibrating" or regime.get("vol_regime_score") is None:
            L.append("  較正中(まだ判定に十分なデータが蓄積されていません)")
        else:
            L.append(f"  現在のレジーム: {_fmt(regime.get('vol_regime'))}"
                     f"(ボラティリティ指標BVP={_fmt(regime.get('vol_regime_score'))})")
        L.append("")

    signal_cards = c.get("signal_cards") or []
    if signal_cards:
        L.append("■ シグナル発火状況(9指標)")
        L.append("  掲示板の偏りを統計的にチェックした一覧。🟢OK=平常範囲内"
                 "　🟠警戒=やや偏りが大きい　🔴発火=しきい値超過。")
        for card in signal_cards:
            L.append(f"  ・{_fmt(card.get('name'))}: {_fmt(card.get('state'))}"
                     f"({_fmt(card.get('note'))})")
        L.append("")

    signal_changes = c.get("signal_state_changes") or []
    if signal_changes:
        compared_date = signal_changes[0].get("compared_date")
        L.append(f"■ 前回取引日({_fmt(compared_date)})からの状態変化")
        for ch in signal_changes:
            L.append(f"  ・{_fmt(ch.get('name'))}: {_fmt(ch.get('from'))} → {_fmt(ch.get('to'))}")
        L.append("")

    if pss:
        L.append("■ 直近の日次推移(価格終値・強気/弱気比率)")
        for row in pss:
            L.append(f"  {row.get('date')}: 終値={_fmt(row.get('price_close'))}"
                     f" 強気={_fmt_pct(row.get('bull_ratio'))} 弱気={_fmt_pct(row.get('bear_ratio'))}")
        L.append("")
    elif trend:
        L.append("■ 直近の日次推移(投稿量・強気/弱気比率)")
        for row in trend:
            L.append(f"  {row.get('date')}: 投稿数={_fmt(row.get('post_count'))}"
                     f" 強気={_fmt_pct(row.get('bull_ratio'))} 弱気={_fmt_pct(row.get('bear_ratio'))}")
        L.append("")

    # ★2026-08-19追加(ユーザー依頼「AI考察は前回からの変化に対する考察も入れる」)。
    # 前回(直近の公開更新サイクル・目安10分前)からの変化量を明示的に計算して渡す。
    # LLMに差分計算をさせず、こちらで確定値を計算してから渡すことで、誤った差分
    # (捏造・計算ミス)を防ぐ(与えられた数値をそのまま記述させるだけにする設計方針は
    # 他セクションと同じ)。
    prev = c.get("previous")
    if prev:
        def _delta(cur, old, fmt="{:+.2f}"):
            if cur is None or old is None:
                return "不明"
            return fmt.format(cur - old)
        cur_price = p.get("last")
        cur_bull = b.get("bull_ratio")
        cur_bear = b.get("bear_ratio")
        cur_posts = b.get("post_count_today")
        prev_price = prev.get("price_last")
        prev_bull = prev.get("bull_ratio")
        prev_bear = prev.get("bear_ratio")
        prev_posts = prev.get("post_count_today")
        L.append(f"■ 前回集計時点({_fmt(prev.get('generated_at'))})との比較")
        L.append(f"  前回値: 現在値={_fmt(prev_price)} 強気比率={_fmt_pct(prev_bull)}"
                 f" 弱気比率={_fmt_pct(prev_bear)} 投稿数={_fmt(prev_posts)}")
        price_delta = _delta(cur_price, prev_price, "{:+.0f}")
        bull_delta = ("不明" if cur_bull is None or prev_bull is None
                     else f"{(cur_bull - prev_bull) * 100:+.1f}pt")
        bear_delta = ("不明" if cur_bear is None or prev_bear is None
                     else f"{(cur_bear - prev_bear) * 100:+.1f}pt")
        posts_delta = _delta(cur_posts, prev_posts, "{:+.0f}")
        L.append(f"  今回との差分: 価格{price_delta} / 強気比率{bull_delta}"
                 f" / 弱気比率{bear_delta} / 投稿数{posts_delta}件増")
        L.append("")

    # ★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を分析
    # するように。翌日の傾向につながる可能性がある」)。東証の正規取引時間外(PTS・
    # 米国ADR)の値動きは翌営業日の東証の値動きに影響しうるため、参考情報として
    # 明示的にプロンプトへ渡す。断定的な予測をさせないための注意書きも併記する。
    eh = c.get("extended_hours")
    if eh and (eh.get("pts") or eh.get("adr")):
        tse_close = eh.get("tse_close") or {}
        L.append(f"■ 東証取引時間外(PTS・米国ADR)の動き"
                 f"(基準=東証最終値{_fmt(tse_close.get('price'))}"
                 f"[{_fmt(tse_close.get('time'))}時点])")
        pts = eh.get("pts")
        if pts:
            L.append(f"  PTS(私設取引システム・夜間取引): {_fmt(pts.get('price'))}"
                     f"({_fmt(pts.get('change_pct'))}% [{_fmt(pts.get('time'))}時点])")
        adr = eh.get("adr")
        if adr:
            L.append(f"  米国ADR円換算: {_fmt(adr.get('price_yen'))}"
                     f"({_fmt(adr.get('change_pct'))}% [{_fmt(adr.get('time'))}時点]"
                     f" ・ADR現地値={_fmt(adr.get('price_usd'))}ドル)")
        L.append("")

    L.append("■ 出力の指示(厳守)")
    L.append(f"  ・日本語で{target_length}でまとめる。")
    L.append("  ・冒頭または末尾に『掲示板の集計センチメントデータに基づく分析であり、"
             "投資助言ではない』旨を明示する。")
    L.append("  ・個別の投稿内容やユーザー名には一切言及しない(そもそも与えられていない)。")
    L.append("  ・断定的な将来予測(上がる/下がる等)や、買い時/売り時等の煽り表現・"
             "売買の推奨をしない。")
    L.append("  ・与えられた集計数値に基づく客観的な記述に徹する。")
    L.append("  ・★最重要: 各項目の数値を順番に言い換えるだけの『報告』にしない。"
             "複数の指標を関連付けて意味を読み解く『分析』を書く(強気比率と過熱度の"
             "整合性、価格の動きと掲示板の反応のタイミングのズレ、前回や直近の推移と"
             "比べて『何が』でなく『なぜ』変わったと考えられるか、等)。与えられた"
             "項目を機械的に網羅する必要はなく、意味のある関係性がある部分を"
             "掘り下げることを優先する。")
    if market_session:
        L.append(f"  ・「市場の状態」({market_session})を踏まえた表現にする。"
                 "前場中・後場中(取引時間内)は「現在の値動きは」等の現在進行形で"
                 "構わないが、寄り付き前は「前営業日の終値を基準に」、昼休みは"
                 "「前場の終値時点では」、取引終了後・休場(土日)は「本日の終値は」"
                 "「直近の取引日の終値は」等、その時間帯の実態に即した書き方にする"
                 "(取引時間外なのに値動きが今まさに動いているかのような書き方をしない)。")
    if prev:
        L.append("  ・「■ 前回集計時点との比較」の差分にも触れ、直近の短時間での"
                 "変化・勢いについて一言言及する(価格や強弱比率が前回からどう動いたか)。")
    if eh and (eh.get("pts") or eh.get("adr")):
        L.append("  ・「■ 東証取引時間外(PTS・米国ADR)の動き」があれば、翌営業日の"
                 "東証の値動きを見る上での参考情報として一言触れる(ただし『翌日は"
                 "上がる/下がる』等の断定的な予測はしない。あくまで『PTS/ADRでは"
                 "こう動いている』という事実の記述に留める)。")
    if signal_cards:
        L.append("  ・「■ シグナル発火状況(9指標)」のうち、OK以外(警戒/発火)の"
                 "指標があれば具体的にどれがどんな状態かに触れ、その背景にありそうな"
                 "投稿者心理(何が偏っているためにその指標が反応しているのか)にも"
                 "一言踏み込む。全て平常範囲内(OK)であれば、その旨を簡潔に述べる"
                 "程度でよい(9指標を一つずつ機械的に列挙しない)。「発火」はあくまで"
                 "統計的な偏りの記述的ラベルであり売買シグナルではないことを"
                 "踏まえた書き方にする。")
    if signal_changes:
        L.append("  ・「■ 前回取引日からの状態変化」があれば、どの指標がどう"
                 "変わったかに触れる。")
    if regime:
        L.append("  ・「■ ボラ・レジーム帯」にも一言触れ、現在の値動きの荒さの"
                 "目安を伝える(較正中であればその旨を正直に書く)。そのレジームの"
                 "水準が、掲示板の強気/弱気比率や過熱度の強さと整合しているか"
                 "(ボラが荒いのにセンチメントは穏やか等のズレがあれば)にも触れる。")
    return "\n".join(L)


# ============================================================================
# LLM 呼び出し(有料API。generate_public_insight() を実際に呼んだ時だけ課金)
# ============================================================================
PUBLIC_INSIGHT_MODEL = config.OPUS_MODEL
PUBLIC_INSIGHT_MAX_TOKENS = 800
# ★2026-08-19: ローカルLLM(lmstudio)経路の出力上限。600字(日本語)は約900トークン
# 相当(analyze.pyの1.5文字/token実測比率と同様の目安)だが、MioTTSで実際に700
# トークン既定値による切り詰めが起きた前例([[reference-miotts-700token-truncation-bug]]
# 相当の教訓)があるため、余裕を持って2000に設定する。
PUBLIC_INSIGHT_LOCAL_MAX_TOKENS = int(os.environ.get("BBS_PUBLIC_INSIGHT_LOCAL_MAX_TOKENS", "2000"))


def _log(msg):
    import os
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] public_insight: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _usage_dict(usage):
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0}
    return {"input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0}


def _call_lmstudio_insight(prompt, model=None, post_fn=None):
    """LM Studio(ローカルLLM)へ考察文生成を1回リクエストする。analyze.py の
    _call_lmstudio_fast と同じ呼び出し規約(config.LOCAL_LLM_ENDPOINTS["lmstudio"]・
    post_fn差し替え可・timeout=config.LMSTUDIO_TIMEOUT_SEC)を踏襲するが、こちらは
    構造化JSON分類ではなく自由文の考察テキストを直接返す。
    戻り値: (text, usage_dict, use_model)。"""
    import requests  # 遅延import(他のローカルLLM呼び出しと同じ流儀)

    poster = post_fn or requests.post
    endpoint = config.LOCAL_LLM_ENDPOINTS["lmstudio"]
    use_model = model or endpoint["model"]
    resp = poster(
        f"{endpoint['base_url']}/chat/completions",
        json={
            "model": use_model,
            "messages": [
                {"role": "system", "content": _PUBLIC_INSIGHT_SYS_LOCAL},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": 0.3,   # 考察文なのでanalyzeの分類用(temperature=0)より僅かに自然な言い回しを許容
            "max_tokens": PUBLIC_INSIGHT_LOCAL_MAX_TOKENS,
        },
        timeout=config.LMSTUDIO_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (data["choices"][0]["message"]["content"] or "").strip()
    usage_raw = data.get("usage") or {}
    usage = {"input_tokens": usage_raw.get("prompt_tokens", 0) or 0,
             "output_tokens": usage_raw.get("completion_tokens", 0) or 0}
    return text, usage, use_model


def generate_public_insight(public_record, *, model=None, max_tokens=None, client=None,
                             backend=None, post_fn=None):
    """
    公開用AI考察文を生成する。

    backend: "lmstudio"(既定・config.PUBLIC_INSIGHT_BACKENDから導出・ローカルLLMの
    ため課金なし=1日1回ゲート撤廃済み・毎回生成OK) | "claude"(従来のOpus API経路・
    環境変数 PUBLIC_INSIGHT_BACKEND=claude で即座に切替可能・**この経路だけ**
    generate_public_insight() を呼んだ時に課金される)。省略時は
    config.PUBLIC_INSIGHT_BACKEND に従う。

    安全設計: 入力は public_record(dict)のみ。個別コメントのリストやテキストを
    受け取れない(build_public_insight_context() 自体が構造的に受け取れない設計。
    このモジュールも同じ入力しか扱わない・バックエンドに関係なく共通)。呼び手
    (public_export.py)は validate_no_leak() を通過済みの public_record だけを
    渡す想定。

    client(claude経路用)を渡さなければ analyze._get_client()(ANTHROPIC_API_KEY必須・
    insight.pyと同じ共有基盤)を使う。post_fn(lmstudio経路用)はrequests.post互換の
    差し替え口(selftestでの実サーバー接続なしテスト用・analyze.pyと同じ流儀)。
    例外は fail-soft: 何が起きても None を返す(呼び手のエクスポート処理自体は
    止めない)。

    戻り値(成功時): {text, model, usage, generated_at, context, backend}。失敗時: None。
    """
    try:
        context = build_public_insight_context(public_record)
        use_backend = backend or config.PUBLIC_INSIGHT_BACKEND

        if use_backend == "lmstudio":
            prompt = render_public_prompt(context, target_length="800字程度")
            text, usage, use_model = _call_lmstudio_insight(prompt, model=model, post_fn=post_fn)
            if not text:
                _log("WARN empty text from LLM (lmstudio); returning None (fail-soft)")
                return None
            generated_at = dt.datetime.now().isoformat(timespec="seconds")
            _log(f"generated chars={len(text)} model={use_model} backend=lmstudio "
                 f"tokens in={usage['input_tokens']} out={usage['output_tokens']}")
            return {"text": text, "model": use_model, "usage": usage,
                    "generated_at": generated_at, "context": context, "backend": "lmstudio"}

        # backend == "claude"(既定の後方互換パス。環境変数 PUBLIC_INSIGHT_BACKEND=claude で
        # 即座に戻せるよう、既存の挙動・戻り値スキーマ(backendキー以外)は一切変更しない)。
        prompt = render_public_prompt(context)
        use_model = model or PUBLIC_INSIGHT_MODEL
        use_max_tokens = max_tokens or PUBLIC_INSIGHT_MAX_TOKENS

        if client is None:
            from analyze import _get_client  # anthropic クライアント基盤を analyze と共有
            client = _get_client()

        resp = client.messages.create(
            model=use_model, max_tokens=use_max_tokens, system=_PUBLIC_INSIGHT_SYS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        usage = _usage_dict(getattr(resp, "usage", None))
        if not text:
            _log("WARN empty text from LLM; returning None (fail-soft)")
            return None

        generated_at = dt.datetime.now().isoformat(timespec="seconds")
        _log(f"generated chars={len(text)} model={use_model} "
             f"tokens in={usage['input_tokens']} out={usage['output_tokens']}")
        return {"text": text, "model": use_model, "usage": usage,
                "generated_at": generated_at, "context": context, "backend": "claude"}
    except Exception as e:
        _log(f"WARN generate_public_insight failed (fail-soft None): {e!r}")
        return None


# ============================================================================
# ニュース見出し要約(★2026-08-27追加・news_fetch.collect_news()から呼ばれる)。
# ユーザー依頼「24時間以内のキオクシアに関係しそうなニュースの要約を公開ダッシュ
# ボードに」。入力はnews_fetch.pyが取得した見出し(タイトル・発行元・時刻)のみ
# ―― 記事本文は一切扱わない(著作権/利用規約への配慮。individual-post-leak防止と
# 同じ「渡せる情報の種類を構造的に絞る」設計思想)。
# ============================================================================
_NEWS_SUMMARY_SYS = (
    "あなたは日本株のニュース見出しを要約する客観的なアナリストです。与えられた"
    "ニュース見出し一覧(タイトル・発行元・発行時刻)だけを根拠に、直近の動向を"
    "簡潔にまとめます。次を絶対に守ります。\n"
    "(1) 個々の見出しを順番に言い換えるだけでなく、全体として何が起きているかを"
    "まとめる。\n"
    "(2) 『買い時/売り時/上がる/下がる』等、断定的な将来予測や煽り表現、"
    "売買の推奨・指示は一切書かない。\n"
    "(3) 見出しに書かれていない情報を推測・憶測で付け足さない(捏造しない)。\n"
    "(4) ★2026-08-27追加(ユーザー依頼「エヌビディアやサンディスクとかも、"
    "キオクシアに影響するニュースになります。キーワードを拡張して取り込むように」)。"
    "見出しの中には対象銘柄自体でなく、NAND関連の主要な顧客・競合企業(エヌビディア="
    "NAND/HBMの大口調達先、サンディスク=フラッシュメモリの直接競合等)についての"
    "見出しが混じることがある。これらは対象銘柄への影響が見込まれる周辺動向として"
    "扱い、対象銘柄自体の話であるかのように混同しない(『エヌビディアの決算は』の"
    "ように主体を明示する)。\n"
    "(5) 日本語で150〜250字程度、簡潔にまとめる。\n"
    "(6) 本文の末尾で、これが『ニュース見出しの要約であり、投資助言ではない』"
    "旨を明示する。"
)


def build_news_summary_context(items):
    """純関数: news_fetch.collect_news()が返すitems(直近24h分の見出しリスト)から、
    プロンプト用のホワイトリスト済みコンテキストを組み立てる。
    build_public_insight_context()と同じ思想(dictをそのまま右から左へ流さず、
    title/source/publishedだけを明示的に抜き出す)。titleが無い項目は捨てる。"""
    return [
        {"title": (it or {}).get("title"), "source": (it or {}).get("source"),
         "published": (it or {}).get("published")}
        for it in (items or []) if isinstance(it, dict) and it.get("title")
    ]


def render_news_summary_prompt(context):
    """build_news_summary_context()の出力 -> ユーザープロンプト文字列(純関数)。"""
    L = []
    L.append("以下は、ある銘柄に関連して直近24時間以内に配信されたニュース見出しの"
             "一覧です(本文は含まれていません・見出しのみ)。これらの見出しだけを"
             "根拠に、簡潔な要約文を書いてください。")
    L.append("")
    for it in context or []:
        title = it.get("title") or ""
        source = it.get("source") or "出所不明"
        L.append(f"・{title}({source})")
    L.append("")
    L.append("■ 出力の指示(厳守)")
    L.append("  ・日本語で150〜250字程度でまとめる。")
    L.append("  ・個々の見出しを順番に言い換えるのでなく、全体としての動向をまとめる。")
    L.append("  ・見出しに書かれていない情報を推測で付け足さない(捏造しない)。")
    L.append("  ・断定的な将来予測(上がる/下がる等)や売買の推奨・煽り表現をしない。")
    L.append("  ・末尾に『ニュース見出しの要約であり、投資助言ではない』旨を明示する。")
    return "\n".join(L)


def _call_lmstudio_news_summary(prompt, model=None, post_fn=None):
    """ニュース要約専用のlmstudio呼び出し(_call_lmstudio_insight()と同じ規約・
    システムプロンプトだけ_NEWS_SUMMARY_SYSへ差し替え・max_tokensも専用値)。"""
    import requests
    poster = post_fn or requests.post
    endpoint = config.LOCAL_LLM_ENDPOINTS["lmstudio"]
    use_model = model or endpoint["model"]
    resp = poster(
        f"{endpoint['base_url']}/chat/completions",
        json={
            "model": use_model,
            "messages": [
                {"role": "system", "content": _NEWS_SUMMARY_SYS},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": 0.3,
            "max_tokens": config.NEWS_SUMMARY_MAX_TOKENS,
        },
        timeout=config.LMSTUDIO_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (data["choices"][0]["message"]["content"] or "").strip()
    usage_raw = data.get("usage") or {}
    usage = {"input_tokens": usage_raw.get("prompt_tokens", 0) or 0,
             "output_tokens": usage_raw.get("completion_tokens", 0) or 0}
    return text, usage, use_model


def generate_news_summary(items, *, model=None, max_tokens=None, client=None,
                          backend=None, post_fn=None):
    """ニュース見出し要約文を生成する(news_fetch.collect_news()から呼ばれる)。
    generate_public_insight()と同じbackend切替(既定config.PUBLIC_INSIGHT_BACKEND)・
    fail-soft(例外/空文字は None)設計を踏襲する。itemsが空(見出しが1件も無い)場合は
    LLMを呼ばずNoneを返す(=無駄な呼び出しをしない・呼び手側の「ニュース無し」表示に
    委ねる)。

    戻り値(成功時): {text, model, usage, generated_at, backend}。失敗時: None。
    """
    try:
        context = build_news_summary_context(items)
        if not context:
            return None
        prompt = render_news_summary_prompt(context)
        use_backend = backend or config.PUBLIC_INSIGHT_BACKEND

        if use_backend == "lmstudio":
            text, usage, use_model = _call_lmstudio_news_summary(
                prompt, model=model, post_fn=post_fn)
            if not text:
                _log("WARN empty text from LLM (lmstudio news summary); "
                     "returning None (fail-soft)")
                return None
            generated_at = dt.datetime.now().isoformat(timespec="seconds")
            _log(f"news_summary generated chars={len(text)} model={use_model} "
                 f"backend=lmstudio")
            return {"text": text, "model": use_model, "usage": usage,
                   "generated_at": generated_at, "backend": "lmstudio"}

        use_model = model or PUBLIC_INSIGHT_MODEL
        use_max_tokens = max_tokens or config.NEWS_SUMMARY_MAX_TOKENS
        if client is None:
            from analyze import _get_client
            client = _get_client()
        resp = client.messages.create(
            model=use_model, max_tokens=use_max_tokens, system=_NEWS_SUMMARY_SYS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        usage = _usage_dict(getattr(resp, "usage", None))
        if not text:
            _log("WARN empty text from LLM (news summary); returning None (fail-soft)")
            return None
        generated_at = dt.datetime.now().isoformat(timespec="seconds")
        _log(f"news_summary generated chars={len(text)} model={use_model}")
        return {"text": text, "model": use_model, "usage": usage,
               "generated_at": generated_at, "backend": "claude"}
    except Exception as e:
        _log(f"WARN generate_news_summary failed (fail-soft None): {e!r}")
        return None


# ============================================================================
# 純関数テスト(ネット/LLM 非依存 = どの環境でも緑・実課金しない=モック)
# ============================================================================
class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeUsage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(300, 150)


class _FakeClient:
    """API を叩かないモック。呼び出し kwargs を calls に記録する。"""
    def __init__(self, text):
        self._text = text
        self.calls = []
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                return _FakeResp(outer._text)

        self.messages = _Messages()


class _RaisingClient:
    """常に例外を投げるモック(fail-soft検証用)。"""
    class _Messages:
        def create(self, **kw):
            raise RuntimeError("boom (simulated API failure)")

    def __init__(self):
        self.messages = self._Messages()


def _sample_public_record():
    """public_export.build_public_record() が作る形の公開レコードのサンプル
    (このモジュールは public_export に依存しないよう、辞書リテラルで直接用意する)。"""
    return {
        "schema_version": "1.0",
        "symbol": "285A",
        "company_name": "キオクシアホールディングス",
        "generated_at": "2026-08-16T15:00:00",
        "price": {"last": 53740.0, "change_pct": 3.75},
        "board": {
            "post_count_today": 1125, "posts_per_hour": 75.0,
            "bull_ratio": 0.249, "bear_ratio": 0.119, "neutral_ratio": 0.632,
            "overheat_score": 10.8, "capitulation_score": 0.0,
        },
        "trend_14d": [
            {"date": "2026-08-15", "post_count": 2723, "bear_ratio": 0.135, "bull_ratio": 0.235},
            {"date": "2026-08-16", "post_count": 1125, "bear_ratio": 0.119, "bull_ratio": 0.249},
        ],
        "price_sentiment_series": [
            {"date": "2026-08-15", "price_close": 51800.0, "bear_ratio": 0.135, "bull_ratio": 0.235},
            {"date": "2026-08-16", "price_close": 53740.0, "bear_ratio": 0.119, "bull_ratio": 0.249},
        ],
        "disclaimer": "本情報は掲示板投稿を統計的に集計したものであり、投資助言ではありません。"
                      "個別の投稿内容は含まれていません。研究・エンタメ用途です。",
    }


def _run_selftests():
    fails = []
    # 密閉化: _log を本番 run.log でなく一時ファイルへ向ける(モックの "simulated API
    # failure" 等のテスト文言が本番ログを汚さないため。selftest.py の他テスト
    # 　(test_forward_oos_freeze_settle等)と同じ分離パターン。2026-08-17: これが
    # 　無かったため実際に本番run.logへ混入し、おにやが誤って本番障害と疑う事故が発生した)。
    import tempfile as _tempfile
    import os as _os
    _saved_log = config.LOG_PATH
    config.LOG_PATH = _os.path.join(_tempfile.mkdtemp(), "run.log")

    def check(name, cond):
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    rec = _sample_public_record()

    # ---- build_public_insight_context: シグネチャが public_record 1個だけであること ----
    sig = inspect.signature(build_public_insight_context)
    params = list(sig.parameters.values())
    check("context: exactly 1 parameter", len(params) == 1)
    check("context: parameter name is public_record", params[0].name == "public_record")
    check("context: no *args (VAR_POSITIONAL)",
          all(p.kind != inspect.Parameter.VAR_POSITIONAL for p in params))
    check("context: no **kwargs (VAR_KEYWORD)",
          all(p.kind != inspect.Parameter.VAR_KEYWORD for p in params))

    # ---- build_public_insight_context: 内容の正しさ ----
    ctx = build_public_insight_context(rec)
    check("context: symbol", ctx["symbol"] == "285A")
    check("context: price.last", ctx["price"]["last"] == 53740.0)
    check("context: board.bull_ratio", ctx["board"]["bull_ratio"] == 0.249)
    check("context: price_sentiment_series length", len(ctx["price_sentiment_series"]) == 2)
    check("context: trend_14d length", len(ctx["trend_14d"]) == 2)
    check("context: empty record safe", build_public_insight_context({}) is not None)
    check("context: None record safe", build_public_insight_context(None) is not None)

    # ---- build_public_insight_context: ホワイトリスト方式(未知キーは通さない) ----
    dirty = dict(rec)
    dirty["board"] = dict(rec["board"])
    dirty["board"]["leaked_sample"] = {
        "text": "SECRET_INDIVIDUAL_COMMENT_YOSHIDA", "author": "hash_abc123",
    }
    dirty["some_unknown_leak_field"] = "SECRET_TOP_LEVEL_LEAK"
    ctx_dirty = build_public_insight_context(dirty)
    ctx_json = __import__("json").dumps(ctx_dirty, ensure_ascii=False)
    check("context: whitelist drops unknown nested leak key",
          "SECRET_INDIVIDUAL_COMMENT_YOSHIDA" not in ctx_json and "hash_abc123" not in ctx_json)
    check("context: whitelist drops unknown top-level key",
          "SECRET_TOP_LEVEL_LEAK" not in ctx_json)

    # ---- render_public_prompt ----
    prompt = render_public_prompt(ctx)
    check("prompt: has symbol", "285A" in prompt)
    check("prompt: has price", "53740" in prompt)
    check("prompt: has bull/bear ratio labels", "強気比率" in prompt and "弱気比率" in prompt)
    check("prompt: has price_sentiment_series dates", "2026-08-15" in prompt and "2026-08-16" in prompt)
    check("prompt: has discipline instructions",
          "投資助言ではない" in prompt and "煽り" in prompt)
    check("prompt: no leaked individual text even from dirty record",
          "SECRET_INDIVIDUAL_COMMENT_YOSHIDA" not in prompt and "hash_abc123" not in prompt)

    # 空コンテキストでも例外にならない
    empty_prompt = render_public_prompt({})
    check("prompt: empty context safe", isinstance(empty_prompt, str) and len(empty_prompt) > 0)

    # ---- extended_hours(PTS・米国ADR) ----
    # ★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を分析
    # するように。翌日の傾向につながる可能性がある」)。
    rec_eh = dict(rec)
    rec_eh["extended_hours"] = {
        "tse_close": {"price": 53740.0, "time": "08/16 15:30"},
        "pts": {"price": 54200.0, "change_pct": 0.86, "time": "08/16 18:00"},
        "adr": {"price_yen": 54500.0, "price_usd": 34.2, "change_pct": 1.41,
               "time": "08/16 23:30"},
    }
    ctx_eh = build_public_insight_context(rec_eh)
    check("context: extended_hours.pts passthrough", ctx_eh["extended_hours"]["pts"]["price"] == 54200.0)
    check("context: extended_hours.adr passthrough",
          ctx_eh["extended_hours"]["adr"]["price_yen"] == 54500.0)
    prompt_eh = render_public_prompt(ctx_eh)
    check("prompt: extended_hours section present when pts/adr available",
          "PTS" in prompt_eh and "ADR" in prompt_eh and "54200" in prompt_eh
          and "54500" in prompt_eh)
    check("prompt: extended_hours instruction present",
          "断定的な予測はしない" in prompt_eh)
    # extended_hours未提供(None)の場合は既存動作を壊さず、そのセクション自体が現れない
    check("context: extended_hours None when absent", ctx["extended_hours"] is None)
    check("prompt: no PTS section when extended_hours absent", "PTS(私設取引システム" not in prompt)

    # ---- signal_cards/regime/signal_state_changes(ダッシュボード掲載情報)----
    # ★2026-08-21追加(ユーザー依頼「AI考察では、ダッシュボードに記載の情報に対する
    # 考察もさせるように。シグナル発火状況とか」)。
    rec_sig = dict(rec)
    rec_sig["signal_cards"] = [
        {"name": "灼熱メーター(過熱)", "value": 15.5, "threshold": ">36", "state": "OK",
         "note": "過熱スコア15.5 (閾値36)",
         "leaked_text": "SECRET_INDIVIDUAL_COMMENT"},  # ホワイトリスト検証用の未知キー
        {"name": "阿鼻叫喚(セリクラ)", "value": 45.0, "threshold": ">30", "state": "発火",
         "note": "阿鼻叫喚スコア45.0 安値圏"},
    ]
    rec_sig["regime"] = {"vol_regime": "警戒", "vol_regime_score": 0.62, "calibration_status": "ok"}
    rec_sig["signal_state_changes"] = [
        {"name": "阿鼻叫喚(セリクラ)", "from": "OK", "to": "発火", "compared_date": "2026-08-15"},
    ]
    ctx_sig = build_public_insight_context(rec_sig)
    check("context: signal_cards passthrough (name/value/threshold/state/note)",
          ctx_sig["signal_cards"][1]["name"] == "阿鼻叫喚(セリクラ)"
          and ctx_sig["signal_cards"][1]["state"] == "発火")
    check("context: signal_cards drops unknown per-card keys (whitelist)",
          "leaked_text" not in ctx_sig["signal_cards"][0]
          and set(ctx_sig["signal_cards"][0]) == {"name", "value", "threshold", "state", "note"})
    check("context: regime passthrough", ctx_sig["regime"]["vol_regime"] == "警戒"
          and ctx_sig["regime"]["vol_regime_score"] == 0.62)
    check("context: signal_state_changes passthrough",
          ctx_sig["signal_state_changes"][0]["to"] == "発火")
    check("context: signal_cards/regime/signal_state_changes empty/absent when not provided",
          ctx["signal_cards"] == [] and ctx["regime"] is None and ctx["signal_state_changes"] == [])

    prompt_sig = render_public_prompt(ctx_sig)
    check("prompt: signal_cards section lists each card's name and state",
          "灼熱メーター(過熱)" in prompt_sig and "阿鼻叫喚(セリクラ)" in prompt_sig
          and "発火" in prompt_sig)
    check("prompt: no leaked per-card field even from a dirty signal_cards entry",
          "SECRET_INDIVIDUAL_COMMENT" not in prompt_sig)
    check("prompt: regime section present", "ボラ・レジーム帯" in prompt_sig and "警戒" in prompt_sig)
    check("prompt: signal_state_changes section present with compared_date",
          "2026-08-15" in prompt_sig and "OK → 発火" in prompt_sig)
    check("prompt: signal_cards instruction present",
          "9指標を一つずつ機械的に列挙しない" in prompt_sig)
    # 未提供時は既存動作を壊さず、いずれのセクションも現れない
    check("prompt: no signal_cards/regime/state-change sections when absent",
          "シグナル発火状況" not in prompt and "ボラ・レジーム帯" not in prompt
          and "からの状態変化" not in prompt)

    # ---- market_session(市場の開場時間帯)----
    # ★2026-08-21追加(ユーザー依頼「公開ダッシュボードのAI考察では、日本市場の
    # 開場時間帯を考慮した考察をするように」)。generated_atから市場状態を判定し、
    # コンテキスト・プロンプトの双方へ正しく反映されることを検証する。
    # rec自体のgenerated_at("2026-08-16T15:00:00")は日曜日 -> 休場(土日)になる。
    check("context: market_session computed from generated_at (Sunday -> 休場(土日))",
          ctx["market_session"] == "休場(土日)")
    check("prompt: market_session section present with the computed label",
          "市場の状態" in prompt and "休場(土日)" in prompt)
    check("prompt: market_session instruction present",
          "取引時間外なのに値動きが今まさに動いているかのような書き方をしない" in prompt)

    # 取引時間内(平日昼)のケースも確認(木曜10:00 -> 前場中)。
    rec_open = dict(rec)
    rec_open["generated_at"] = "2026-08-20T10:00:00"
    ctx_open = build_public_insight_context(rec_open)
    check("context: market_session weekday morning -> 前場中",
          ctx_open["market_session"] == "前場中")
    prompt_open = render_public_prompt(ctx_open)
    check("prompt: market_session label reflects 前場中",
          "前場中" in prompt_open)

    # generated_at が無い/壊れている場合は None(fail-soft)・セクション自体が現れない。
    rec_no_gen = dict(rec)
    del rec_no_gen["generated_at"]
    check("context: market_session None when generated_at absent",
          build_public_insight_context(rec_no_gen)["market_session"] is None)
    rec_bad_gen = dict(rec)
    rec_bad_gen["generated_at"] = "not-a-timestamp"
    ctx_bad = build_public_insight_context(rec_bad_gen)
    check("context: market_session None when generated_at unparseable (fail-soft, no crash)",
          ctx_bad["market_session"] is None)
    prompt_bad = render_public_prompt(ctx_bad)
    check("prompt: no market_session section when unavailable",
          "市場の状態" not in prompt_bad)

    # ---- generate_public_insight: 正常系(モック・claude経路)----
    # ★2026-08-19: config.PUBLIC_INSIGHT_BACKEND の既定が"lmstudio"へ変わったため、
    # 以下のclaude(Anthropic)経路のテストは全て backend="claude" を明示する
    # (省略すると既定のlmstudio経路に流れ、client=フェイクモックが無視されて実際に
    # ローカルLM Studioへネットワーク接続してしまう=このモジュール自身のselftestが
    # ハングしかけた事故が2026-08-19に発生・以後この明示を徹底する)。
    sample_text = ("本情報は掲示板の集計センチメントデータに基づく分析であり、投資助言では"
                  "ありません。285Aは前日比+3.75%、掲示板の強気比率は24.9%、弱気比率は"
                  "11.9%でした。投稿速度は1時間あたり75件と活発でした。")
    fc = _FakeClient(sample_text)
    out = generate_public_insight(rec, client=fc, backend="claude")
    check("generate: returns dict on success", out is not None)
    check("generate: text matches mock", out["text"] == sample_text)
    check("generate: one LLM call", len(fc.calls) == 1)
    check("generate: default model is OPUS_MODEL", fc.calls[0]["model"] == config.OPUS_MODEL)
    check("generate: system prompt passed", fc.calls[0]["system"] == _PUBLIC_INSIGHT_SYS)
    check("generate: usage summed", out["usage"]["input_tokens"] == 300)
    check("generate: has generated_at", out.get("generated_at") is not None)
    check("generate: context echoed back", out["context"]["symbol"] == "285A")
    check("generate: backend echoed back as claude", out.get("backend") == "claude")

    # モデル/max_tokens 明示指定が反映される
    fc2 = _FakeClient(sample_text)
    generate_public_insight(rec, client=fc2, model=config.HAIKU_MODEL, max_tokens=123, backend="claude")
    check("generate: custom model honored", fc2.calls[0]["model"] == config.HAIKU_MODEL)
    check("generate: custom max_tokens honored", fc2.calls[0]["max_tokens"] == 123)

    # ---- generate_public_insight: fail-soft(例外時 None) ----
    out_fail = generate_public_insight(rec, client=_RaisingClient(), backend="claude")
    check("generate: fail-soft returns None on exception", out_fail is None)

    # public_record が空/None でも例外にならず fail-soft(モック応答は返る)
    out_empty = generate_public_insight({}, client=_FakeClient(sample_text), backend="claude")
    check("generate: empty public_record still generates (no crash)", out_empty is not None)
    out_none = generate_public_insight(None, client=_FakeClient(sample_text), backend="claude")
    check("generate: None public_record still generates (no crash)", out_none is not None)

    # 空文字を返すモックは None 扱い(fail-soft)
    out_blank = generate_public_insight(rec, client=_FakeClient(""), backend="claude")
    check("generate: blank text -> None (fail-soft)", out_blank is None)

    # ---- generate_public_insight: 正常系(モック・lmstudio経路)★2026-08-19追加 ----
    # post_fn差し替えで実サーバー接続なしにテストする(analyze.pyのselftestと同じ流儀)。
    lm_calls = []

    def _fake_lmstudio_post(url, json=None, timeout=None):
        lm_calls.append({"url": url, "json": json, "timeout": timeout})

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": sample_text}}],
                        "usage": {"prompt_tokens": 400, "completion_tokens": 200}}
        return _R()

    out_lm = generate_public_insight(rec, backend="lmstudio", post_fn=_fake_lmstudio_post)
    check("generate(lmstudio): returns dict on success", out_lm is not None)
    check("generate(lmstudio): text matches mock", out_lm["text"] == sample_text)
    check("generate(lmstudio): one LLM call", len(lm_calls) == 1)
    check("generate(lmstudio): model is the configured lmstudio model",
          out_lm["model"] == config.LOCAL_LLM_ENDPOINTS["lmstudio"]["model"])
    check("generate(lmstudio): system prompt is the local variant",
          lm_calls[0]["json"]["messages"][0]["content"] == _PUBLIC_INSIGHT_SYS_LOCAL)
    check("generate(lmstudio): prompt asks for 800字程度 (not the claude 400-600字)",
          "800字程度" in lm_calls[0]["json"]["messages"][1]["content"])
    check("generate(lmstudio): usage mapped from prompt/completion_tokens",
          out_lm["usage"] == {"input_tokens": 400, "output_tokens": 200})
    check("generate(lmstudio): backend echoed back as lmstudio", out_lm.get("backend") == "lmstudio")
    check("generate(lmstudio): timeout uses config.LMSTUDIO_TIMEOUT_SEC",
          lm_calls[0]["timeout"] == config.LMSTUDIO_TIMEOUT_SEC)

    # lmstudio経路のfail-soft(HTTPエラー等の例外) -> None
    def _raising_post(url, json=None, timeout=None):
        raise RuntimeError("boom (simulated lmstudio connection failure)")
    out_lm_fail = generate_public_insight(rec, backend="lmstudio", post_fn=_raising_post)
    check("generate(lmstudio): fail-soft returns None on exception", out_lm_fail is None)

    # lmstudio経路で空文字が返る場合もNone扱い(fail-soft)
    def _blank_post(url, json=None, timeout=None):
        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": ""}}], "usage": {}}
        return _R()
    out_lm_blank = generate_public_insight(rec, backend="lmstudio", post_fn=_blank_post)
    check("generate(lmstudio): blank text -> None (fail-soft)", out_lm_blank is None)

    # ---- ニュース見出し要約(★2026-08-27追加) ----
    news_items = [
        {"title": "キオクシア、決算を発表", "source": "日本経済新聞",
         "published": "2026-08-27T01:00:00",
         "leaked_field": "SECRET_SHOULD_NOT_APPEAR"},  # ホワイトリスト検証用
        {"title": "半導体市況が回復基調", "source": "Reuters",
         "published": "2026-08-27T03:00:00"},
        {"title": None, "source": "無視されるはず(タイトル無し)"},
    ]
    news_ctx = build_news_summary_context(news_items)
    check("news: whitelist keeps only title/source/published",
          set(news_ctx[0]) == {"title", "source", "published"})
    check("news: leaked_field dropped", "leaked_field" not in news_ctx[0]
          and "SECRET_SHOULD_NOT_APPEAR" not in __import__("json").dumps(news_ctx, ensure_ascii=False))
    check("news: item without title is dropped", len(news_ctx) == 2)

    news_prompt = render_news_summary_prompt(news_ctx)
    check("news prompt: has both headlines", "キオクシア、決算を発表" in news_prompt
          and "半導体市況が回復基調" in news_prompt)
    check("news prompt: has sources", "日本経済新聞" in news_prompt and "Reuters" in news_prompt)
    check("news prompt: has discipline instructions",
          "投資助言ではない" in news_prompt and "捏造" in news_prompt)

    # empty items -> None without calling the LLM at all
    check("generate_news_summary: empty items -> None (no LLM call)",
          generate_news_summary([], backend="claude", client=_FakeClient(sample_text)) is None)

    news_fc = _FakeClient("半導体市況の回復とキオクシアの決算発表が報じられました。"
                          "※本要約はニュース見出しに基づくものであり、投資助言ではありません。")
    news_out = generate_news_summary(news_items, client=news_fc, backend="claude")
    check("generate_news_summary(claude): returns dict on success", news_out is not None)
    check("generate_news_summary(claude): system prompt is the news variant",
          news_fc.calls[0]["system"] == _NEWS_SUMMARY_SYS)
    check("generate_news_summary(claude): backend echoed back", news_out.get("backend") == "claude")

    news_lm_calls = []

    def _fake_news_lmstudio_post(url, json=None, timeout=None):
        news_lm_calls.append(json)

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": sample_text}}],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 60}}
        return _R()

    news_out_lm = generate_news_summary(news_items, backend="lmstudio",
                                        post_fn=_fake_news_lmstudio_post)
    check("generate_news_summary(lmstudio): returns dict on success", news_out_lm is not None)
    check("generate_news_summary(lmstudio): system prompt is the news variant",
          news_lm_calls[0]["messages"][0]["content"] == _NEWS_SUMMARY_SYS)
    check("generate_news_summary(lmstudio): max_tokens uses config.NEWS_SUMMARY_MAX_TOKENS",
          news_lm_calls[0]["max_tokens"] == config.NEWS_SUMMARY_MAX_TOKENS)

    news_out_fail = generate_news_summary(news_items, client=_RaisingClient(), backend="claude")
    check("generate_news_summary: fail-soft returns None on exception", news_out_fail is None)

    config.LOG_PATH = _saved_log
    print(f"\n{'PASS' if not fails else 'FAIL'}: {len(fails)} failure(s)")
    return len(fails)


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_selftests() else 0)
