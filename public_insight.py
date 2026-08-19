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


# ============================================================================
# システムプロンプト(安全設計の核心=想定読者・トーン方針を内包)
# ============================================================================
_PUBLIC_INSIGHT_SYS = (
    "あなたは日本株の掲示板センチメント集計データを解説する、一般公開向けの客観的な"
    "アナリストです。想定読者は一般の個人投資家です。次を絶対に守ります。\n"
    "(1) 本文の冒頭または末尾で、これが『掲示板投稿の集計センチメントデータに基づく"
    "分析であり、投資助言ではない』旨を明示する。\n"
    "(2) 個別の投稿内容・ユーザー名・投稿者IDには一切言及しない(そもそも与えられる"
    "入力は集計数値だけであり、個別投稿の情報は渡されていない)。\n"
    "(3) 『買い時/売り時/上がる/下がる/仕込め/利確しろ』等、断定的な将来予測や"
    "煽り表現、売買の推奨・指示は一切書かない。\n"
    "(4) 与えられた集計数値(価格・投稿量・強気/弱気比率・過熱度等)に基づく、"
    "事実ベースの客観的な記述に徹する。\n"
    "(5) 日本語で200〜400字程度、簡潔にまとめる。"
)

# ★2026-08-19: ローカルLLM(lmstudio)経路専用のシステムプロンプト(おにや08:57投稿・
#   試作600字版がベース)。安全設計の(1)〜(4)はClaude版と全く同じ、(5)の目標文字数
#   だけ600字程度へ変更(ローカルLLMは無料のためClaude版より詳しめの分量で運用する)。
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
    "総悲観度スコア等)に基づく、事実ベースの客観的な記述に徹する。\n"
    "(5) 日本語で600字程度、価格動向/投稿量/強気弱気比率/過熱度・総悲観度スコアに"
    "触れながらまとめる。"
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

    return {
        "symbol": r.get("symbol"),
        "company_name": r.get("company_name"),
        "generated_at": r.get("generated_at"),
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
    }


# 呼び手/テストがシグネチャを機械検証しやすいよう、引数名も固定しておく。
assert list(inspect.signature(build_public_insight_context).parameters) == ["public_record"]


def _fmt(v):
    return "不明" if v is None else v


def _fmt_pct(v):
    return "不明" if v is None else f"{v * 100:.1f}%"


def render_public_prompt(context, target_length="200〜400字程度"):
    """build_public_insight_context() の出力 -> ユーザープロンプト文字列(純関数)。
    system は _PUBLIC_INSIGHT_SYS(またはローカルLLM経路では_PUBLIC_INSIGHT_SYS_LOCAL)。
    個別投稿の情報は入力(context)に存在しないため、出力にも一切現れない。

    target_length: 出力の指示文に埋め込む目標文字数(既定は従来のClaude版と同じ
    "200〜400字程度"・後方互換)。★2026-08-19: ローカルLLM経路(おにや08:57投稿の
    600字版プロンプト)向けに"600字程度"を渡せるようパラメータ化。"""
    c = context or {}
    p = c.get("price") or {}
    b = c.get("board") or {}
    pss = c.get("price_sentiment_series") or []
    trend = c.get("trend_14d") or []

    L = []
    L.append(f"銘柄: {_fmt(c.get('symbol'))}({_fmt(c.get('company_name'))})")
    L.append(f"集計時刻: {_fmt(c.get('generated_at'))}")
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
    L.append(f"  ・日本語で{target_length}、簡潔にまとめる。")
    L.append("  ・冒頭または末尾に『掲示板の集計センチメントデータに基づく分析であり、"
             "投資助言ではない』旨を明示する。")
    L.append("  ・個別の投稿内容やユーザー名には一切言及しない(そもそも与えられていない)。")
    L.append("  ・断定的な将来予測(上がる/下がる等)や、買い時/売り時等の煽り表現・"
             "売買の推奨をしない。")
    L.append("  ・与えられた集計数値に基づく客観的な記述に徹する。")
    if prev:
        L.append("  ・「■ 前回集計時点との比較」の差分にも触れ、直近の短時間での"
                 "変化・勢いについて一言言及する(価格や強弱比率が前回からどう動いたか)。")
    if eh and (eh.get("pts") or eh.get("adr")):
        L.append("  ・「■ 東証取引時間外(PTS・米国ADR)の動き」があれば、翌営業日の"
                 "東証の値動きを見る上での参考情報として一言触れる(ただし『翌日は"
                 "上がる/下がる』等の断定的な予測はしない。あくまで『PTS/ADRでは"
                 "こう動いている』という事実の記述に留める)。")
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
            prompt = render_public_prompt(context, target_length="600字程度")
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
    check("generate(lmstudio): system prompt is the 600字 local variant",
          lm_calls[0]["json"]["messages"][0]["content"] == _PUBLIC_INSIGHT_SYS_LOCAL)
    check("generate(lmstudio): prompt asks for 600字程度 (not the claude 200-400字)",
          "600字程度" in lm_calls[0]["json"]["messages"][1]["content"])
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

    config.LOG_PATH = _saved_log
    print(f"\n{'PASS' if not fails else 'FAIL'}: {len(fails)} failure(s)")
    return len(fails)


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_selftests() else 0)
