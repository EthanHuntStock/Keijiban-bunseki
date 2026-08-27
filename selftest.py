# -*- coding: utf-8 -*-
"""
selftest.py - 純関数のロジックテスト。ネットワーク/API 非依存で緑になること。
実行: python selftest.py   (KABU系 env は不要)
"""
import os
import sys
import datetime as dt

import config
import collect_yahoo as C
import collect_5ch as C5
import collect_intl as CI
import snapshot as S
import analyze as A
import signals as SG
import price_fetch as PF
import jsonl_window as JW
import mojibake_cache as MC
import news_fetch as NF


FAILS = []


def check(name, cond):
    status = "OK  " if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        FAILS.append(name)


# ---- ミニHTML(実DOMのクラス名を模したフィクスチャ) ------------------------
MINI_HTML = """
<html><body>
<article class="_BbsItem_xx_10">
  <div class="_BbsItem__postDateBlock_xx_38">
    <a class="_BbsItem__commentNo_xx_42" href="/quote/285A.T/forum/1001">No.<!-- -->1001</a>
    <time class="_BbsItem__postDate_xx_38">2026/7/8 16:19</time>
  </div>
  <div class="_BbsItem__body_xx_85"><p>43000まできたら全力で買う。決算に期待。</p></div>
  <div class="_BbsItem__actionBlock_xx_152">
    <button aria-label="はいを送る"><span class="_ReactionButton__count_x">5</span></button>
    <button aria-label="いいえを送る"><span class="_ReactionButton__count_x">2</span></button>
  </div>
</article>
<article class="_BbsItem_xx_10">
  <div class="_BbsItem__postDateBlock_xx_38">
    <a class="_BbsItem__commentNo_xx_42" href="/quote/285A.T/forum/1002">No.<!-- -->1002</a>
    <time class="_BbsItem__postDate_xx_38">2026/7/8 16:20</time>
  </div>
  <div class="_BbsItem__body_xx_85">
    <div><a href="/quote/285A.T/forum/1001">&gt;&gt;1001</a></div>
    <p>もう暴落するから逃げた方がいい。損切り済み。</p>
  </div>
  <div class="_BbsItem__actionBlock_xx_152">
    <button aria-label="はいを送る"><span class="_ReactionButton__count_x">0</span></button>
    <button aria-label="いいえを送る"><span class="_ReactionButton__count_x">0</span></button>
  </div>
</article>
<article class="_BbsItem_xx_10"><div>broken item without id</div></article>
</body></html>
"""


def test_parse():
    rows = C.parse_bbs_html(MINI_HTML)
    check("parse: 2 valid comments (broken one skipped)", len(rows) == 2)
    r0 = rows[0]
    check("parse: id extracted", r0["id"] == "1001")
    check("parse: date ISO", r0["ts"] == "2026-07-08T16:19:00")
    check("parse: votes yes=5 no=2", r0["votes_yes"] == 5 and r0["votes_no"] == 2)
    check("parse: body text present", "43000" in r0["text"])
    # 返信引用リンク(>>1001)が本文に混ざらない
    check("parse: reply-quote excluded from body", ">>1001" not in rows[1]["text"])
    check("parse: empty html -> []", C.parse_bbs_html("") == [])
    check("parse: no-article html -> []", C.parse_bbs_html("<html><body>x</body></html>") == [])


def test_dedupe():
    rows = [{"id": "1"}, {"id": "2"}, {"id": "2"}, {"id": "3"}]
    new = C.dedupe_new(rows, {"1"})
    ids = [r["id"] for r in new]
    check("dedupe: seen removed + internal dupes collapsed", ids == ["2", "3"])
    check("dedupe: empty seen keeps all uniques",
          [r["id"] for r in C.dedupe_new(rows, set())] == ["1", "2", "3"])


def test_date():
    check("date: full ymd", C.parse_post_date("2026/7/8 16:19") == "2026-07-08T16:19:00")
    check("date: garbage passthrough", C.parse_post_date("なにか") == "なにか")
    check("date: None -> None", C.parse_post_date(None) is None)


def test_sentiment_agg():
    rows = [
        {"meaningful": True, "sentiment": "bullish"},
        {"meaningful": True, "sentiment": "bullish"},
        {"meaningful": True, "sentiment": "bearish"},
        {"meaningful": True, "sentiment": "neutral"},
        {"meaningful": False, "sentiment": "bullish"},  # 除外
    ]
    agg = A.aggregate_sentiment(rows)
    check("agg: meaningful=4", agg["meaningful"] == 4)
    check("agg: bull=2 bear=1 neu=1",
          agg["counts"] == {"bullish": 2, "bearish": 1, "neutral": 1})
    check("agg: bull ratio 0.5", abs(agg["ratios"]["bullish"] - 0.5) < 1e-9)
    empty = A.aggregate_sentiment([])
    check("agg: empty safe", empty["meaningful"] == 0 and empty["ratios"]["bullish"] == 0.0)


def test_spikes():
    prev = {"決算": 2, "需給": 5}
    cur = {"決算": 5, "需給": 6, "暴落": 4}
    sp = S.detect_spikes(prev, cur, ratio=2.0, min_count=3)
    topics = {s["topic"] for s in sp}
    check("spike: 決算(2->5, 2.5x) detected", "決算" in topics)
    check("spike: 需給(5->6, 1.2x) not detected", "需給" not in topics)
    check("spike: 暴落(new topic, >=min) detected", "暴落" in topics)
    check("spike: below min_count ignored",
          S.detect_spikes({}, {"x": 2}, min_count=3) == [])


def test_topic_cluster_counts():
    rows = [
        {"meaningful": True, "topics": ["決算", "需給"], "cluster_label": "決算期待"},
        {"meaningful": True, "topics": ["決算"], "cluster_label": "決算期待"},
        {"meaningful": True, "topics": ["チャート"], "cluster_label": "調整局面"},
        {"meaningful": False, "topics": ["煽り"], "cluster_label": "煽り"},  # 除外
    ]
    tc = S.topic_counts(rows)
    check("topic_counts: 決算=2", tc.get("決算") == 2)
    check("topic_counts: 煽り excluded", "煽り" not in tc)
    cc = S.cluster_counts(rows)
    check("cluster_counts: 決算期待=2", cc.get("決算期待") == 2)


def test_today_rows():
    rows = [
        {"ts": "2026-07-08T16:19:00", "id": "a"},
        {"ts": "2026-07-07T10:00:00", "id": "b"},
        {"ts": None, "id": "c"},
    ]
    tr = S.today_rows(rows, day="2026-07-08")
    check("today_rows: only matching date", [r["id"] for r in tr] == ["a"])


def test_cluster_texts():
    texts = [
        "決算に期待して買い増しした",
        "決算が楽しみ、買い増ししたい",
        "決算内容に期待している",
        "もう暴落するから損切りした",
        "暴落が怖いので損切り撤退",
        "暴落して損切り済み",
    ]
    labels = A.cluster_texts(texts, distance_threshold=0.85, min_comments=3)
    check("cluster: returns label per text", len(labels) == len(texts))
    # 期待方向と弱気方向が別クラスタに割れる(>=2クラスタ)
    check("cluster: at least 2 clusters formed", len(set(labels)) >= 2)
    # 少数件は単一クラスタ
    check("cluster: <min -> single cluster", A.cluster_texts(["a", "b"]) == [0, 0])
    check("cluster: empty -> []", A.cluster_texts([]) == [])


def test_json_extract():
    check("json: plain array", A._extract_json_array('[{"i":0}]') == [{"i": 0}])
    check("json: fenced array",
          A._extract_json_array('```json\n[{"i":1}]\n```') == [{"i": 1}])
    check("json: embedded array",
          A._extract_json_array('前置き [{"i":2}] 後置き') == [{"i": 2}])
    check("json: garbage -> []", A._extract_json_array("no json here") == [])


def test_build_cluster_summaries():
    rows = [
        {"id": "1", "text": "aaa", "sentiment": "bullish", "votes_yes": 3, "cluster_id": 0},
        {"id": "2", "text": "bbb", "sentiment": "bullish", "votes_yes": 1, "cluster_id": 0},
        {"id": "3", "text": "ccc", "sentiment": "bearish", "votes_yes": 9, "cluster_id": 1},
    ]
    cs = A.build_cluster_summaries(rows)
    check("summaries: 2 clusters", len(cs) == 2)
    top = cs[0]
    check("summaries: sorted by count desc", top["cluster_id"] == 0 and top["count"] == 2)
    check("summaries: rep text by votes", cs[1]["rep_texts"][0] == "ccc")


# ---- 5ch パーサ --------------------------------------------------------------
MINI_5CH_FIND = """
<html><body>
<a class="list_line_link" href="//greta.5ch.io/test/read.cgi/poverty/1783406983">
  【仕手株】キオクシア 終値 72400円 -11.26% (767)</a>
<a class="list_line_link" href="//nova.5ch.io/test/read.cgi/livegalileo/1783492764">
  285A 結局マイナス (4)</a>
<a class="list_line_link" href="//x.5ch.io/test/read.cgi/news/1/">
  全く関係ないスレ (10)</a>
</body></html>
"""

MINI_5CH_THREAD = """
<html><body>
<div class="clear post" data-id="1" id="1">
  <div class="post-header"><span class="postid">1</span>
    <span class="postusername"><b>名無し</b></span>
    <span class="date">2026/07/07(火) 14:39:01.33</span></div>
  <div class="post-content">キオクシアは決算に期待。買い増した。</div>
</div>
<div class="clear post" data-id="2" id="2">
  <div class="post-header"><span class="postid">2</span>
    <span class="date">2026/07/07(火) 14:40:10.00</span></div>
  <div class="post-content">&gt;&gt;1 いや暴落するぞ、逃げろ。</div>
</div>
<div class="clear post" data-id="3" id="3">
  <div class="post-content"></div>
</div>
</body></html>
"""


def test_mojibake_and_charset():
    check("mojibake: U+FFFD flagged", C.is_mojibake("post-content �� �") is True)
    check("mojibake: clean JP ok", C.is_mojibake("キオクシアは決算に期待。買い増した。") is False)
    check("mojibake: clean EN ok", C.is_mojibake("$KXHCF adding once again, great earnings") is False)
    check("mojibake: emoji JP ok", C.is_mojibake("上がれー🚀🚀 期待してる😀") is False)
    garble = "".join(chr(c) for c in range(0x80, 0x80 + 40))
    check("mojibake: latin-1 blob flagged", C.is_mojibake(garble) is True)
    check("mojibake: empty safe", C.is_mojibake("") is False)

    check("charset: shift_jis->cp932", C.normalize_charset("Shift_JIS") == "cp932")
    check("charset: x-sjis->cp932", C.normalize_charset("x-sjis") == "cp932")
    check("charset: utf-8 stays", C.normalize_charset("UTF-8") == "utf-8")
    check("charset: none->default", C.normalize_charset(None, "cp932") == "cp932")

    check("pick_charset: header wins",
          C5.pick_charset("text/html; charset=Shift_JIS", b"", "utf-8") == "cp932")
    check("pick_charset: meta fallback",
          C5.pick_charset("text/html", b'<meta charset="Shift_JIS">', "utf-8") == "cp932")
    check("pick_charset: default fallback",
          C5.pick_charset("text/html", b"<html>", "shift_jis") == "cp932")


def test_mojibake_cache():
    import tempfile, shutil

    d = tempfile.mkdtemp()
    orig_data_dir = config.DATA_DIR
    try:
        config.DATA_DIR = d  # 独立tmpdirへ差し替え(既存ログには一切触れない)

        rows = [
            {"id": "1", "text": "キオクシアは決算に期待。買い増した。"},
            {"id": "2", "text": "".join(chr(c) for c in range(0x80, 0x80 + 40))},  # garble
            {"id": "3", "text": "post-content ��"},  # U+FFFD garble
            {"id": 4, "text": "上がれー🚀🚀 期待してる😀"},  # int id also ok
        ]

        hits1 = MC.get_or_compute(rows)
        check("mojibake_cache: id keys as str", set(hits1.keys()) == {"1", "2", "3", "4"})
        check("mojibake_cache: judged correctly (1)", hits1["1"] is False)
        check("mojibake_cache: judged correctly (2)", hits1["2"] is True)
        check("mojibake_cache: judged correctly (3)", hits1["3"] is True)
        check("mojibake_cache: judged correctly (4)", hits1["4"] is False)

        p = MC._cache_path()
        check("mojibake_cache: file created after miss", os.path.exists(p))

        # 2周目=全ヒット。判定結果は不変で、is_mojibakeを再計算せず引けること。
        import collect_yahoo as _CY
        calls = {"n": 0}
        orig_fn = _CY.is_mojibake

        def _counting(*a, **kw):
            calls["n"] += 1
            return orig_fn(*a, **kw)

        MC.is_mojibake = _counting
        try:
            hits2 = MC.get_or_compute(rows)
        finally:
            MC.is_mojibake = orig_fn
        check("mojibake_cache: 2nd call all cache hits (no compute)", calls["n"] == 0)
        check("mojibake_cache: 2nd call same results", hits2 == hits1)

        # id無し行はキャッシュに乗らないがresultにも含まれない(fail-soft・呼び出し側対応)。
        rows_noid = [{"text": "テスト"}]
        hits3 = MC.get_or_compute(rows_noid)
        check("mojibake_cache: id-less row not cached", hits3 == {})

        # 壊れたキャッシュファイルでも例外を出さず空dictにfail-soft
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        check("mojibake_cache: corrupted file -> empty dict (fail-soft)",
              MC.load_cache() == {})

        # signals.clean_rows経由でも同じ判定になること(garbledフラグも尊重)
        rows_cr = rows + [{"id": "5", "text": "普通の文章", "garbled": True}]
        cleaned = SG.clean_rows(rows_cr)
        cleaned_ids = {r["id"] for r in cleaned}
        check("clean_rows: keeps non-garble ids only", cleaned_ids == {"1", 4})
    finally:
        config.DATA_DIR = orig_data_dir
        shutil.rmtree(d, ignore_errors=True)


def test_5ch_search():
    threads = C5.search_threads(MINI_5CH_FIND, title_filter=["キオクシア", "285A"])
    urls = [u for u, t in threads]
    check("5ch search: 2 matched (irrelevant filtered)", len(threads) == 2)
    check("5ch search: https-completed", all(u.startswith("https://") for u in urls))
    b, tid = C5.parse_thread_url(urls[0])
    check("5ch search: board/threadid parsed", b == "poverty" and tid == "1783406983")


def test_5ch_thread():
    posts = C5.parse_thread(MINI_5CH_THREAD, "poverty", "1783406983")
    check("5ch thread: 2 posts (empty body skipped)", len(posts) == 2)
    check("5ch thread: id namespaced", posts[0]["id"] == "5ch:poverty:1783406983:1")
    check("5ch thread: date ISO", posts[0]["ts"] == "2026-07-07T14:39:01")
    check("5ch thread: source tag", posts[0]["source"] == "5ch")
    check("5ch thread: reply-quote stripped", not posts[1]["text"].startswith(">>1"))
    check("5ch date garbage passthrough", C5.parse_5ch_date("xxx") == "xxx")


def _build_5ch_thread_html(n):
    """n件のダミー投稿divを持つread.cgi風HTMLを組み立てる(テスト用)。"""
    divs = []
    for i in range(1, n + 1):
        divs.append(
            f'<div class="clear post" data-id="{i}" id="{i}">'
            f'  <div class="post-header"><span class="postid">{i}</span>'
            f'    <span class="date">2026/08/21(金) 12:00:00.00</span></div>'
            f'  <div class="post-content">レス{i}</div>'
            f'</div>'
        )
    return "<html><body>" + "".join(divs) + "</body></html>"


def test_5ch_thread_no_truncation_beyond_old_80_cap():
    # 2026-08-21是正の回帰テスト:「活況スレ×長時間停止で末尾80件の外側が
    # 永久欠落する」旧バグの再発防止。120件のスレでも既定max_postsで
    # 全件(120件)取れること(旧上限80のままなら41件が消えて79個になる)。
    html = _build_5ch_thread_html(120)
    posts = C5.parse_thread(html, "poverty", "9999999999")
    check("5ch thread: default max_posts no longer truncates at old 80 cap",
          len(posts) == 120)
    check("5ch thread: oldest post(res1) still present after fix",
          any(p["id"].endswith(":1") for p in posts))
    check("5ch thread: newest post(res120) present",
          any(p["id"].endswith(":120") for p in posts))
    # max_postsを明示的に絞れば従来どおり末尾(最新側)のみに切り詰められる挙動は維持
    posts_capped = C5.parse_thread(html, "poverty", "9999999999", max_posts=10)
    check("5ch thread: explicit max_posts still windows to newest N",
          len(posts_capped) == 10 and posts_capped[0]["id"].endswith(":111"))


# ---- intl パーサ -------------------------------------------------------------
def test_reddit_parse():
    data = {"data": {"children": [
        {"data": {"id": "ab1", "title": "Kioxia soars", "selftext": "great earnings",
                  "created_utc": 1751990000, "score": 12, "author": "u1", "subreddit": "stocks"}},
        {"data": {"id": "ab2", "title": "", "selftext": "", "created_utc": 1751990001,
                  "score": 0, "author": "u2", "subreddit": "wsb"}},  # 空本文=除外
    ]}}
    rows = CI.parse_reddit_json(data)
    check("reddit: 1 non-empty parsed", len(rows) == 1)
    check("reddit: id namespaced", rows[0]["id"] == "reddit:ab1")
    check("reddit: source + score->votes", rows[0]["source"] == "reddit" and rows[0]["votes_yes"] == 12)
    check("reddit: text has title+body", "Kioxia soars" in rows[0]["text"])
    check("reddit: bad input safe", CI.parse_reddit_json(None) == [])


def test_stocktwits_parse():
    data = {"messages": [
        {"id": 99, "body": "$KXHCF adding", "created_at": "2026-06-25T10:00:00Z",
         "likes": {"total": 3}, "user": {"username": "trader"}},
        {"id": 100, "body": "", "likes": {"total": 0}},  # 空=除外
    ]}
    rows = CI.parse_stocktwits_json(data)
    check("stocktwits: 1 non-empty parsed", len(rows) == 1)
    check("stocktwits: id namespaced", rows[0]["id"] == "stocktwits:99")
    check("stocktwits: likes->votes", rows[0]["votes_yes"] == 3)
    check("stocktwits: Z stripped from ts", rows[0]["ts"] == "2026-06-25T10:00:00")
    check("stocktwits: bad input safe", CI.parse_stocktwits_json({}) == [])


# ---- source別集計 ------------------------------------------------------------
# ---- Yahoo 内部JSON API パーサ ----------------------------------------------
def test_yahoo_api_parse():
    html = ('...<script>var x="\\"jwtToken\\":\\"'
            'eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3ODM1MDQ2NTJ9.gn3XabcDEF_-123'
            '\\""</script> <a href="/quote/285A.T/forum/1348365">a</a>'
            '<a href="/quote/285A.T/forum/1348264">b</a>')
    jwt = C.extract_jwt(html)
    check("yahoo api: jwt extracted", jwt is not None and jwt.startswith("eyJ") and jwt.count(".") == 2)
    check("yahoo api: max mid", C.extract_max_mid(html) == 1348365)
    check("yahoo api: no-mid -> None", C.extract_max_mid("no ids here") is None)

    body = "傷を浅くしたい<br>75000で売り&hellip;<b>強気</b>"
    txt = C.clean_forum_body(body)
    check("yahoo api: br->newline", "\n" in txt)
    check("yahoo api: entity unescaped", "…" in txt and "&hellip;" not in txt)
    check("yahoo api: tags stripped", "<b>" not in txt and "強気" in txt)

    items = [
        {"part": 1348365, "body": "買い増した<br>決算に期待",
         "postDate": "2026/7/8 16:59", "good": 5, "bad": 1,
         "feelLabel": "strongest", "dispname": "u1"},
        {"part": 1348360, "body": "暴落するぞ", "postDate": "2026/7/8 16:50",
         "good": 0, "bad": 0, "parent": 1348365, "dispname": "u2"},
        {"part": 1348359, "body": "", "postDate": "2026/7/8 16:49"},  # 空body=除外
    ]
    rows = C.parse_forum_items(items)
    check("yahoo api: 2 parsed (empty skipped)", len(rows) == 2)
    check("yahoo api: id=str(part)", rows[0]["id"] == "1348365")
    check("yahoo api: ts ISO", rows[0]["ts"] == "2026-07-08T16:59:00")
    check("yahoo api: votes good/bad", rows[0]["votes_yes"] == 5 and rows[0]["votes_no"] == 1)
    check("yahoo api: feel kept", rows[0]["feel"] == "strongest")
    check("yahoo api: is_reply from parent", rows[1]["is_reply"] is True and rows[0]["is_reply"] is False)
    check("yahoo api: source=yahoo", rows[0]["source"] == "yahoo")
    check("yahoo api: min_part", C.min_part(items) == 1348359)
    check("yahoo api: empty items safe", C.parse_forum_items([]) == [])


def test_feel_vs_llm():
    rows = [
        {"feel": "strongest", "sentiment": "bullish"},   # agree
        {"feel": "strong", "sentiment": "bearish"},       # disagree
        {"feel": "weakest", "sentiment": "bearish"},       # agree
        {"feel": "both", "sentiment": "bullish"},          # both->neutral vs bullish: disagree
        {"feel": "both", "sentiment": "neutral"},          # both->neutral vs neutral: agree
        {"feel": None, "sentiment": "bullish"},            # 除外
    ]
    fx = S.feel_vs_llm(rows)
    check("feel_vs_llm: feel_dist counts", fx["feel_dist"]["strongest"] == 1 and fx["feel_dist"]["both"] == 2)
    check("feel_vs_llm: both counted in matrix (neutral row)", fx["matrix"]["neutral"]["neutral"] == 1)
    check("feel_vs_llm: agree=3 disagree=2", fx["agree"] == 3 and fx["disagree"] == 2)
    check("feel_vs_llm: agree_rate", fx["agree_rate"] == round(3/5, 3))
    check("feel_vs_llm: no-feel empty safe", S.feel_vs_llm([{"sentiment": "bullish"}])["agree_rate"] is None)


def test_feel_to_sentiment_both_maps_neutral():
    """snapshot._feel_to_sentiment: 'both'(様子見)は neutral として扱う(捨てない)。"""
    check("feel_to_sentiment: both->neutral", S._feel_to_sentiment("both") == "neutral")
    check("feel_to_sentiment: strong->bullish", S._feel_to_sentiment("strong") == "bullish")
    check("feel_to_sentiment: strongest->bullish", S._feel_to_sentiment("strongest") == "bullish")
    check("feel_to_sentiment: weak->bearish", S._feel_to_sentiment("weak") == "bearish")
    check("feel_to_sentiment: weakest->bearish", S._feel_to_sentiment("weakest") == "bearish")
    check("feel_to_sentiment: neutral->neutral", S._feel_to_sentiment("neutral") == "neutral")
    check("feel_to_sentiment: none/empty->None", S._feel_to_sentiment(None) is None
          and S._feel_to_sentiment("") is None)
    check("feel_to_sentiment: unknown value->None", S._feel_to_sentiment("weird") is None)


def test_interleave_by_source():
    rows = [{"source": "5ch", "id": i} for i in range(4)] + \
           [{"source": "stocktwits", "id": "s1"}, {"source": "yahoo", "id": "y1"}]
    out = A.interleave_by_source(rows)
    srcs = [r["source"] for r in out]
    check("interleave: no starvation (stocktwits within first 3)", "stocktwits" in srcs[:3])
    check("interleave: all rows preserved", len(out) == 6)
    check("interleave: 5ch internal order kept",
          [r["id"] for r in out if r["source"] == "5ch"] == [0, 1, 2, 3])


def test_iter_jsonl_torn_write_resilient():
    """★2026-08-19追加(おにや22:13投稿・重大障害調査)。raw_comments.jsonlはcatchup/
    フル実行が並行追記する共有ファイルのため、1行のwriteがちょうど部分的にしか
    flushされていないタイミングで読みが重なると、マルチバイトUTF-8文字が分断され
    「不正なバイト列(torn write)」になり得る。実際に2026-08-19 17:42〜22:10の
    約4時間半、analyze.py._iter_jsonl()がこのバイト列に遭遇してUnicodeDecodeErrorで
    停止し続け、analyzeパイプライン全体が完全に止まっていたことが実測で確認された。
    このテストは、ファイル中に不正なUTF-8バイト列を含む行が1行混入していても、
    その行だけをスキップして前後の正常な行は問題なく読めることを検証する
    (=修正前は、この不正バイト列に遭遇した時点でジェネレータ全体が例外を伝播させ
    以降の全行が読めなくなっていた)。"""
    import tempfile
    tmp = tempfile.mktemp(suffix=".jsonl")
    try:
        with open(tmp, "wb") as f:
            f.write(b'{"id": "a1", "text": "normal row 1"}\n')
            # 実際のtorn writeを模した不正バイト列(単独の0x8aは有効なUTF-8開始バイトでない)
            f.write(b'{"id": "a2", "text": "\xe9\x96\x8b\xe5\xa7\x8b\x8a broken row"}\n')
            f.write(b'{"id": "a3", "text": "normal row 3"}\n')
        rows = list(A._iter_jsonl(tmp))
        check("iter_jsonl: torn-write row skipped, not crashed", len(rows) == 2)
        check("iter_jsonl: row before the torn line still read", rows[0]["id"] == "a1")
        check("iter_jsonl: row after the torn line still read (this is the regression)",
              rows[1]["id"] == "a3")
    finally:
        import os as _os
        try:
            _os.remove(tmp)
        except OSError:
            pass


def test_pending_raw_newest_first():
    """★2026-08-19追加・同日中に実データで見落としを発見し是正(ユーザー承認済み・
    「10分更新に乗る情報/間に合わない情報の仕分け」)。

    初回実装(単純にreverse()してからinterleave_by_source()へ渡す方式)は、実runで
    「新しいはずのcatchupバッチに数時間前の投稿が混入する」事故を起こした
    (おにや13:53投稿で実測発覚=analyzed_at13:42のバッチにts10:19〜12:50が混在)。
    原因はソースごとに"直近で取得できた最新投稿"の鮮度が大きく異なる場合、
    ラウンドロビンだとソース間で古い投稿と新しい投稿が混ざってしまうこと。
    是正後は newest_first=True の時、interleave_by_source を使わず**全ソース横断で
    ts降順に厳密ソート**する(このテストが実際に検証する挙動)。
    実データ書込みなし(config.RAW_COMMENTS_PATHを一時ファイルへ差し替え)。"""
    import json, tempfile, os as _os
    orig_raw = config.RAW_COMMENTS_PATH
    orig_analyzed = config.ANALYZED_PATH
    d = tempfile.mkdtemp()
    try:
        config.RAW_COMMENTS_PATH = _os.path.join(d, "raw.jsonl")
        config.ANALYZED_PATH = _os.path.join(d, "analyzed.jsonl")
        # 単一ソース(yahoo)で古い順に4件書く(id=0が最古・id=3が最新)。
        with open(config.RAW_COMMENTS_PATH, "w", encoding="utf-8") as f:
            for i in range(4):
                f.write(json.dumps({"id": f"y{i}", "source": "yahoo", "text": "t",
                                    "ts": f"2026-08-19T10:0{i}:00"},
                                   ensure_ascii=False) + "\n")
        default_order = A._pending_raw()
        check("pending-raw: default (newest_first=False) keeps oldest-first order",
              [r["id"] for r in default_order] == ["y0", "y1", "y2", "y3"])

        newest_order = A._pending_raw(newest_first=True)
        check("pending-raw: newest_first=True sorts to newest-first order (by ts)",
              [r["id"] for r in newest_order] == ["y3", "y2", "y1", "y0"])

        # limitと組み合わせても正しく「最新N件」を取る(古い順の末尾N件ではない)。
        newest_2 = A._pending_raw(limit=2, newest_first=True)
        check("pending-raw: newest_first=True + limit=2 -> the 2 newest ids",
              [r["id"] for r in newest_2] == ["y3", "y2"])

        # ★実runで発覚した回帰ケース: ソースをまたぐと"直近取得できた最新"の鮮度が
        # 大きく食い違うことがある(5chが数時間分の古いスレッドをまとめて返す等)。
        # newest_first=True はソースを問わず真にts降順であるべき(ラウンドロビンで
        # 混ぜない)。yahooは13:00台の新しい投稿・5chは10:00台の古い投稿しか無い
        # 状況を再現する。
        with open(config.RAW_COMMENTS_PATH, "w", encoding="utf-8") as f:
            rows = [
                {"id": "5ch_old1", "source": "5ch", "ts": "2026-08-19T10:19:00"},
                {"id": "5ch_old2", "source": "5ch", "ts": "2026-08-19T10:24:00"},
                {"id": "yahoo_new1", "source": "yahoo", "ts": "2026-08-19T13:49:00"},
                {"id": "5ch_old3", "source": "5ch", "ts": "2026-08-19T10:25:00"},
                {"id": "yahoo_new2", "source": "yahoo", "ts": "2026-08-19T13:50:00"},
            ]
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        cross_source = A._pending_raw(limit=2, newest_first=True)
        check("pending-raw: newest_first=True picks globally-newest across sources "
              "(not round-robin-diluted by a stale source)",
              [r["id"] for r in cross_source] == ["yahoo_new2", "yahoo_new1"])

        # ts が非ISO(5chの「Over 1000」等のスレッド状態文字列)の行は最古扱いになり、
        # 誤って"最新"として先頭に来ない。
        with open(config.RAW_COMMENTS_PATH, "w", encoding="utf-8") as f:
            rows2 = [
                {"id": "garbled_ts", "source": "5ch", "ts": "Over 1000"},
                {"id": "real_new", "source": "yahoo", "ts": "2026-08-19T13:50:00"},
            ]
            for r in rows2:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        garbled_check = A._pending_raw(newest_first=True)
        check("pending-raw: newest_first=True treats non-ISO ts as oldest (not newest)",
              [r["id"] for r in garbled_check] == ["real_new", "garbled_ts"])
    finally:
        config.RAW_COMMENTS_PATH = orig_raw
        config.ANALYZED_PATH = orig_analyzed


def test_dedupe_analyzed_by_id():
    rows = [
        {"id": "a", "sentiment": "bullish"},
        {"id": "b", "sentiment": "bearish"},
        {"id": "a", "sentiment": "neutral"},   # 後勝ち
    ]
    out = S.dedupe_analyzed_by_id(rows)
    check("dedupe_analyzed: collapses to 2", len(out) == 2)
    amap = {r["id"]: r for r in out}
    check("dedupe_analyzed: keeps last", amap["a"]["sentiment"] == "neutral")


def test_source_breakdown():
    rows = [
        {"source": "yahoo", "meaningful": True, "sentiment": "bullish"},
        {"source": "yahoo", "meaningful": False, "sentiment": "bullish"},
        {"source": "5ch", "meaningful": True, "sentiment": "bearish"},
        {"source": "reddit", "meaningful": True, "sentiment": "neutral"},
    ]
    sb = S.source_breakdown(rows)
    check("source_bd: yahoo count=2 meaningful=1", sb["yahoo"]["count"] == 2 and sb["yahoo"]["meaningful"] == 1)
    check("source_bd: 5ch bearish=1", sb["5ch"]["bearish"] == 1)
    check("source_bd: reddit present", sb["reddit"]["neutral"] == 1)


# ---- tokenizer graceful fallback --------------------------------------------
def test_tokenizer_fallback():
    mode = A.resolve_tokenizer("word", n_texts=5)
    check("tokenizer: resolves to word|char", mode in ("word", "char"))
    check("tokenizer: unknown mode -> char", A.resolve_tokenizer("bogus") == "char")
    tw = A.tokenize_words("決算に期待している")
    check("tokenizer: tokenize_words returns str or None", (tw is None) or isinstance(tw, str))
    # 両モードでクラスタリングが落ちない(件数分ラベルを返す)
    texts = ["決算に期待して買い増し", "決算が楽しみ買い増し", "決算内容に期待",
             "暴落するから損切り", "暴落が怖い損切り", "暴落して損切り済み"]
    lab_w = A.cluster_texts(texts, tokenizer="word")
    lab_c = A.cluster_texts(texts, tokenizer="char")
    check("tokenizer: word cluster len ok", len(lab_w) == len(texts))
    check("tokenizer: char cluster len ok", len(lab_c) == len(texts))
    check("tokenizer: word forms >=2 clusters", len(set(lab_w)) >= 2)


# ---- 非LLM 辞書分類(BBS_USE_LLM=0 の既定経路) ------------------------------
def test_classify_lexicon():
    # 強気語 -> bullish
    check("lexicon: bullish word -> bullish",
          A.classify_lexicon("ここから爆上げ、握力試される。買い増しした") == "bullish")
    # 弱気語 -> bearish
    check("lexicon: bearish word -> bearish",
          A.classify_lexicon("損切りして退場、まさに阿鼻叫喚の暴落") == "bearish")
    # 語なし -> neutral(feelなし)
    check("lexicon: no lexicon word -> neutral",
          A.classify_lexicon("今日の出来高はどのくらいだろう") == "neutral")
    # 同数/ゼロ -> feel フォールバック
    check("lexicon: tie/zero falls back to feel(strong->bullish)",
          A.classify_lexicon("特になし", feel="strongest") == "bullish")
    check("lexicon: tie/zero falls back to feel(weak->bearish)",
          A.classify_lexicon("特になし", feel="weakest") == "bearish")
    check("lexicon: feel None -> neutral",
          A.classify_lexicon("特になし", feel=None) == "neutral")
    # 直前否定で符号反転(強気語+否定 -> bearish 寄り)
    check("lexicon: negation flips bullish word",
          A.classify_lexicon("全然上げない、弱い") == "bearish")
    # 意味あり判定(無内容/挨拶/絵文字のみは False)
    check("meaningful_lexicon: greeting-only -> False",
          A.is_meaningful_lexicon("おはようございます") is False)
    check("meaningful_lexicon: too short -> False", A.is_meaningful_lexicon("乙") is False)
    check("meaningful_lexicon: substantive -> True",
          A.is_meaningful_lexicon("決算跨ぎで信用買いが増えているのが気になる") is True)
    # TF-IDF 見出しは API 非依存で文字列を返す
    lab = A.label_from_tfidf(["決算に期待して買い増し", "決算が楽しみ、買い増し", "決算内容に期待"])
    check("label_from_tfidf: returns non-empty str", isinstance(lab, str) and len(lab) > 0)
    check("label_from_tfidf: empty -> その他", A.label_from_tfidf([]) == "その他")


# ---- analyze_batch_with_lmstudio_hybrid(辞書プレフィルタ+LM Studio) ----------
# 実サーバーへ接続せず post_fn 差し替えでLLM呼び出し部分だけモックする。
class _FakeLmstudioResp:
    """requests.Response 互換の最小フェイク(raise_for_status/json のみ)。"""
    def __init__(self, arr):
        self._arr = arr

    def raise_for_status(self):
        pass

    def json(self):
        import json as _json
        content = _json.dumps(self._arr, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


def _make_fake_post(arr, calls):
    def _post(url, json=None, timeout=None):
        calls.append(json)
        return _FakeLmstudioResp(arr)
    return _post


def test_lmstudio_hybrid():
    # ケース1: 全件「辞書だけで確信を持って判定できる」または「明確に無内容」-> LLM不呼び出し
    calls = []
    batch = [
        {"i": 0, "text": "ここから爆上げ、握力試される。買い増しした"},  # bull_hits>bear_hits
        {"i": 1, "text": "損切りして退場、まさに阿鼻叫喚の暴落"},          # bear_hits>bull_hits
        {"i": 2, "text": "乙"},                                        # is_meaningful_lexicon=False
    ]
    res, usage = A.analyze_batch_with_lmstudio_hybrid(batch, post_fn=_make_fake_post([], calls))
    check("hybrid: all-confident/not-meaningful -> no LLM call", len(calls) == 0)
    check("hybrid: usage is None (contract same as analyze_batch_with_local_llm)", usage is None)
    check("hybrid: confident bullish via lexicon",
          res[0]["sentiment"] == "bullish" and res[0]["meaningful"] is True and res[0]["_source"] == "lexicon")
    check("hybrid: confident bearish via lexicon",
          res[1]["sentiment"] == "bearish" and res[1]["meaningful"] is True and res[1]["_source"] == "lexicon")
    check("hybrid: not-meaningful excluded via lexicon (no LLM)",
          res[2]["meaningful"] is False and res[2]["_source"] == "lexicon")

    # ケース2: 一部だけ曖昧(bull_hits==bear_hits==0) -> 曖昧分だけLLMへ1回
    calls = []
    batch2 = [
        {"i": 0, "text": "ここから爆上げ、握力試される。買い増しした"},   # confident bull
        {"i": 1, "text": "今日は決算発表があるらしい"},                    # 曖昧(0-0)
    ]
    llm_arr = [{"i": 1, "meaningful": True, "sentiment": "neutral"}]
    res2, _ = A.analyze_batch_with_lmstudio_hybrid(batch2, post_fn=_make_fake_post(llm_arr, calls))
    check("hybrid: partial-ambiguous -> exactly one LLM call", len(calls) == 1)
    sent_prompt = calls[0]["messages"][1]["content"]
    check("hybrid: confident item excluded from LLM prompt", "0: ここから" not in sent_prompt)
    check("hybrid: ambiguous item included in LLM prompt", "1: 今日は決算発表があるらしい" in sent_prompt)
    check("hybrid: confident item stays lexicon-sourced",
          res2[0]["_source"] == "lexicon" and res2[0]["sentiment"] == "bullish")
    check("hybrid: ambiguous item sourced from lmstudio",
          res2[1]["_source"] == "lmstudio" and res2[1]["sentiment"] == "neutral"
          and res2[1]["meaningful"] is True)

    # ケース3: 全件曖昧 -> 全件LLMへ1回
    calls = []
    batch3 = [
        {"i": 0, "text": "今日は決算発表があるらしい"},
        {"i": 5, "text": "明日の値動きが気になるところ"},
    ]
    llm_arr3 = [
        {"i": 0, "meaningful": True, "sentiment": "neutral"},
        {"i": 5, "meaningful": True, "sentiment": "bullish"},
    ]
    res3, _ = A.analyze_batch_with_lmstudio_hybrid(batch3, post_fn=_make_fake_post(llm_arr3, calls))
    check("hybrid: all-ambiguous -> exactly one LLM call", len(calls) == 1)
    check("hybrid: all-ambiguous results both from lmstudio",
          res3[0]["_source"] == "lmstudio" and res3[5]["_source"] == "lmstudio")
    check("hybrid: all-ambiguous sentiment passthrough",
          res3[0]["sentiment"] == "neutral" and res3[5]["sentiment"] == "bullish")

    # config.LOCAL_LLM_ENDPOINTS["lmstudio"] のエントリ存在確認(base_url/model)
    ep = config.LOCAL_LLM_ENDPOINTS.get("lmstudio", {})
    check("config: lmstudio endpoint has base_url/model", bool(ep.get("base_url")) and bool(ep.get("model")))
    check("config: lmstudio is a valid BBS_LLM_BACKEND choice", "lmstudio" in config._BBS_LLM_BACKEND_CHOICES)

    # ---- ★2026-08-17: チャンク分割(config.LMSTUDIO_HYBRID_CHUNK_SIZE)の検証 --------
    # 曖昧アイテム全件を1回のバッチ呼び出しに集約すると、実運用規模(n=400級)で
    # LM Studio側のコンテキスト長上限を超過しHTTP 400になると実測判明したため導入
    # (2026-08-17・CROSS_PROJECT_LOG参照)。この件数ごとに複数回のAPI呼び出しへ分割される。
    import re as _re_chunk

    def _idx_from_prompt(prompt):
        """簡素化プロンプトの "i: text" 行から i の一覧を抽出する(テスト用の簡易パーサ)。"""
        return [int(m) for m in _re_chunk.findall(r"^(\d+):", prompt, _re_chunk.MULTILINE)]

    def _make_chunk_echo_post(sentiment="neutral"):
        """チャンク内の各アイテムを sentiment でそのまま返すモック(呼ばれた回数を検証しやすくする)。"""
        def _post(url, json=None, timeout=None):
            idxs = _idx_from_prompt(json["messages"][1]["content"])
            arr = [{"i": idx, "meaningful": True, "sentiment": sentiment} for idx in idxs]
            return _FakeLmstudioResp(arr)
        return _post

    # ケース4: 曖昧アイテムがchunk_sizeを超えたら複数回のLLM呼び出しに分割される
    calls4 = []
    ambiguous5 = [{"i": n, "text": "今日は決算発表があるらしい"} for n in range(5)]  # 全件曖昧(0-0)
    orig_chunk_size = config.LMSTUDIO_HYBRID_CHUNK_SIZE
    config.LMSTUDIO_HYBRID_CHUNK_SIZE = 2  # 一時的に小さくしてチャンク分割を発生させる
    try:
        echo = _make_chunk_echo_post("neutral")

        def _counting_post(url, json=None, timeout=None):
            calls4.append(json)
            return echo(url, json=json, timeout=timeout)
        res4, usage4 = A.analyze_batch_with_lmstudio_hybrid(ambiguous5, post_fn=_counting_post)
        check("hybrid-chunk: 5 items / chunk_size=2 -> 3 LLM calls (2+2+1)", len(calls4) == 3)
        check("hybrid-chunk: each call sends at most chunk_size items",
              all(len(_idx_from_prompt(c["messages"][1]["content"])) <= 2 for c in calls4))
        check("hybrid-chunk: all 5 items present in output", all(n in res4 for n in range(5)))
        check("hybrid-chunk: all sourced from lmstudio", all(res4[n]["_source"] == "lmstudio" for n in range(5)))
        check("hybrid-chunk: usage is None (contract unchanged)", usage4 is None)
    finally:
        config.LMSTUDIO_HYBRID_CHUNK_SIZE = orig_chunk_size

    # ケース5: 1チャンクが失敗しても他チャンクの結果は保持される(オールオアナッシング回避)
    calls5 = []
    ambiguous4 = [{"i": n, "text": "今日は決算発表があるらしい"} for n in range(4)]  # 全件曖昧(0-0)
    orig_chunk_size = config.LMSTUDIO_HYBRID_CHUNK_SIZE
    config.LMSTUDIO_HYBRID_CHUNK_SIZE = 2
    try:
        echo_bull = _make_chunk_echo_post("bullish")
        call_no = [0]

        def _failing_first_post(url, json=None, timeout=None):
            call_no[0] += 1
            calls5.append(json)
            if call_no[0] == 1:
                raise RuntimeError("simulated: n_keep >= n_ctx (context length exceeded)")
            return echo_bull(url, json=json, timeout=timeout)
        res5, usage5 = A.analyze_batch_with_lmstudio_hybrid(ambiguous4, post_fn=_failing_first_post)
        check("hybrid-chunk-fail: exception in chunk 1 does not propagate", True)
        check("hybrid-chunk-fail: both chunks were attempted (2 calls)", len(calls5) == 2)
        check("hybrid-chunk-fail: failed chunk's items (0,1) absent from output",
              0 not in res5 and 1 not in res5)
        check("hybrid-chunk-fail: surviving chunk's items (2,3) present with correct sentiment",
              res5.get(2, {}).get("sentiment") == "bullish" and res5.get(3, {}).get("sentiment") == "bullish"
              and res5.get(2, {}).get("_source") == "lmstudio")
        check("hybrid-chunk-fail: usage is None (contract unchanged even on partial failure)", usage5 is None)
    finally:
        config.LMSTUDIO_HYBRID_CHUNK_SIZE = orig_chunk_size

    # ケース5.5: ★2026-08-17 壁時計デッドライン(config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC)。
    #   件数上限(LMSTUDIO_MAX_COMMENTS_PER_RUN)は「1件あたりの平均所要時間」という仮定からの
    #   逆算に過ぎず、実際の所要時間がその仮定を超えると総処理時間に上限がかからない問題が
    #   実runで発覚(21:00 runがLM Studio推論の実際の重さで40分予算を超過)。
    #   予算を既に超過した状態(負数)で呼ぶと、1回もLLM呼び出しをせず即座に諦めることを確認。
    orig_budget_deadline = config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC
    ambiguous_dl = [{"i": n, "text": "今日は決算発表があるらしい"} for n in range(4)]
    config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC = -1
    try:
        def _fail_if_called(url, json=None, timeout=None):
            raise AssertionError("post_fn should never be called (deadline already past)")
        res_dl, usage_dl = A.analyze_batch_with_lmstudio_hybrid(ambiguous_dl, post_fn=_fail_if_called)
        check("hybrid-deadline: already-expired budget -> no LLM call at all", res_dl == {})
        check("hybrid-deadline: usage is None (contract unchanged)", usage_dl is None)
    finally:
        config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC = orig_budget_deadline

    # ケース5.6: 予算に十分な余裕があれば全チャンクが通常通り処理される(デッドライン導入による
    #   回帰が無いことの確認)。
    config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC = 9999
    orig_chunk_size = config.LMSTUDIO_HYBRID_CHUNK_SIZE
    config.LMSTUDIO_HYBRID_CHUNK_SIZE = 2
    try:
        calls_ok = []
        echo_bull_ok = _make_chunk_echo_post("bullish")

        def _counting_post(url, json=None, timeout=None):
            calls_ok.append(json)
            return echo_bull_ok(url, json=json, timeout=timeout)
        res_ok, _ = A.analyze_batch_with_lmstudio_hybrid(ambiguous_dl, post_fn=_counting_post)
        check("hybrid-deadline: ample budget -> all chunks processed (2 calls)", len(calls_ok) == 2)
        check("hybrid-deadline: ample budget -> all items present", all(n in res_ok for n in range(4)))
    finally:
        config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC = orig_budget_deadline
        config.LMSTUDIO_HYBRID_CHUNK_SIZE = orig_chunk_size

    # ケース6: ★2026-08-17 トークン予算ベースの分割(_split_ambiguous_into_chunks)そのものを検証。
    # 件数上限(LMSTUDIO_HYBRID_CHUNK_SIZE)は十分大きいままでも、トークン予算
    # (LMSTUDIO_HYBRID_TOKEN_BUDGET)を絞ると、投稿文の長さに応じて分割されることを確認する
    # (固定件数チャンクだと同じ件数でも文字数次第でコンテキスト長超過を防げないと実データで
    # 判明したため、この方式へ変更した・CROSS_PROJECT_LOG参照)。
    orig_budget = config.LMSTUDIO_HYBRID_TOKEN_BUDGET
    orig_chunk_size = config.LMSTUDIO_HYBRID_CHUNK_SIZE
    orig_out_tokens = config.LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM
    config.LMSTUDIO_HYBRID_CHUNK_SIZE = 1000  # 件数上限では絶対に区切られないようにする
    config.LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM = 0  # 出力分の見積もりは今回無視して単純化
    try:
        # 短文5件(推定トークン小) -> 予算を大きくすれば1チャンク
        short_items = [{"i": n, "text": "うん"} for n in range(5)]
        config.LMSTUDIO_HYBRID_TOKEN_BUDGET = 10000
        chunks_a = A._split_ambiguous_into_chunks(short_items)
        check("token-chunk: short items + large budget -> 1 chunk", len(chunks_a) == 1)
        check("token-chunk: 1 chunk contains all 5 items", len(chunks_a[0]) == 5)

        # 同じ5件でも予算を極端に絞ると複数チャンクに分かれる
        config.LMSTUDIO_HYBRID_TOKEN_BUDGET = 5
        chunks_b = A._split_ambiguous_into_chunks(short_items)
        check("token-chunk: same items + tiny budget -> multiple chunks", len(chunks_b) > 1)
        check("token-chunk: no item lost across chunks",
              sorted(it["i"] for c in chunks_b for it in c) == [0, 1, 2, 3, 4])
        check("token-chunk: every chunk non-empty (no infinite loop / empty chunk)",
              all(len(c) >= 1 for c in chunks_b))

        # 長文1件だけで予算を超える極端ケース -> それでも1件は必ず入る(空チャンクにならない)
        huge_item = [{"i": 99, "text": "あ" * 500}]
        config.LMSTUDIO_HYBRID_TOKEN_BUDGET = 5
        chunks_c = A._split_ambiguous_into_chunks(huge_item)
        check("token-chunk: single oversized item still gets its own chunk (no infinite loop)",
              len(chunks_c) == 1 and len(chunks_c[0]) == 1 and chunks_c[0][0]["i"] == 99)

        # 空リスト -> 空リスト
        check("token-chunk: empty ambiguous list -> no chunks", A._split_ambiguous_into_chunks([]) == [])
    finally:
        config.LMSTUDIO_HYBRID_TOKEN_BUDGET = orig_budget
        config.LMSTUDIO_HYBRID_CHUNK_SIZE = orig_chunk_size
        config.LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM = orig_out_tokens

    # config: チャンク関連定数の存在・型確認
    check("config: LMSTUDIO_HYBRID_CHUNK_SIZE is a positive int",
          isinstance(config.LMSTUDIO_HYBRID_CHUNK_SIZE, int) and config.LMSTUDIO_HYBRID_CHUNK_SIZE > 0)
    check("config: LMSTUDIO_HYBRID_TOKEN_BUDGET is a positive int",
          isinstance(config.LMSTUDIO_HYBRID_TOKEN_BUDGET, int) and config.LMSTUDIO_HYBRID_TOKEN_BUDGET > 0)
    check("config: LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM is a non-negative int",
          isinstance(config.LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM, int)
          and config.LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM >= 0)


# ---- price_fetch(fixture JSON) ----------------------------------------------
PRICE_FIXTURE = {
    "chart": {"error": None, "result": [{
        "meta": {"symbol": "285A.T", "currency": "JPY", "gmtoffset": 32400,
                 "timezone": "JST", "regularMarketPrice": 71870.0,
                 "previousClose": 72400.0, "chartPreviousClose": 76260.0,
                 "dataGranularity": "5m"},
        "timestamp": [100, 200, 300, 400],
        "indicators": {
            "quote": [{"open": [1.0, 2.0, None, 4.0], "high": [1.5, 2.5, None, 4.5],
                        "low": [0.5, 1.5, None, 3.5], "close": [1.2, 2.2, None, 4.2],
                        "volume": [10, 20, None, 0]}],
            "adjclose": [{"adjclose": [1.2, 2.2, None, 4.2]}],
        },
    }]}
}


def test_analyze_time_budget_sec_param():
    """★2026-08-19追加(おにや11:11投稿(a)対応): analyze()のtime_budget_sec引数が
    実際にデッドライン計算へ反映されることを直接検証する(config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC
    をグローバルに書き換えずに、呼び出し単位で予算を上書きできることの確認=
    run_once.pyのcatchupモードがCATCHUP_ANALYZE_TIME_BUDGET_SECを渡す設計の土台)。
    実サーバー接続なし(post_fnモック)・実データ書込みなし(一時ディレクトリ)。"""
    import json, tempfile, os as _os
    orig_raw = config.RAW_COMMENTS_PATH
    orig_analyzed = config.ANALYZED_PATH
    orig_log = config.LOG_PATH
    orig_backend = config.BBS_LLM_BACKEND
    orig_batch_size = config.ANALYZE_BATCH_SIZE
    d = tempfile.mkdtemp()
    try:
        config.RAW_COMMENTS_PATH = _os.path.join(d, "raw.jsonl")
        config.ANALYZED_PATH = _os.path.join(d, "analyzed.jsonl")
        config.LOG_PATH = _os.path.join(d, "run.log")
        config.BBS_LLM_BACKEND = "lmstudio"
        config.ANALYZE_BATCH_SIZE = 2
        with open(config.RAW_COMMENTS_PATH, "w", encoding="utf-8") as f:
            for i in range(6):
                f.write(json.dumps({"id": f"y{i}", "ts": "2026-08-19T09:00:00",
                                    "source": "yahoo", "text": "今日は決算発表があるらしい"},
                                   ensure_ascii=False) + "\n")

        # time_budget_sec=-1(既に超過)を明示指定 -> 1回もLLMを呼ばず全件が未分析のまま残る
        # (config.LMSTUDIO_ANALYZE_TIME_BUDGET_SECは既定値[正]のまま=グローバルは書き換えない)
        def _fail_if_called(url, json=None, timeout=None):
            raise AssertionError("should never be called (time_budget_sec=-1 already expired)")
        import unittest.mock as _mock
        with _mock.patch("requests.post", _fail_if_called):
            summary = A.analyze(time_budget_sec=-1)
        check("analyze-budget: time_budget_sec=-1 -> analyzed=0 (all deadline-skipped)",
              summary["analyzed"] == 0)
        an_ids = set()
        with open(config.ANALYZED_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    an_ids.add(json.loads(line)["id"])
        check("analyze-budget: nothing written to analyzed.jsonl (stays in backlog)",
              len(an_ids) == 0)

        # time_budget_sec省略 -> config.LMSTUDIO_ANALYZE_TIME_BUDGET_SEC(既定・正の値)を
        # 使うため、全件処理される(回帰なし)。
        def _echo_bull(url, json=None, timeout=None):
            import re as _re
            body = json["messages"][1]["content"]
            idxs = [int(m) for m in _re.findall(r"^(\d+):", body, flags=_re.M)]

            class _R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {"choices": [{"message": {"content": __import__("json").dumps(
                        [{"i": i, "meaningful": True, "sentiment": "bullish"} for i in idxs])}}]}
            return _R()
        with _mock.patch("requests.post", _echo_bull):
            summary2 = A.analyze()
        check("analyze-budget: time_budget_sec omitted -> default budget processes all 6",
              summary2["analyzed"] == 6)
    finally:
        config.RAW_COMMENTS_PATH = orig_raw
        config.ANALYZED_PATH = orig_analyzed
        config.LOG_PATH = orig_log
        config.BBS_LLM_BACKEND = orig_backend
        config.ANALYZE_BATCH_SIZE = orig_batch_size


def test_price_parse():
    p = PF.parse_chart_json(PRICE_FIXTURE)
    check("price: no error", "error" not in p)
    check("price: null-close slot excluded", len(p["bars"]) == 3)
    check("price: bar fields", p["bars"][0]["close"] == 1.2 and p["bars"][0]["volume"] == 10)
    check("price: adjclose kept", p["bars"][2]["adjclose"] == 4.2)
    check("price: volume None->0", p["bars"][2]["volume"] == 0)
    check("price: meta passthrough", p["meta"]["regularMarketPrice"] == 71870.0)
    check("price: error response",
          PF.parse_chart_json({"chart": {"error": "bad"}}).get("error") == "bad")
    check("price: garbage safe", PF.parse_chart_json(None).get("error") is not None)
    last, prev, chg = PF.latest_price_change(p)
    check("price: change pct", last == 71870.0 and prev == 72400.0
          and abs(chg - round((71870 - 72400) / 72400 * 100, 2)) < 1e-9)
    check("price: change None-safe", PF.latest_price_change(None) == (None, None, None))


def test_price_adr_pts_parse():
    """★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を分析
    するように」)。nikkei225jp.comのADR/PTSフィード(`var ADRm=[[ts_ms,tse,pts,adr_yen,
    adr_usd],...];`というJS配列リテラル・空欄は空文字でJSON非互換)のパーサーを
    実データの実際の書式(2026-08-19実測で採取)に基づく固定文字列で検証する。"""
    text = (
        "var ADRm = [\n"
        "[1786540500000,,51400,,],\n"
        "[1786541400000,,,51866,32.65],\n"
        "[1786541520000,,51700,51979,32.72],\n"
        "[1787121000000,49950,50000,,]\n"
        "];"
    )
    rows = PF.parse_adr_pts_js(text)
    check("adr_pts: 4 rows parsed", len(rows) == 4)
    check("adr_pts: ts is epoch seconds (ms/1000)", rows[0]["ts"] == 1786540500)
    check("adr_pts: empty fields -> None (not 0/empty string)",
          rows[0]["tse"] is None and rows[0]["adr_yen"] is None and rows[0]["adr_usd"] is None)
    check("adr_pts: pts value parsed", rows[0]["pts"] == 51400.0)
    check("adr_pts: adr_yen/adr_usd both parsed on the same row",
          rows[1]["adr_yen"] == 51866.0 and rows[1]["adr_usd"] == 32.65 and rows[1]["tse"] is None)
    check("adr_pts: row with all 4 values populated",
          rows[2]["pts"] == 51700.0 and rows[2]["adr_yen"] == 51979.0 and rows[2]["adr_usd"] == 32.72)
    check("adr_pts: tse present (regular-session tick)", rows[3]["tse"] == 49950.0)
    check("adr_pts: empty text -> empty list (fail-soft)", PF.parse_adr_pts_js("") == [])
    check("adr_pts: garbage text -> empty list (fail-soft)", PF.parse_adr_pts_js("not json at all") == [])


# ---- jsonl_window(直近読込・末尾逆読み) ---------------------------------------
def test_jsonl_window():
    import json as _json
    import tempfile
    import datetime as _dt

    base = _dt.datetime(2026, 1, 1, 9, 0, 0)
    rows = [{"id": i, "ts": (base + _dt.timedelta(hours=i)).isoformat(), "v": i}
            for i in range(300)]
    tmp = tempfile.mktemp(suffix=".jsonl")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(_json.dumps(r, ensure_ascii=False) + "\n")

        full = JW.read_jsonl_full(tmp)
        check("jsonl_window: full read matches", full == rows)

        tail5 = JW.read_jsonl_tail(tmp, 5)
        check("jsonl_window: tail(5) = last5 (oldest->newest)", tail5 == rows[-5:])
        check("jsonl_window: tail(0) empty", JW.read_jsonl_tail(tmp, 0) == [])

        now = base + _dt.timedelta(hours=299) + _dt.timedelta(minutes=1)
        cutoff = (now - _dt.timedelta(days=5)).isoformat()
        expected = [r for r in rows if r["ts"] >= cutoff]
        recent = JW.read_jsonl_recent(tmp, days=5, now=now)
        check("jsonl_window: recent(5d) matches date-window subset",
              recent == expected and 0 < len(expected) < len(rows))

        # 逆順に読んでもchunk境界でマルチバイト文字(UTF-8)を壊さないこと(小さいchunk_sizeで確認)。
        rev_lines = list(JW._iter_lines_reverse(tmp, chunk_size=37))
        parsed = [_json.loads(l) for l in reversed(rev_lines)]
        check("jsonl_window: tiny chunk_size reverse read matches forward order",
              parsed == rows)

        # fail-soft: 孤立した逆行行(短いノイズ)は打ち切らず、正しい直近集合を返す。
        noisy_rows = (rows[:250]
                      + [{"id": f"noise{i}", "ts": "2020-01-01T00:00:00", "v": -1}
                         for i in range(20)]
                      + rows[250:])
        tmp2 = tempfile.mktemp(suffix=".jsonl")
        with open(tmp2, "w", encoding="utf-8") as f:
            for r in noisy_rows:
                f.write(_json.dumps(r, ensure_ascii=False) + "\n")
        recent_noisy = JW.read_jsonl_recent(tmp2, days=5, now=now, stop_after_old=500)
        check("jsonl_window: isolated reversed rows tolerated (no data loss)",
              recent_noisy == expected)
        os.remove(tmp2)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    check("jsonl_window: missing file -> [] (full)", JW.read_jsonl_full("__nope__.jsonl") == [])
    check("jsonl_window: missing file -> [] (tail)", JW.read_jsonl_tail("__nope__.jsonl", 5) == [])
    check("jsonl_window: missing file -> [] (recent)",
          JW.read_jsonl_recent("__nope__.jsonl", days=5) == [])

    # ★2026-08-19追加(おにや22:13投稿・重大障害調査): read_jsonl_full()が1行の
    # torn write(不正なUTF-8バイト列)に遭遇しても、その行だけをスキップして
    # 前後の正常な行は読めること(修正前は外側のtry/exceptがファイル全体の
    # イテレーションを丸ごと打ち切り、破損箇所より後ろの行が静かに欠落していた)。
    import tempfile as _tf
    tmp3 = _tf.mktemp(suffix=".jsonl")
    try:
        with open(tmp3, "wb") as f:
            f.write(b'{"id": "b1"}\n')
            f.write(b'{"id": "b2", "text": "\xe9\x96\x8b\xe5\xa7\x8b\x8a broken"}\n')
            f.write(b'{"id": "b3"}\n')
        full3 = JW.read_jsonl_full(tmp3)
        check("jsonl_window: read_jsonl_full torn-write row skipped, not crashed",
              len(full3) == 2)
        check("jsonl_window: read_jsonl_full row after torn line still read",
              [r["id"] for r in full3] == ["b1", "b3"])
    finally:
        if os.path.exists(tmp3):
            os.remove(tmp3)


# ---- signals ------------------------------------------------------------------
def _mk(ts, sent, text="", votes=0, author=None, meaningful=True):
    return {"ts": ts, "sentiment": sent, "text": text, "votes_yes": votes,
            "author": author, "meaningful": meaningful}


def test_sig_lexicon():
    texts = ["今日で退場します。損切りしました", "爆益で祭りだ🚀", "暴落くるぞ逃げろ",
             "決算に期待", ""]
    lx = SG.lexicon_counts(texts)
    check("lex: capitulation hit", lx["capitulation"]["hits"] == 1)
    check("lex: euphoria hit", lx["euphoria"]["hits"] == 1)
    check("lex: aori hit", lx["aori"]["hits"] == 1)
    check("lex: index per100", lx["capitulation"]["index"] == 20.0)
    check("lex: clean text no hit", SG.lexicon_counts(["決算に期待"])["euphoria"]["hits"] == 0)
    check("lex: empty safe", SG.lexicon_counts([])["capitulation"]["index"] == 0.0)


def test_sig_zscore():
    z = SG.trailing_zscore([10, 12, 10, 11, 10, 20], window=5)
    check("z: spike positive", z is not None and z > 3)
    check("z: short history None", SG.trailing_zscore([1, 2], window=20) is None)
    check("z: zero variance None", SG.trailing_zscore([5, 5, 5, 5, 5, 5], window=5) is None)


def test_sig_other_symbols():
    r = SG.other_symbol_ratio(["7203も買った", "2026年は上がる", "サムスンが好調",
                                "キオクシア最高", "72400円で売った"])
    # 7203(コード) と サムスン の2件のみ。2026=年号除外。72400=5桁で非該当。
    check("other: hits=2", r["hits"] == 2)
    check("other: ratio", abs(r["ratio"] - 0.4) < 1e-9)
    check("other: empty safe", SG.other_symbol_ratio([])["ratio"] == 0.0)


def test_sig_resolved_sentiment():
    """resolved_sentiment: 自己申告(feel)優先・様子見/無し/未知値はsentimentへフォールバック。"""
    check("resolved: feel=strong overrides AI bearish",
          SG.resolved_sentiment({"feel": "strong", "sentiment": "bearish"}) == "bullish")
    check("resolved: feel=strongest overrides AI bearish",
          SG.resolved_sentiment({"feel": "strongest", "sentiment": "bearish"}) == "bullish")
    check("resolved: feel=weak overrides AI bullish",
          SG.resolved_sentiment({"feel": "weak", "sentiment": "bullish"}) == "bearish")
    check("resolved: feel=weakest overrides AI bullish",
          SG.resolved_sentiment({"feel": "weakest", "sentiment": "bullish"}) == "bearish")
    check("resolved: feel=both falls back to sentiment",
          SG.resolved_sentiment({"feel": "both", "sentiment": "neutral"}) == "neutral")
    check("resolved: feel absent falls back to sentiment",
          SG.resolved_sentiment({"sentiment": "bullish"}) == "bullish")
    check("resolved: feel unknown value falls back to sentiment",
          SG.resolved_sentiment({"feel": "weird", "sentiment": "bearish"}) == "bearish")
    check("resolved: both feel and sentiment absent -> None",
          SG.resolved_sentiment({}) is None)


def test_sig_ratios_votes_hourly_feel_priority():
    """sentiment_ratios/votes_concentration/hourly_buckets が resolved_sentiment 経由になっていること。"""
    rows = [
        # A: feelなし -> AI判定(bearish)のままフォールバック(対照)
        {"ts": "2026-07-08T09:00:00", "sentiment": "bearish", "votes_yes": 20,
         "author": "a", "meaningful": True},
        # B: AI判定はbearishだが自己申告strong(強気)->bullishとして数えられるべき
        {"ts": "2026-07-08T09:05:00", "sentiment": "bearish", "votes_yes": 50,
         "author": "b", "meaningful": True, "feel": "strong"},
        # C: AI判定はbullishだが自己申告weakest(弱気)->bearishとして数えられるべき
        {"ts": "2026-07-08T09:30:00", "sentiment": "bullish", "votes_yes": 5,
         "author": "c", "meaningful": True, "feel": "weakest"},
        # D: 様子見(both)はAI判定(neutral)のままフォールバック
        {"ts": "2026-07-08T10:00:00", "sentiment": "neutral", "votes_yes": 0,
         "author": "d", "meaningful": True, "feel": "both"},
    ]
    # n=4: bull=1(B) bear=2(A,C) neutral=1(D)
    ratios = SG.sentiment_ratios(rows)
    check("ratios: feel-flip(B) counted as bull", ratios["bull_ratio"] == round(1 / 4, 3))
    check("ratios: no-feel(A)+feel-flip(C) counted as bear", ratios["bear_ratio"] == round(2 / 4, 3))
    check("ratios: both(D) counted as neutral", ratios["neutral_ratio"] == round(1 / 4, 3))
    votes = SG.votes_concentration(rows)
    check("votes: bull_votes uses resolved (B: bearish->bullish via feel, votes=50)",
          votes["bull_votes"] == 50)
    check("votes: bear_votes uses resolved (A:20 + C: bullish->bearish via feel:5 = 25)",
          votes["bear_votes"] == 25)
    hb = SG.hourly_buckets(rows, day="2026-07-08")
    check("hourly: 09h bullish=1(B) bearish=2(A,C)",
          hb["09"]["bullish"] == 1 and hb["09"]["bearish"] == 2)
    check("hourly: 10h neutral from both-fallback", hb["10"]["neutral"] == 1)


def test_sig_votes():
    rows = [_mk("2026-07-08T10:00:00", "bullish", votes=62),
            _mk("2026-07-08T10:01:00", "bullish", votes=10),
            _mk("2026-07-08T10:02:00", "bearish", votes=24)]
    v = SG.votes_concentration(rows)
    check("votes: bull total", v["bull_votes"] == 72)
    check("votes: ratio", v["bull_bear_ratio"] == 3.0)
    check("votes: max bullish", v["max_bullish_votes"] == 62)
    check("votes: bear share", abs(v["bear_votes_share"] - 0.25) < 1e-9)
    check("votes: empty safe", SG.votes_concentration([])["max_bullish_votes"] == 0)


def test_sig_named():
    rows = [{"author": "a"}] * 20 + [{"author": "b"}] * 10 + [{"author": None}] * 5
    nc = SG.named_concentration(rows, min_n=30)
    check("named: n counts author-only", nc["n"] == 30)
    check("named: top5 share", nc["top5_share"] == 1.0)
    small = SG.named_concentration([{"author": "a"}] * 5, min_n=30)
    check("named: below min hidden", small["top5_share"] is None)


def test_sig_named_user_fallback():
    """② author欠落(userのみ)の生行も投稿者集計に含める(全体約2割を取りこぼさない)。"""
    # author 12件 + user限定 12件 + author=None&user 8件 = 32件全て名前あり
    rows = ([{"author": "a"}] * 12 + [{"user": "b"}] * 12
            + [{"author": None, "user": "c"}] * 8)
    nc = SG.named_concentration(rows, min_n=30)
    check("named: user-only rows included in n", nc["n"] == 32)
    check("named: user rows counted in top5 share", nc["top5_share"] == 1.0)
    authors = {a["author"] for a in nc["top_authors"]}
    check("named: user name 'b' present as poster", "b" in authors)
    # author と user 両方ある行は author を優先(ハッシュID名前空間の一貫性)
    nc2 = SG.named_concentration([{"author": "x", "user": "y"}] * 30, min_n=30)
    check("named: author preferred when both present",
          nc2["n"] == 30 and nc2["top_authors"][0]["author"] == "x")
    # 名前が全く無い行(author/user共にNone)は従来通り数えない
    nc3 = SG.named_concentration([{"author": None, "user": None}] * 40, min_n=30)
    check("named: no-name rows excluded", nc3["n"] == 0 and nc3["top5_share"] is None)


def test_sig_hourly():
    rows = [_mk("2026-07-08T09:10:00", "bullish"), _mk("2026-07-08T09:40:00", "bearish"),
            _mk("2026-07-08T10:05:00", "neutral"),
            _mk("2026-07-07T09:00:00", "bullish")]  # 別日=除外
    hb = SG.hourly_buckets(rows, day="2026-07-08")
    check("hourly: 2 buckets", sorted(hb.keys()) == ["09", "10"])
    check("hourly: 09 counts", hb["09"]["total"] == 2 and hb["09"]["bearish"] == 1)
    check("hourly: pph", SG.posts_per_hour(rows, day="2026-07-08") == 1.5)


def test_sig_lowzone():
    bars = [{"low": 100, "close": 105}] * 19 + [{"low": 90, "close": 95}]
    check("lowzone: at low True", SG.low_zone(bars, 91.0) is True)
    check("lowzone: far False", SG.low_zone(bars, 120.0) is False)
    check("lowzone: no data None", SG.low_zone([], 100.0) is None)
    check("lowzone: no price None", SG.low_zone(bars, None) is None)


def test_sig_gauges():
    ratios = {"n": 20, "bull_ratio": 0.1, "bear_ratio": 0.85, "neutral_ratio": 0.05}
    lex = SG.lexicon_counts(["損切りして退場します"] * 8 + ["普通のコメント"] * 2)
    votes = {"bull_bear_ratio": 0.2, "max_bullish_votes": 5, "bear_votes_share": 0.9,
             "bull_votes": 2, "bear_votes": 40}
    g = SG.oniya_gauges(ratios=ratios, lex_today=lex, votes=votes, posts_z=None,
                        cap_z=None, eup_z=None, in_low_zone=True, calib_days=2)
    check("gauge: capitulation high on panic", g["capitulation"] > 70)
    check("gauge: state = セリクラ", g["state"].startswith("セリクラ(逆張り"))
    check("gauge: calibrating flagged", g["calibrating"] is True and "較正中" in g["state"])
    # 過熱ケース
    ratios2 = {"n": 20, "bull_ratio": 0.9, "bear_ratio": 0.05, "neutral_ratio": 0.05}
    lex2 = SG.lexicon_counts(["爆益🚀最強"] * 8 + ["普通"] * 2)
    votes2 = {"bull_bear_ratio": 8.0, "max_bullish_votes": 150, "bear_votes_share": 0.1,
              "bull_votes": 200, "bear_votes": 20}
    g2 = SG.oniya_gauges(ratios=ratios2, lex_today=lex2, votes=votes2, posts_z=3.0,
                         cap_z=None, eup_z=None, in_low_zone=False, calib_days=30)
    check("gauge: overheat high on mania", g2["overheat"] > 70)
    check("gauge: state = 過熱警戒", g2["state"] == "過熱警戒")
    # 中立ケース
    g3 = SG.oniya_gauges(ratios={"n": 5, "bull_ratio": 0.4, "bear_ratio": 0.4,
                                  "neutral_ratio": 0.2},
                         lex_today=SG.lexicon_counts(["普通"] * 5),
                         votes={"bull_bear_ratio": 1.0, "max_bullish_votes": 3,
                                "bear_votes_share": 0.5, "bull_votes": 3, "bear_votes": 3},
                         posts_z=0.0, cap_z=0.0, eup_z=0.0,
                         in_low_zone=False, calib_days=30)
    check("gauge: neutral state", g3["state"] == "中立")


def test_sig_cap_shock_and_price_freeze_guard():
    """
    ★2026-08-19 おにやキャリブレーション(15:43投稿)実装分のテスト。
    (1) oniya_gauges: c_shockが安値圏(in_low_zone)無しでも当日急落率だけで
        阿鼻叫喚スコアを高め、状態が「セリクラ」になる(旧c_low ANDは撤廃)。
    (2) build_signal_cards: 阿鼻叫喚カードの発火判定にin_low_zoneが不要になった。
    (3) compute_signals: price_intradayの最終バーが当日(day)と一致しない場合、
        change_pct(chg)がNoneになり凍結表示バグを防ぐ(price/prevは残す)。
    """
    # bear_ratio/bear_votes_shareをNoneにしてcap_shock以外の成分を平均から除外し、
    # 「c_shockだけでcapitulationを押し上げられるか」を単離して検証する
    # (他成分が0近傍で平均に混ざると希釈されテストが不安定になるため)。
    ratios = {"n": 10, "bull_ratio": 0.4, "bear_ratio": None, "neutral_ratio": 0.1}
    lex = SG.lexicon_counts(["普通のコメント"] * 10)
    votes = {"bull_bear_ratio": 1.0, "max_bullish_votes": 3, "bear_votes_share": None,
             "bull_votes": 5, "bear_votes": 5}
    # 安値圏でなくても大幅下落(-15%)ならcapitulationがFIRE(30)超に達すること
    g_shock = SG.oniya_gauges(ratios=ratios, lex_today=lex, votes=votes, posts_z=None,
                              cap_z=None, eup_z=None, in_low_zone=False, calib_days=30,
                              price_change_pct=-15.0)
    check("cap_shock: capitulation high on -15% shock even off-low-zone",
          g_shock["capitulation"] > config.SIG_CAPITULATION_FIRE)
    check("cap_shock: state セリクラ without in_low_zone",
          g_shock["state"].startswith("セリクラ(逆張り"))
    check("cap_shock: components.cap.shock present", g_shock["components"]["cap"]["shock"] == 1.0)
    # 軽微な下落(-1%、SIG_CAP_SHOCK_MIN_PCT=3%未満)はshock寄与0
    g_mild = SG.oniya_gauges(ratios=ratios, lex_today=lex, votes=votes, posts_z=None,
                             cap_z=None, eup_z=None, in_low_zone=False, calib_days=30,
                             price_change_pct=-1.0)
    check("cap_shock: mild -1% -> shock component 0",
          g_mild["components"]["cap"]["shock"] == 0.0)
    # price_change_pct未指定(None)はshock None・cap_partsから除外されるだけでクラッシュしない
    g_none = SG.oniya_gauges(ratios=ratios, lex_today=lex, votes=votes, posts_z=None,
                             cap_z=None, eup_z=None, in_low_zone=False, calib_days=30)
    check("cap_shock: price_change_pct omitted -> shock None (no crash)",
          g_none["components"]["cap"]["shock"] is None)

    # build_signal_cards: 阿鼻叫喚カードがin_low_zone=False・capitulation>FIREでも発火
    cards = SG.build_signal_cards(
        ratios=ratios, lex=lex, votes=votes,
        named={"top5_share": 0.02}, other={"ratio": 0.01}, posts_z=0.0,
        gauges=g_shock, pph=1.0, in_low_zone=False)
    cap_card = next(c for c in cards if c["name"] == "阿鼻叫喚(セリクラ)")
    check("cards: capitulation card fires without in_low_zone", cap_card["state"] == "発火")

    # compute_signals: 価格データが当日と一致しない(非取引日の古いバー)場合chgはNone
    import time as _time
    def _epoch(date_str):
        import datetime as _dt
        return int(_dt.datetime.strptime(date_str + "T09:00:00", "%Y-%m-%dT%H:%M:%S").timestamp())
    stale_intraday = {
        "meta": {"regularMarketPrice": 50000.0, "previousClose": 49500.0},
        "bars": [{"ts": _epoch("2026-07-17"), "close": 50000.0}],
    }
    rows = [_mk("2026-07-20T09:10:00", "bearish", "普通", votes=1, author="a")]
    s_stale = SG.compute_signals(rows, price_daily=None, price_intraday=stale_intraday,
                                 day="2026-07-20")  # 非取引日=07-17のバーのまま
    check("freeze-guard: stale price date -> change_pct None",
          s_stale["price"]["change_pct"] is None)
    check("freeze-guard: price/prev still populated (not nulled)",
          s_stale["price"]["last"] == 50000.0 and s_stale["price"]["prev_close"] == 49500.0)
    # バーの日付がasof_dateと一致する場合はchgが通常通り出る
    fresh_intraday = {
        "meta": {"regularMarketPrice": 50000.0, "previousClose": 49500.0},
        "bars": [{"ts": _epoch("2026-07-20"), "close": 50000.0}],
    }
    s_fresh = SG.compute_signals(rows, price_daily=None, price_intraday=fresh_intraday,
                                 day="2026-07-20")
    check("freeze-guard: fresh price date -> change_pct populated",
          s_fresh["price"]["change_pct"] is not None)


def test_sig_compute_and_cards():
    rows = [
        _mk("2026-07-08T09:10:00", "bearish", "損切りして退場します", votes=30, author="a"),
        _mk("2026-07-08T09:20:00", "bearish", "暴落だ逃げろ", votes=10, author="a"),
        _mk("2026-07-08T10:00:00", "bullish", "爆益🚀", votes=62, author="b"),
    ]
    S_ = SG.compute_signals(rows, price_daily=None, price_intraday=None, day="2026-07-08")
    check("compute: ratios present", S_["ratios"]["n"] == 3)
    check("compute: gauges 0-100",
          0 <= S_["gauges"]["overheat"] <= 100 and 0 <= S_["gauges"]["capitulation"] <= 100)
    check("compute: 9 cards", len(S_["cards"]) == 9)
    check("compute: card states valid",
          all(c["state"] in ("OK", "警戒", "発火") for c in S_["cards"]))
    check("compute: price None-safe", S_["price"]["last"] is None)
    check("compute: empty rows safe",
          SG.compute_signals([], day="2026-07-08")["cards"] is not None)
    # 疎な日(カバレッジ<10)は較正日数/zの基準に数えない
    sparse = [_mk(f"2026-06-{d:02d}T10:00:00", "neutral", "a") for d in range(1, 25)]
    S2 = SG.compute_signals(sparse + rows, day="2026-07-08")
    check("compute: sparse days excluded from calib",
          S2["calib_days"] == 0 and S2["gauges"]["calibrating"] is True)
    check("compute: sparse days -> posts_z None", S2["posts_z"] is None)


def test_sig_raw_vs_analyzed_split():
    """量/語彙は raw 全件、強弱比は analyzed のサンプル、を検証。"""
    # analyzed = 3件のAI判定サンプル(bull2/bear1)
    analyzed = [_mk("2026-07-08T09:00:00", "bullish", "決算に期待"),
                _mk("2026-07-08T09:01:00", "bullish", "買い増した"),
                _mk("2026-07-08T09:02:00", "bearish", "少し不安")]
    # raw = 100件(うち語彙該当多数)。garbled/欠損textも混ぜる。
    raw = []
    for i in range(60):
        raw.append({"ts": f"2026-07-08T09:{i % 60:02d}:00", "text": "損切りして退場します", "source": "yahoo"})
    for i in range(40):
        raw.append({"ts": f"2026-07-08T10:{i % 60:02d}:00", "text": "爆益🚀最強", "source": "5ch"})
    raw.append({"ts": "2026-07-08T11:00:00", "text": "壊れ���", "source": "5ch"})  # 化け=除外
    S3 = SG.compute_signals(analyzed, raw_rows=raw, day="2026-07-08")
    check("split: true_volume from raw (化け除外)", S3["true_volume"] == 100)
    check("split: analyzed_today from analyzed", S3["analyzed_today"] == 3)
    check("split: ratios from analyzed (n=3)", S3["ratios"]["n"] == 3)
    check("split: bull ratio ~0.667",
          abs(S3["ratios"]["bull_ratio"] - round(2/3, 3)) < 1e-9)
    # 語彙は raw 全件から: capitulation 60件/euphoria 40件 (計100)
    check("split: capitulation lex from raw", S3["lexicon"]["capitulation"]["hits"] == 60)
    check("split: euphoria lex from raw", S3["lexicon"]["euphoria"]["hits"] == 40)
    check("split: posts_per_hour from raw > analyzed",
          S3["posts_per_hour"] >= 40)  # raw多数 vs analyzed3件
    # raw未指定なら後方互換で analyzed を量源に流用
    S4 = SG.compute_signals(analyzed, day="2026-07-08")
    check("split: backward-compat raw=None uses analyzed", S4["true_volume"] == 3)


# ============================================================================
# 研究層(signal_engine / export / forward_oos / backtest / eventstudy)
# ============================================================================
import signal_engine as ENG
import export_signal as EXP
import forward_oos as FOOS
import backtest as BT
from research import eventstudy as ES
from research import run_study as RS
import public_export as PE
import public_insight as PINS
import public_sheets_sync as PSS
import run_once as RO
import public_dashboard as PD   # ★2026-08-20追加: 純粋な補助関数(_today_time_buckets等)
                                 # をテスト対象にするため。st.*呼び出しはmain()内のみ
                                 # なのでbareモードimport自体は副作用なし(streamlitの
                                 # "No runtime found"警告が出るが無害・無視してよい)。

# ============================================================================
# 高度化4柱 + moomoo(各自ファイル内の純関数テストを束ねる)
# ============================================================================
import insight as INSIGHT
import alerts as ALERTS
import cluster_trend as CTREND
import moomoo_source as MOOMOO
import collect_intl as CINTL
import backtest as BACKTEST
import votes_snapshot as VOTES
from research import evaluate as EVAL
from research import vol_eval as VOLEVAL
from research import intraday_linkage as ILINK
from research import retail_chase as RCHASE
from research import afterhours_bearish as ABEAR
from research import votes_pit_eval as VPIT
from research import sector_peer_lead as PEERLEAD
from research import attention_relative as ATTNREL
from research import premarket_lead as PMLEAD
from research import margin_balance_285A as MARGIN
# ダッシュボード統合モジュール(板/熱狂/米ピア/収集鮮度)
import board_read as BOARDRO
import euphoria as EUPH
import peer_lead_read as PEERLEADRO
import feed_health as FEEDHEALTH
import reversal_signals as REVSIG


def _run_pillar_selftests():
    """柱1/3/4 + 研究層評価 + moomoo + collect_intl(Reddit OAuth) の自己テストを実行し、総失敗数を返す。"""
    total = 0
    for name, fn in [("insight(柱1)", INSIGHT._run_selftests),
                     ("alerts(柱3)", ALERTS._run_selftests),
                     ("cluster_trend(柱4)", CTREND._tests),
                     ("evaluate(柱2)", EVAL._run_selftests),
                     ("vol_eval(V0評価ハーネス)", VOLEVAL._run_selftests),
                     ("intraday_linkage(掲示板×株価連動)", ILINK._run_selftests),
                     ("retail_chase(小口chase/capit・rc1)", RCHASE._run_selftests),
                     ("afterhours_bearish(場後弱気→翌日リターン・ab1)", ABEAR._run_selftests),
                     ("backtest(逆張り売買ルール)", BACKTEST._run_selftests),
                     ("moomoo_source", MOOMOO._run_selftests),
                     ("collect_intl(Reddit OAuth)", CINTL._run_selftests),
                     ("votes_snapshot(点時刻票)", VOTES._run_selftests),
                     ("votes_pit_eval(vote_ratio PIT前向き)", VPIT._run_selftests),
                     ("sector_peer_lead(半導体ピア→285A)", PEERLEAD._run_selftests),
                     ("attention_relative(掲示板注目の相対)", ATTNREL._run_selftests),
                     ("premarket_lead(海外ピア×掲示板→285A寄り予測)", PMLEAD._run_selftests),
                     ("margin_balance(信用残→セリクラ判定)", MARGIN._run_selftests),
                     ("board_read(L2板要約)", BOARDRO._selftest),
                     # euphoria._selftest は bool(True=PASS)ゆえ int 失敗数へ変換
                     ("euphoria(熱狂/天井指数)", lambda: 0 if EUPH._selftest() else 1),
                     ("peer_lead_read(米ピア→寄り予測)", PEERLEADRO._run_selftests),
                     ("feed_health(収集鮮度)", FEEDHEALTH._selftest),
                     ("public_insight(公開用AI考察)", PINS._run_selftests),
                     ("reversal_signals(折り返し極値自動検出)", REVSIG._run_selftests)]:
        print(f"\n----- {name} -----")
        try:
            total += int(fn() or 0)
        except Exception as e:
            print(f"[FAIL] {name} raised {e!r}")
            total += 1
    return total


def _dense_raw(days, posts_per_day=12, hour_buckets=(9, 10, 11, 12)):
    """密セッション用の合成raw: 各日 posts_per_day件を複数取引時間バケットに散らす。"""
    rows = []
    for d in days:
        for i in range(posts_per_day):
            hh = hour_buckets[i % len(hour_buckets)]
            rows.append({"ts": f"{d}T{hh:02d}:{(i*3) % 60:02d}:00",
                         "text": "普通のコメント", "source": "yahoo"})
    return rows


def test_eng_dense_and_calib():
    days = [f"2026-06-{d:02d}" for d in range(1, 8)]  # 7 dense days
    raw = _dense_raw(days)
    dense = ENG.dense_session_dates(raw, "2026-07-08")
    check("eng: dense dates count", len(dense) == 7)
    # 1時間だけに300件=非dense(z=69アーティファクト回帰テスト)
    spike1h = [{"ts": f"2026-07-07T10:{i % 60:02d}:00", "text": "x", "source": "yahoo"}
               for i in range(300)]
    d2 = ENG.dense_session_dates(spike1h, "2026-07-08")
    check("eng: 300posts-in-1hour NOT dense", d2 == [])
    st, ramp = ENG.calibration_status(6)
    check("eng: calibrating<20", st == "calibrating" and 0 < ramp < 1)
    st2, ramp2 = ENG.calibration_status(25)
    check("eng: calibrated>=20", st2 == "calibrated" and ramp2 == 1.0)


def test_eng_robust_z_pctrank():
    # <10 usable points -> None (suppress cross-day z)
    check("eng: robust_z <floor None", ENG.robust_z([1, 2, 3, 4, 5]) is None)
    series = [10.0] * 12 + [10.0]           # MAD==0
    check("eng: robust_z MAD0 None", ENG.robust_z(series) is None)
    series2 = [float(x) for x in range(12)] + [50.0]  # last is outlier, 12 hist pts
    z = ENG.robust_z(series2)
    check("eng: robust_z winsor-bounded", z is not None and abs(z) <= config.BVP_WINSOR_Z + 1e-9)
    # excludes current session: last value doesn't enter its own baseline
    pr = ENG.pct_rank([float(x) for x in range(12)] + [100.0])
    check("eng: pct_rank high for outlier", pr is not None and pr >= 0.9)
    check("eng: pct_rank <floor None", ENG.pct_rank([1, 2, 3]) is None)


def test_eng_bvp_and_direction():
    # calibrating: >half None -> bvp None
    b0 = ENG.compute_bvp({"post_volume_surprise": None, "velocity_surge": None,
                          "lexicon_intensity": 0.5, "sentiment_disagreement": 0.5})
    check("eng: bvp calibrating None", b0["bvp"] is None and b0["calibrating"])
    # all present, named concentration down-weights volume
    b1 = ENG.compute_bvp({"post_volume_surprise": 1.0, "velocity_surge": 1.0,
                          "lexicon_intensity": 0.0, "sentiment_disagreement": 0.0,
                          "named_top5_share": 0.8})
    check("eng: bvp manipulation gate", "named_concentration" in b1["gated_by"])
    check("eng: bvp in [0,1]", b1["bvp"] is None or 0 <= b1["bvp"] <= 1)
    # direction_candidate never routable
    dc = ENG.direction_candidate({"overheat": 90, "capitulation": 10},
                                 {"in_low_zone": False, "named_top5_share": 0.1})
    check("eng: dir status 未検証", dc["status"] == "未検証")
    # fade_down requires low top5 (broad panic not orchestrated)
    dc2 = ENG.direction_candidate({"overheat": 10, "capitulation": 85},
                                  {"in_low_zone": True, "named_top5_share": 0.8})
    check("eng: fade_down blocked when orchestrated", dc2["side"] != "fade_down")
    dc3 = ENG.direction_candidate({"overheat": 10, "capitulation": 85},
                                  {"in_low_zone": True, "named_top5_share": 0.05})
    check("eng: fade_down when broad panic", dc3["side"] == "fade_down")


def test_eng_confidence_and_regime():
    c = ENG.meta_confidence(6, 5000, 0.9, calibrating=True)
    check("eng: conf<=cap while calibrating", c <= config.BVP_CONF_CALIB_CAP + 1e-9)
    check("eng: regime calibrating on short hist", ENG.classify_regime(0.9, [0.1] * 3) == "calibrating")
    hist = [x / 100 for x in range(20)]
    check("eng: regime extreme on p95", ENG.classify_regime(0.99, hist) == "extreme")
    check("eng: regime calm on low", ENG.classify_regime(0.01, hist) == "calm")


def test_eng_export_record_schema():
    days = [f"2026-06-{d:02d}" for d in range(1, 8)]
    raw = _dense_raw(days) + _dense_raw(["2026-07-08"], posts_per_day=15)
    an = [_mk("2026-07-08T10:00:00", "bearish", "損切り", meaningful=True)]
    sig = SG.compute_signals(an, raw_rows=raw, day="2026-07-08")
    rec = ENG.build_export_record(sig, run_ts="2026-07-08T15:31:00",
                                  cutoff="close_consolidated", raw_rows=raw)
    errs = EXP.validate_record(rec)
    check("export: schema valid", errs == [])
    check("export: calibrating", rec["calibration_status"] == "calibrating")
    check("export: confidence<=cap", rec["confidence"] <= config.BVP_CONF_CALIB_CAP + 1e-9)
    check("export: dir status const", rec["direction_candidate"]["status"] == "未検証")
    check("export: disclaimer const", rec["disclaimer"] == ENG.DISCLAIMER)
    check("export: no posts_z artifact (dense-honest/None)",
          rec["features"]["posts_z"] is None)
    check("export: spec_hash stamped", rec["signal_spec_hash"].startswith("sh1_"))


def test_eng_state_dense_honest():
    """① state の較正カウントを dense session 基準へ統一(非dense n=15を出さない)。"""
    check("state: calib count -> dense",
          ENG.state_dense_honest("中立(較正中 n=15日)", 6, True) == "中立(較正中 n=6日)")
    # ベース状態(括弧を含む)は保持し、較正カウントだけ差し替える
    check("state: base state with parens preserved",
          ENG.state_dense_honest("セリクラ(逆張り買い候補ゾーン)(較正中 n=15日)", 6, True)
          == "セリクラ(逆張り買い候補ゾーン)(較正中 n=6日)")
    # dense基準で較正済みならサフィックスを付けない
    check("state: calibrated -> no suffix",
          ENG.state_dense_honest("過熱警戒", 25, False) == "過熱警戒")
    # 非dense側にサフィックス無しでも dense基準が較正中なら付け直す(整合)
    check("state: reconcile add suffix when dense-calibrating",
          ENG.state_dense_honest("過熱警戒", 6, True) == "過熱警戒(較正中 n=6日)")


def test_eng_export_state_and_range_dense_honest():
    """① export/凍結行の state 計数=dense、③ range_day_score も dense-honest(未了None)。"""
    import re as _re
    days = [f"2026-06-{d:02d}" for d in range(1, 8)]  # 7 dense days (<20 => calibrating)
    raw = _dense_raw(days) + _dense_raw(["2026-07-08"], posts_per_day=15)
    an = [_mk("2026-07-08T10:00:00", "bearish", "損切り", meaningful=True)]
    sig = SG.compute_signals(an, raw_rows=raw, day="2026-07-08")
    # 前提: signals側 calib_days(非dense)と dense_count は本ケースで異なる(=二重基準が観測可能)
    rec = ENG.build_export_record(sig, run_ts="2026-07-08T15:31:00",
                                  cutoff="close_consolidated", raw_rows=raw)
    m = _re.search(r"n=(\d+)日", rec["features"]["state"])
    check("state: export n == dense calib_days",
          m is not None and int(m.group(1)) == rec["calib_days"])
    check("state: signals非dense計数と分離(gauge n != export n)",
          f"n={sig['calib_days']}日" not in rec["features"]["state"]
          or sig["calib_days"] == rec["calib_days"])
    # ③ range_day_score は較正中 None(vol_regime_score と整合)
    check("range_day: None while calibrating (dense-honest)",
          rec["range_day_score"] is None)
    check("range_day: consistent with vol_regime_score",
          (rec["range_day_score"] is None) == (rec["vol_regime_score"] is None))
    # 純関数: dense-honest posts_z=None -> None
    check("range_day: None posts_z -> None",
          ENG.range_day_score({"posts_z": None}, {"lexicon_intensity": 0.2}) is None)


def test_eng_data_health_wiring():
    """④ 収集メタ由来の data_health(stale/page_cap_hit)が export に反映される。"""
    days = [f"2026-06-{d:02d}" for d in range(1, 8)]
    raw = _dense_raw(days) + _dense_raw(["2026-07-08"], posts_per_day=15)
    an = [_mk("2026-07-08T10:00:00", "bearish", "損切り")]
    sig = SG.compute_signals(an, raw_rows=raw, day="2026-07-08")
    rec = ENG.build_export_record(sig, run_ts="2026-07-08T15:31:00",
                                  cutoff="close_consolidated", raw_rows=raw,
                                  data_health={"stale": True, "page_cap_hit": True})
    check("data_health: page_cap_hit propagated", rec["data_health"]["page_cap_hit"] is True)
    check("data_health: stale propagated", rec["data_health"]["stale"] is True)
    # 未指定なら後方互換で False 既定
    rec2 = ENG.build_export_record(sig, run_ts="2026-07-08T15:31:00",
                                   cutoff="close_consolidated", raw_rows=raw)
    check("data_health: default False when unset",
          rec2["data_health"]["page_cap_hit"] is False
          and rec2["data_health"]["stale"] is False)


def test_export_atomic_and_append(tmpdir=None):
    import tempfile, os as _os, json as _json
    d = tempfile.mkdtemp()
    p = _os.path.join(d, "latest.json")
    EXP._atomic_write_json(p, {"a": 1})
    EXP._atomic_write_json(p, {"a": 2})   # replace, not append
    check("export: atomic replace", _json.load(open(p, encoding="utf-8"))["a"] == 2)
    hp = _os.path.join(d, "history.jsonl")
    EXP._append_jsonl(hp, {"x": 1})
    EXP._append_jsonl(hp, {"x": 2})
    lines = open(hp, encoding="utf-8").read().strip().split("\n")
    check("export: history append-only (2 lines)", len(lines) == 2)


def _pe_sample_S(true_volume=250, pph=20.5, bull=0.55, bear=0.25, neutral=0.20,
                 overheat=62.3, capitulation=18.0, price_last=4250.0, chg=-0.8):
    return {
        "true_volume": true_volume,
        "posts_per_hour": pph,
        "ratios": {"bull_ratio": bull, "bear_ratio": bear, "neutral_ratio": neutral},
        "gauges": {"overheat": overheat, "capitulation": capitulation},
        "price": {"last": price_last, "change_pct": chg},
        # ★2026-08-19追加: 9シグナルカード(公開ダッシュボード用・集計値のみ)。
        "cards": [{"name": "灼熱メーター(過熱)", "value": 62.3, "threshold": ">70",
                   "state": "警戒", "note": "過熱スコア62.3 (閾値70)"}],
        # 個別投稿寄りのフィールド(named/votes等)も混ぜておき、build_public_record が
        # これらを一切参照しない(=拾わない)ことを併せて確認する。
        "named": {"n": 40, "top5_share": 0.4,
                  "top_authors": [{"author": "abcdef123456", "count": 9}]},
        "votes": {"bull_votes": 10, "bear_votes": 2, "max_bullish_votes": 30},
    }


def test_public_export_build_record():
    S = _pe_sample_S()
    trend = [{"date": "2026-08-09", "post_count": 200, "bear_ratio": 0.2, "bull_ratio": 0.5},
             {"date": "2026-08-10", "post_count": 250, "bear_ratio": 0.25, "bull_ratio": 0.55}]
    rec = PE.build_public_record(S, None, trend, generated_at="2026-08-10T15:00:00")
    check("pe: schema_version", rec["schema_version"] == "1.0")
    check("pe: symbol", rec["symbol"] == config.SYMBOL)
    check("pe: company_name", rec["company_name"] == PE.COMPANY_NAME)
    check("pe: price.last", rec["price"]["last"] == 4250.0)
    check("pe: price.change_pct", rec["price"]["change_pct"] == -0.8)
    check("pe: board.post_count_today", rec["board"]["post_count_today"] == 250)
    check("pe: board.bull_ratio", rec["board"]["bull_ratio"] == 0.55)
    check("pe: board.bear_ratio", rec["board"]["bear_ratio"] == 0.25)
    check("pe: board.overheat_score", rec["board"]["overheat_score"] == 62.3)
    check("pe: board.capitulation_score", rec["board"]["capitulation_score"] == 18.0)
    check("pe: trend_14d passthrough length", len(rec["trend_14d"]) == 2)
    check("pe: disclaimer set", rec["disclaimer"] == PE.DISCLAIMER)
    # named/votes(個別投稿寄り)は build_public_record の出力に一切現れないこと
    check("pe: named/top_authors not carried into record",
          "named" not in rec and "votes" not in rec)
    check("pe: built record itself has no leak", PE.validate_no_leak(rec) == [])
    # ★2026-08-19追加: signal_cards(9シグナル・公開ダッシュボード用)passthrough
    check("pe: signal_cards passthrough from S['cards']",
          rec["signal_cards"] == S["cards"])
    check("pe: signal_cards defaults to [] when S has no cards",
          PE.build_public_record({}, None, [])["signal_cards"] == [])
    # regime 未指定時はキー自体を含めない(既存動作を壊さない)。指定時は3フィールドのみ
    # ホワイトリスト方式で抜き出す(余計なキーが混ざっていても通さない)。
    check("pe: regime omitted when None", "regime" not in rec)
    rec_regime = PE.build_public_record(
        S, None, trend,
        regime={"vol_regime": "calm", "vol_regime_score": 0.42,
                "calibration_status": "calibrated", "direction_candidate": "up"})
    check("pe: regime included when given",
          rec_regime["regime"] == {"vol_regime": "calm", "vol_regime_score": 0.42,
                                    "calibration_status": "calibrated"})
    check("pe: regime whitelist drops unknown fields (direction_candidate)",
          "direction_candidate" not in rec_regime["regime"])
    check("pe: regime record has no leak", PE.validate_no_leak(rec_regime) == [])
    # ★2026-08-19追加: intraday_today 未指定時はキー自体を含めない・指定時は
    # {price, sentiment} がそのまま(欠けているサブキーは空リストで補完)入る。
    check("pe: intraday_today omitted when None", "intraday_today" not in rec)
    rec_intraday = PE.build_public_record(
        S, None, trend, intraday_today={"price": [{"time": "09:00", "price_close": 50000.0}]})
    check("pe: intraday_today included when given",
          rec_intraday["intraday_today"]["price"] == [{"time": "09:00", "price_close": 50000.0}])
    check("pe: intraday_today missing 'sentiment' subkey defaults to []",
          rec_intraday["intraday_today"]["sentiment"] == [])
    check("pe: intraday_today record has no leak", PE.validate_no_leak(rec_intraday) == [])
    # price_sentiment_series 未指定時は空リスト(既存呼び出し元の動作を壊さない)
    check("pe: price_sentiment_series defaults to []", rec["price_sentiment_series"] == [])
    # ai_commentary 未指定時はキー自体を含めない(既存動作を壊さない)
    check("pe: ai_commentary omitted when None", "ai_commentary" not in rec)
    # ★2026-08-25追加(ユーザー指摘「公開用ダッシュボード(streamlit版)は直ってないのでは」
    # =9指標の推移スパークライン用データをstreamlit版へも渡す新規パラメータ)。
    # board_history_14d/signal_state_changes等と同じ「未指定ならキー自体を含めない」設計。
    check("pe: signal_cards_history_14d omitted when None",
          "signal_cards_history_14d" not in rec)
    rec_signal_hist = PE.build_public_record(
        S, None, trend,
        signal_cards_history_14d=[{"date": "2026-08-24", "ネームド集中": 0.1}])
    check("pe: signal_cards_history_14d included when given",
          rec_signal_hist["signal_cards_history_14d"] == [{"date": "2026-08-24", "ネームド集中": 0.1}])
    check("pe: signal_cards_history_14d record has no leak",
          PE.validate_no_leak(rec_signal_hist) == [])

    # ★2026-08-27追加(ユーザー依頼「24時間以内のキオクシアに関係しそうなニュースの
    # 要約を公開ダッシュボードに」)。newsパラメータのpassthrough+ホワイトリストを検証。
    check("pe: news omitted when None", "news" not in rec)
    rec_news = PE.build_public_record(
        S, None, trend,
        news={"items": [{"title": "ニュース見出しA", "link": "https://x/a",
                        "source": "日本経済新聞", "published": "2026-08-27T01:00:00",
                        "leaked_field": "SECRET_SHOULD_NOT_APPEAR"}],
              "summary": {"text": "要約テキスト。投資助言ではありません。",
                         "generated_at": "2026-08-27T01:05:00"},
              "has_new": True})
    check("pe: news.items passthrough (whitelisted fields only, 'link' renamed to 'article_link')",
          rec_news["news"]["items"][0]["title"] == "ニュース見出しA"
          and rec_news["news"]["items"][0]["article_link"] == "https://x/a"
          and "leaked_field" not in rec_news["news"]["items"][0]
          and "link" not in rec_news["news"]["items"][0])
    check("pe: news.summary_text/summary_generated_at passthrough",
          rec_news["news"]["summary_text"] == "要約テキスト。投資助言ではありません。"
          and rec_news["news"]["summary_generated_at"] == "2026-08-27T01:05:00")
    check("pe: news.has_new NOT carried into public record (internal flag only)",
          "has_new" not in rec_news["news"] and "summary" not in rec_news["news"])
    check("pe: news record has no leak", PE.validate_no_leak(rec_news) == [])
    rec_news_no_summary = PE.build_public_record(
        S, None, trend, news={"items": [], "summary": None, "has_new": False})
    check("pe: news.summary_text None when no summary available",
          rec_news_no_summary["news"]["summary_text"] is None
          and rec_news_no_summary["news"]["items"] == [])

    # price.last が signals側で未確定(None)の時は price_d の meta へフォールバック
    S2 = _pe_sample_S(price_last=None)
    rec2 = PE.build_public_record(S2, {"meta": {"regularMarketPrice": 4111.0}}, [])
    check("pe: price fallback from price_d.meta", rec2["price"]["last"] == 4111.0)

    # price_sentiment_series を渡した場合はそのまま渡され、ai_commentary を渡した場合は
    # {text, generated_at} のdictとしてキーが追加される。
    pss = [{"date": "2026-08-10", "price_close": 4250.0, "bear_ratio": 0.25, "bull_ratio": 0.55}]
    rec3 = PE.build_public_record(S, None, trend, price_sentiment_series=pss,
                                  ai_commentary={"text": "集計値に基づく客観的な考察文。",
                                                "generated_at": "2026-08-10T15:05:00"})
    check("pe: price_sentiment_series passthrough", rec3["price_sentiment_series"] == pss)
    check("pe: ai_commentary included when given",
          rec3["ai_commentary"]["text"] == "集計値に基づく客観的な考察文。"
          and rec3["ai_commentary"]["generated_at"] == "2026-08-10T15:05:00")
    # ai_commentary.text は意図的な例外パスなので漏洩検出されない
    check("pe: ai_commentary.text does not trigger leak", PE.validate_no_leak(rec3) == [])


def test_public_export_load_regime_readonly():
    """★2026-08-19追加: _load_regime_readonly()(公開ダッシュボードのボラ・レジーム帯用・
    signal_export/latest.jsonを読み取り専用で覗く)のfail-soft動作を検証する。
    ファイル無し/壊れたJSONでも例外を投げずNoneを返し、正常時は3フィールドだけを
    抜き出すことを確認する(実ファイルI/Oのみ・実データ書込みなし)。"""
    import tempfile, os as _os, json as _json
    orig_path = config.SIGNAL_LATEST_PATH
    d = tempfile.mkdtemp()
    try:
        config.SIGNAL_LATEST_PATH = _os.path.join(d, "latest.json")
        check("pe-regime: missing file -> None (fail-soft)",
              PE._load_regime_readonly() is None)

        with open(config.SIGNAL_LATEST_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json..")
        check("pe-regime: broken JSON -> None (fail-soft)",
              PE._load_regime_readonly() is None)

        with open(config.SIGNAL_LATEST_PATH, "w", encoding="utf-8") as f:
            _json.dump({"vol_regime": "extreme", "vol_regime_score": 0.91,
                       "calibration_status": "calibrated",
                       "direction_candidate": "down", "n": 42}, f)
        got = PE._load_regime_readonly()
        check("pe-regime: normal read extracts exactly 3 fields",
              got == {"vol_regime": "extreme", "vol_regime_score": 0.91,
                      "calibration_status": "calibrated"})
    finally:
        config.SIGNAL_LATEST_PATH = orig_path


def test_public_export_trend_from_snapshots():
    snaps = [
        {"date": "2026-08-01", "day_cumulative": 10,
         "signals": {"true_volume": 10, "bear_ratio": 0.2, "bull_ratio": 0.5}},
        {"date": "2026-08-01", "day_cumulative": 30,   # 同日2件目=こちらが「その日最後」
         "signals": {"true_volume": 30, "bear_ratio": 0.28, "bull_ratio": 0.45}},
        {"date": "2026-08-02", "day_cumulative": 5,
         "signals": {"true_volume": 5, "bear_ratio": None, "bull_ratio": None}},
        {"date": "2026-08-03", "day_cumulative": 42, "signals": None},  # rollup失敗行
    ]
    trend = PE.trend_14d_from_snapshots(snaps)
    check("pe-trend: 3 distinct days", len(trend) == 3)
    d1 = trend[0]
    check("pe-trend: last-of-day used (30 not 10)", d1["date"] == "2026-08-01"
          and d1["post_count"] == 30)
    check("pe-trend: bear_ratio from last snapshot of day", d1["bear_ratio"] == 0.28)
    check("pe-trend: missing ratios -> None (no fabrication)", trend[1]["bear_ratio"] is None)
    check("pe-trend: signals=None -> fallback to day_cumulative", trend[2]["post_count"] == 42)
    check("pe-trend: days cap respected",
          len(PE.trend_14d_from_snapshots(snaps, days=1)) == 1)
    check("pe-trend: no leak keys in trend output", PE.validate_no_leak(trend) == [])


def test_public_export_extended_hours_summary():
    """★2026-08-19追加(ユーザー依頼「AI分析はPTS・米国ADRの時間帯もそれらの値を分析
    するように」)。extended_hours_summary()が(a)TSE正規セッション終値行を基準点として
    正しく検出する(b)基準点より後のPTS/ADR最新値だけを抜き出す(基準点より前や
    tse自体の行は候補にしない)(c)change_pctがTSE終値比で正しく計算される
    (d)PTS/ADRどちらか片方しか動きが無い時間帯でも欠けている方はNone(捏造しない)
    (e)フィード自体が空/Noneでもクラッシュせず None を返す、ことを検証する。"""
    adr_pts = {
        "rows": [
            {"ts": 1000, "tse": 50000.0, "pts": None, "adr_yen": None, "adr_usd": None},
            {"ts": 1010, "tse": 50200.0, "pts": None, "adr_yen": None, "adr_usd": None},
            # ↑ここまでが正規セッション。ts=1010(50200円)が基準点(=最終tse行)。
            {"ts": 1020, "tse": None, "pts": 50100.0, "adr_yen": None, "adr_usd": None},
            {"ts": 1030, "tse": None, "pts": 50300.0, "adr_yen": None, "adr_usd": None},
            {"ts": 1040, "tse": None, "pts": None, "adr_yen": 51000.0, "adr_usd": 32.5},
        ],
    }
    out = PE.extended_hours_summary(adr_pts)
    check("extended_hours: tse_close is the last row with tse present (50200, not 50000)",
          out["tse_close"]["price"] == 50200.0)
    check("extended_hours: pts takes the latest PTS observation after the boundary",
          out["pts"]["price"] == 50300.0)
    check("extended_hours: pts change_pct vs tse_close",
          abs(out["pts"]["change_pct"] - round((50300.0 - 50200.0) / 50200.0 * 100, 2)) < 1e-9)
    check("extended_hours: adr yen/usd both present", out["adr"]["price_yen"] == 51000.0
          and out["adr"]["price_usd"] == 32.5)
    check("extended_hours: adr change_pct vs tse_close",
          abs(out["adr"]["change_pct"] - round((51000.0 - 50200.0) / 50200.0 * 100, 2)) < 1e-9)

    # PTSのみ動いていてADRはまだ(=米国市場が開く前)の場合、adrはNone(捏造しない)
    adr_only = {
        "rows": [
            {"ts": 1000, "tse": 50000.0, "pts": None, "adr_yen": None, "adr_usd": None},
            {"ts": 1020, "tse": None, "pts": 50500.0, "adr_yen": None, "adr_usd": None},
        ],
    }
    out2 = PE.extended_hours_summary(adr_only)
    check("extended_hours: pts present, adr None when US market hasn't opened yet",
          out2["pts"] is not None and out2["adr"] is None)

    # フィード自体が空/None/tse行が1つも無い場合はクラッシュせずNoneを返す(fail-soft)
    check("extended_hours: empty feed -> None", PE.extended_hours_summary(None) is None)
    check("extended_hours: no rows -> None", PE.extended_hours_summary({"rows": []}) is None)
    check("extended_hours: no tse rows at all -> None (no boundary to anchor on)",
          PE.extended_hours_summary({"rows": [{"ts": 1, "tse": None, "pts": 100.0,
                                               "adr_yen": None, "adr_usd": None}]}) is None)


def test_public_export_price_sentiment_series():
    """★2026-08-19: 「過去14日間は、非営業日はなしにしましょう」「営業日のみとして」
    (ユーザー依頼)を受け、日付の基準を「snapshots日付(暦日=土日祝含む)」から
    「実際に価格barが存在する日(=営業日)」へ変更した。08-02は意図的に価格barを
    与えない(=休日を模す)ことで、非営業日が出力から除外されることを検証する。"""
    # センチメント側(snapshots)は 08-01/08-02/08-03 の3日分あるが、08-02は非営業日
    # (価格barが無い)ため出力からは除外されるはず。
    snaps = [
        {"date": "2026-08-01", "day_cumulative": 10,
         "signals": {"true_volume": 10, "bear_ratio": 0.20, "bull_ratio": 0.50}},
        {"date": "2026-08-02", "day_cumulative": 5,
         "signals": {"true_volume": 5, "bear_ratio": 0.10, "bull_ratio": 0.60}},
        {"date": "2026-08-03", "day_cumulative": 8,
         "signals": {"true_volume": 8, "bear_ratio": 0.15, "bull_ratio": 0.55}},
    ]
    # 価格側(price_daily)は 08-01(月) と 08-03(水) のみ(08-02は休日を模して意図的に欠測)。
    # ts は price_fetch.parse_chart_json と同じ UNIX epoch(秒)。gmtoffset=32400(JST)で
    # 09:00 JST 相当を使う(research.eventstudy._epoch_day と同じ変換式=既存の日足取得
    # パターンを再利用・新規CSV等を再パースしない)。
    price_daily = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day("2026-08-01"), "open": 3950.0, "high": 4020.0,
             "low": 3900.0, "close": 4000.0, "volume": 1234567},
            {"ts": ES._epoch_day("2026-08-03"), "open": 4050.0, "high": 4250.0,
             "low": 4020.0, "close": 4200.0, "volume": 2345678},
        ],
    }
    pss = PE.price_sentiment_series_from_snapshots(snaps, price_daily)
    check("pss: only trading days included (non-trading 08-02 excluded)", len(pss) == 2)
    dates_out = {r["date"] for r in pss}
    check("pss: 08-02 (no price bar = non-trading day) is excluded entirely",
          "2026-08-02" not in dates_out)
    by_date = {r["date"]: r for r in pss}
    check("pss: 08-01 price/OHLC matches exact bar",
          by_date["2026-08-01"]["price_close"] == 4000.0
          and by_date["2026-08-01"]["price_open"] == 3950.0
          and by_date["2026-08-01"]["price_high"] == 4020.0
          and by_date["2026-08-01"]["price_low"] == 3900.0)
    check("pss: 08-01 sentiment matches snapshot", by_date["2026-08-01"]["bull_ratio"] == 0.50
          and by_date["2026-08-01"]["bear_ratio"] == 0.20)
    check("pss: 08-03 price matches exact bar (new value, not stale carry)",
          by_date["2026-08-03"]["price_close"] == 4200.0)
    check("pss: 08-03 sentiment matches its own snapshot",
          by_date["2026-08-03"]["bull_ratio"] == 0.55 and by_date["2026-08-03"]["bear_ratio"] == 0.15)
    # ★2026-08-20追加(ユーザー依頼「センチメント推移のグラフに、投稿量の棒グラフを
    # 足せますか」): post_count(その営業日の投稿数)がsnapshotのsignals.true_volumeから
    # そのまま伝播すること。
    check("pss: post_count propagated from signals.true_volume",
          by_date["2026-08-01"]["post_count"] == 10 and by_date["2026-08-03"]["post_count"] == 8)
    # ★2026-08-19追加(ユーザー依頼「価格推移に出来高を足せますか」): price_volumeが
    # 日足バーのvolumeをそのまま(合算不要)伝播すること。
    check("pss: price_volume propagated from daily bar",
          by_date["2026-08-01"]["price_volume"] == 1234567
          and by_date["2026-08-03"]["price_volume"] == 2345678)

    # 価格データが全く無い(price_daily=None)時は営業日が0件と判定され、空リストを返す
    # (捏造しない・クラッシュしない)。
    pss_no_price = PE.price_sentiment_series_from_snapshots(snaps, None)
    check("pss: price_daily=None -> no trading days known -> empty list (no crash, no fabrication)",
          pss_no_price == [])

    # センチメント記録の無い営業日(board観測がまだ無い日)-> bull/bear_ratioはNone
    # (捏造しない)が、価格側は正しく出る(価格は取引所発の別ソースなので独立に成立する)。
    price_daily_no_sentiment = {"meta": {"gmtoffset": 32400},
                                "bars": [{"ts": ES._epoch_day("2026-08-05"), "close": 4300.0}]}
    pss_no_sent = PE.price_sentiment_series_from_snapshots(snaps, price_daily_no_sentiment)
    check("pss: trading day with no matching snapshot -> price present, ratios None",
          len(pss_no_sent) == 1 and pss_no_sent[0]["date"] == "2026-08-05"
          and pss_no_sent[0]["price_close"] == 4300.0
          and pss_no_sent[0]["bull_ratio"] is None and pss_no_sent[0]["bear_ratio"] is None)
    check("pss: trading day with no matching snapshot -> post_count also None (not fabricated)",
          pss_no_sent[0]["post_count"] is None)

    # days cap は trading-day基準でも同様に効く
    check("pss: days cap respected (last N trading days)",
          len(PE.price_sentiment_series_from_snapshots(snaps, price_daily, days=1)) == 1)

    # 個別投稿由来のキーが一切無いこと(価格系列にも漏洩検査を通す)
    check("pss: no leak keys in price_sentiment_series output", PE.validate_no_leak(pss) == [])

    # ★2026-08-20追加(ユーザー指摘「過去14日間の推移は自己データで作成している
    # のでは」への対応): Yahoo日足(price_daily)に08-04が無い(=日付切替直後の
    # 一時的なnull終値等を想定)場合でも、price_intraday(自前で収集済みの5分足)
    # から自動で日足OHLCVを補完できることを検証する。
    price_daily_missing_0804 = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day("2026-08-03"), "open": 4050.0, "high": 4250.0,
             "low": 4020.0, "close": 4200.0, "volume": 2345678},
            # 08-04は意図的に欠測(Yahoo日足の一時的null終値を模す)
        ],
    }
    price_intraday_has_0804 = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day("2026-08-04"), "open": 4100.0, "high": 4180.0,
             "low": 4080.0, "close": 4120.0, "volume": 500000},
            {"ts": ES._epoch_day("2026-08-04") + 300, "open": 4120.0, "high": 4300.0,
             "low": 4110.0, "close": 4280.0, "volume": 600000},
        ],
    }
    snaps_0804 = snaps + [{"date": "2026-08-04", "day_cumulative": 3,
                          "signals": {"true_volume": 3, "bear_ratio": 0.30, "bull_ratio": 0.40}}]
    pss_fallback = PE.price_sentiment_series_from_snapshots(
        snaps_0804, price_daily_missing_0804, price_intraday=price_intraday_has_0804)
    by_date_fb = {r["date"]: r for r in pss_fallback}
    check("pss: intraday fallback fills in a day missing from the daily endpoint",
          "2026-08-04" in by_date_fb)
    check("pss: intraday-derived OHLC is correct (open=first,high=max,low=min,close=last)",
          by_date_fb["2026-08-04"]["price_open"] == 4100.0
          and by_date_fb["2026-08-04"]["price_high"] == 4300.0
          and by_date_fb["2026-08-04"]["price_low"] == 4080.0
          and by_date_fb["2026-08-04"]["price_close"] == 4280.0
          and by_date_fb["2026-08-04"]["price_volume"] == 1100000)
    check("pss: intraday-derived day still carries its own sentiment",
          by_date_fb["2026-08-04"]["bull_ratio"] == 0.40)
    # 日足に既にある日は、日中足があっても上書きしない(公式値を優先)
    check("pss: existing daily-bar day is NOT overridden by intraday fallback",
          by_date_fb["2026-08-03"]["price_close"] == 4200.0)
    # price_intraday省略時は従来通り(補完なし)の挙動を維持する(後方互換)
    pss_no_fallback = PE.price_sentiment_series_from_snapshots(snaps_0804, price_daily_missing_0804)
    check("pss: without price_intraday, missing day stays excluded (backward compatible)",
          "2026-08-04" not in {r["date"] for r in pss_no_fallback})


def test_public_export_intraday_today_series():
    """★2026-08-19追加(ユーザー依頼「当日の価格推移とセンチメント推移も入れる」・同日中に
    「本日は10分足、14日分は日足に」との追加依頼で価格側を10分リサンプルへ変更)。
    intraday_today_series()が(a)本日分のsnapshots/price_intraidayバーだけを抽出し
    他日分を混入させないこと(b)価格barを10分足へリサンプルする(同じ10分バケット内の
    複数バーはバケット内最後の値=より新しい観測を採用)こと(c)価格barのepoch ts +
    gmtoffsetでのJST変換が正しいこと(d)データ無しでもクラッシュせず空リストを返すこと、
    を検証する。"""
    today = "2026-08-19"
    snaps = [
        {"date": "2026-08-18", "timestamp": "2026-08-18T15:00:00",
         "signals": {"bull_ratio": 0.9, "bear_ratio": 0.05}},   # 前日分=混入しないこと
        {"date": today, "timestamp": f"{today}T09:05:00",
         "signals": {"bull_ratio": 0.30, "bear_ratio": 0.55}},
        {"date": today, "timestamp": f"{today}T09:15:00",
         "signals": {"bull_ratio": 0.32, "bear_ratio": 0.50}},
    ]
    # ts は UNIX epoch(秒)。gmtoffset=32400(JST+9h)で 09:00 JST 相当を作る
    # (price_sentiment_series系のテストと同じ変換パターン)。
    # ES._epoch_day(date_str) は「その日 09:00 JST」のepoch秒を返す(日足の代表時刻)ため、
    # 09:00 JSTちょうどのバーはそのまま・+300秒=09:05JST・+900秒=09:15JST。
    # 09:00と09:05は同じ10分バケット(09:00-09:09)に入るため、リサンプル後は
    # そのバケット内で後着(09:05側=50100.0)の値が採用されるはず。09:15は別バケット。
    price_intraday = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day("2026-08-18"), "open": 51000.0, "high": 51000.0,
             "low": 51000.0, "close": 51000.0, "volume": 999999},          # 前日09:00 JST分
            {"ts": ES._epoch_day(today), "open": 49900.0, "high": 50050.0,
             "low": 49850.0, "close": 50000.0, "volume": 10000},           # 09:00 JST(同バケット・最初)
            {"ts": ES._epoch_day(today) + 300, "open": 50000.0, "high": 50150.0,
             "low": 49950.0, "close": 50100.0, "volume": 15000},           # 09:05 JST(同バケット・後着)
            {"ts": ES._epoch_day(today) + 900, "open": 50100.0, "high": 50250.0,
             "low": 50080.0, "close": 50200.0, "volume": 5000},            # 09:15 JST(別バケット)
        ],
    }
    out = PE.intraday_today_series(snaps, price_intraday, today=today)
    check("intraday-today: only today's price bars included (prior day excluded)",
          len(out["price"]) == 2)   # 09:00バケット1つ + 09:10バケット1つ = 2点(10分リサンプル)
    check("intraday-today: price times bucketed to 10-min JST marks",
          {p["time"] for p in out["price"]} == {"09:00", "09:10"})
    price_by_time = {p["time"]: p["price_close"] for p in out["price"]}
    check("intraday-today: 09:00 bucket takes the later-observed value within it (50100, not 50000)",
          price_by_time["09:00"] == 50100.0)
    check("intraday-today: 09:15 bar falls into the 09:10 bucket with its own value",
          price_by_time["09:10"] == 50200.0)
    # ★2026-08-19追加(ユーザー依頼「価格推移をローソク足にしてほしい」): OHLC集計。
    # 09:00バケットは2本のbar(09:00,09:05)から成る -> open=最初のbarのopen(49900)・
    # high=両barの最大(50150)・low=両barの最小(49850)・close=最後のbarのclose(50100)。
    p0900 = next(p for p in out["price"] if p["time"] == "09:00")
    check("intraday-today: bucket open = first bar's open", p0900["price_open"] == 49900.0)
    check("intraday-today: bucket high = max across bars in bucket", p0900["price_high"] == 50150.0)
    check("intraday-today: bucket low = min across bars in bucket", p0900["price_low"] == 49850.0)
    check("intraday-today: bucket close = last bar's close", p0900["price_close"] == 50100.0)
    # ★2026-08-19追加(ユーザー依頼「価格推移に出来高を足せますか」): 出来高は
    # バケット内の全bar合計(09:00バケット=10000+15000=25000)。
    check("intraday-today: bucket volume = sum of bars in bucket", p0900["price_volume"] == 25000)
    p0910 = next(p for p in out["price"] if p["time"] == "09:10")
    check("intraday-today: single-bar bucket volume = that bar's volume", p0910["price_volume"] == 5000)
    check("intraday-today: only today's sentiment snapshots included (prior day excluded)",
          len(out["sentiment"]) == 2)
    check("intraday-today: sentiment times taken from snapshot timestamp",
          {s["time"] for s in out["sentiment"]} == {"09:05", "09:15"})
    check("intraday-today: sentiment ratios preserved",
          any(s["bull_ratio"] == 0.30 for s in out["sentiment"]))
    check("intraday-today: no leak keys", PE.validate_no_leak(out) == [])

    # データ無し(空/None)でもクラッシュせず空リストを返す(fail-soft)
    empty = PE.intraday_today_series([], None, today=today)
    check("intraday-today: empty snapshots + no price -> both lists empty (no crash)",
          empty == {"price": [], "sentiment": []})

    # ★2026-08-19追加(ユーザー指摘「引けの時の出来高は出ませんか」): price_fetch.pyの
    # 末尾volume=0合成バー(現在値のみ・実取引なし)が、直前の実取引が無い"新しい"
    # 10分バケット境界(例:15:30:00)にちょうど乗った場合、単独のvolume=0バケットに
    # ならず、直前バケットへ終値/高値/安値だけ吸収されること(=出来高ゼロの
    # 空ローソクが引けの位置に残らない)を検証する。
    close_today = "2026-08-19"
    price_intraday_close = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            # 15:20 JST台の実バー(出来高あり)
            {"ts": ES._epoch_day(close_today) + 6 * 3600 + 20 * 60, "open": 50200.0,
             "high": 50340.0, "low": 49930.0, "close": 49970.0, "volume": 622600},
            # 15:30:00 JSTちょうど=引け直後のYahoo合成現在値バー(volume=0・単独で新バケット化)
            {"ts": ES._epoch_day(close_today) + 6 * 3600 + 30 * 60, "open": 49950.0,
             "high": 49950.0, "low": 49950.0, "close": 49950.0, "volume": 0},
        ],
    }
    out_close = PE.intraday_today_series([], price_intraday_close, today=close_today)
    check("intraday-today(close-merge): no separate 15:30 zero-volume bucket created",
          "15:30" not in {p["time"] for p in out_close["price"]})
    check("intraday-today(close-merge): exactly 1 bucket remains (15:20)",
          len(out_close["price"]) == 1 and out_close["price"][0]["time"] == "15:20")
    check("intraday-today(close-merge): closing price (49950) absorbed into last real bucket",
          out_close["price"][0]["price_close"] == 49950.0)
    check("intraday-today(close-merge): real volume from 15:20 preserved (not zeroed out)",
          out_close["price"][0]["price_volume"] == 622600)

    # 対照: 合成バーが単独でなく実バーと同じバケットに収まる場合(例: 15:25着地)は
    # 従来通りそのバケット内の一部として自然に集計され、削除・吸収は起きない。
    price_intraday_same_bucket = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day(close_today) + 6 * 3600 + 20 * 60, "open": 50200.0,
             "high": 50340.0, "low": 49930.0, "close": 49970.0, "volume": 622600},
            {"ts": ES._epoch_day(close_today) + 6 * 3600 + 25 * 60, "open": 49950.0,
             "high": 49950.0, "low": 49950.0, "close": 49950.0, "volume": 0},
        ],
    }
    out_same = PE.intraday_today_series([], price_intraday_same_bucket, today=close_today)
    check("intraday-today(close-merge): synthetic bar sharing an existing bucket is not dropped",
          len(out_same["price"]) == 1 and out_same["price"][0]["time"] == "15:20"
          and out_same["price"][0]["price_close"] == 49950.0
          and out_same["price"][0]["price_volume"] == 622600)

    # 単独バケットしか無い(直前バケットが無い)場合はマージ対象が無いため
    # そのまま残す(出来高ゼロを捏造で埋めない・fail-soft)。
    price_intraday_lone = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day(close_today), "open": 49950.0, "high": 49950.0,
             "low": 49950.0, "close": 49950.0, "volume": 0},
        ],
    }
    out_lone = PE.intraday_today_series([], price_intraday_lone, today=close_today)
    check("intraday-today(close-merge): sole zero-volume bucket kept as-is (nothing to merge into)",
          len(out_lone["price"]) == 1 and out_lone["price"][0]["price_volume"] == 0)

    # ★2026-08-20追加(ユーザー依頼「センチメント推移のグラフに、投稿量の棒グラフを
    # 足せますか」): signals.true_volumeは当日リセット・単調増加の累積投稿数
    # (実測確認済み)。先頭点はtrue_volumeそのもの、以降は直前スナップショットとの
    # 差分(=その区間の新規投稿数)がpost_countとして出ること。日境界(前日23:xx→
    # 当日00:xx)は today フィルタで除外されるため差分計算に混入しないこと。
    today_pc = "2026-08-19"
    snaps_pc = [
        {"date": "2026-08-18", "timestamp": "2026-08-18T23:57:00",
         "signals": {"bull_ratio": 0.5, "bear_ratio": 0.5, "true_volume": 17650}},  # 前日分=混入しないこと
        {"date": today_pc, "timestamp": f"{today_pc}T09:05:00",
         "signals": {"bull_ratio": 0.30, "bear_ratio": 0.55, "true_volume": 120}},
        {"date": today_pc, "timestamp": f"{today_pc}T09:15:00",
         "signals": {"bull_ratio": 0.32, "bear_ratio": 0.50, "true_volume": 310}},
        {"date": today_pc, "timestamp": f"{today_pc}T09:25:00",
         "signals": {"bull_ratio": 0.35, "bear_ratio": 0.48, "true_volume": 305}},  # 減少(補正等)->0扱い
    ]
    out_pc = PE.intraday_today_series(snaps_pc, None, today=today_pc)
    sent_by_time = {s["time"]: s for s in out_pc["sentiment"]}
    check("intraday-today(post_count): first point uses true_volume itself (no prior to diff against)",
          sent_by_time["09:05"]["post_count"] == 120)
    check("intraday-today(post_count): later points are the diff vs the previous snapshot",
          sent_by_time["09:15"]["post_count"] == 190)   # 310-120
    check("intraday-today(post_count): a decrease is clamped to 0, not fabricated negative",
          sent_by_time["09:25"]["post_count"] == 0)     # 305-310 -> clamped
    # true_volumeが無い(既存のsnapshot形式)場合はNoneのまま(捏造しない)。
    snaps_no_tv = [{"date": today_pc, "timestamp": f"{today_pc}T10:00:00",
                    "signals": {"bull_ratio": 0.4, "bear_ratio": 0.4}}]
    out_no_tv = PE.intraday_today_series(snaps_no_tv, None, today=today_pc)
    check("intraday-today(post_count): missing true_volume -> post_count None (fail-soft)",
          out_no_tv["sentiment"][0]["post_count"] is None)


def test_public_export_adr_pts_price_fallback():
    """★2026-08-20追加(ユーザー指摘「最新の株価が反映されていない」への対応)。
    実測で確認済みの障害: Yahoo Finance chart API(query1.finance.yahoo.com)が
    本日分の出来高>0のbarを一切返さなくなり(regularMarketTimeだけは進むが価格・
    出来高は凍結)、ヘッダー現在値・本日の推移チャートの両方が前日終値のまま
    固まってしまう。Yahooとは完全独立なnikkei225jp.comのTSE現在値フィード
    (adr_pts.tse列)へのフォールバック一式(_today_yahoo_has_volume /
    _previous_close_price / _latest_tse_price_from_adr_pts /
    _today_tse_ohlc_from_adr_pts / intraday_today_seriesのadr_pts引数)を検証する。"""
    today = "2026-08-20"

    # --- _today_yahoo_has_volume ---
    price_intraday_stale = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day(today), "open": 49950.0, "high": 49950.0,
             "low": 49950.0, "close": 49950.0, "volume": 0},   # 合成バーのみ・出来高0
        ],
    }
    check("today-has-volume: single zero-volume synthetic bar -> False",
          PE._today_yahoo_has_volume(price_intraday_stale, today=today) is False)
    check("today-has-volume: no bars at all -> False",
          PE._today_yahoo_has_volume({"meta": {"gmtoffset": 32400}, "bars": []},
                                     today=today) is False)
    check("today-has-volume: None price_intraday -> False (fail-soft)",
          PE._today_yahoo_has_volume(None, today=today) is False)
    price_intraday_healthy = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day(today), "open": 49950.0, "high": 50100.0,
             "low": 49900.0, "close": 50050.0, "volume": 12345},   # 実取引あり
        ],
    }
    check("today-has-volume: a real-volume bar present today -> True",
          PE._today_yahoo_has_volume(price_intraday_healthy, today=today) is True)
    # 前日分のvolumeは無視する(本日判定に混入しない)
    price_intraday_only_yesterday = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day("2026-08-19"), "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "volume": 99999},
        ],
    }
    check("today-has-volume: only yesterday's bar has volume -> False for today",
          PE._today_yahoo_has_volume(price_intraday_only_yesterday, today=today) is False)

    # --- _previous_close_price ---
    price_daily = {
        "meta": {"gmtoffset": 32400},
        "bars": [
            {"ts": ES._epoch_day("2026-08-18"), "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 61840.0, "volume": 1},
            {"ts": ES._epoch_day("2026-08-19"), "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 49950.0, "volume": 1},
            {"ts": ES._epoch_day(today), "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 99999.0, "volume": 1},   # 本日分は無視されるはず
        ],
    }
    check("previous-close: returns the latest date strictly before `today` (not today's own bar)",
          PE._previous_close_price(price_daily, today=today) == 49950.0)
    check("previous-close: no prior dates -> None (fail-soft)",
          PE._previous_close_price({"bars": []}, today=today) is None)

    # --- _latest_tse_price_from_adr_pts / _today_tse_ohlc_from_adr_pts ---
    adr_pts = {
        "rows": [
            {"ts": ES._epoch_day("2026-08-19"), "tse": 12345.0,
             "pts": None, "adr_yen": None, "adr_usd": None},   # 前日分=混入しないこと
            {"ts": ES._epoch_day(today) + 0,   "tse": 50500.0,
             "pts": None, "adr_yen": None, "adr_usd": None},
            {"ts": ES._epoch_day(today) + 60,  "tse": 50750.0,
             "pts": None, "adr_yen": None, "adr_usd": None},
            {"ts": ES._epoch_day(today) + 120, "tse": None,
             "pts": None, "adr_yen": None, "adr_usd": None},   # 欠損行=無視すること
            {"ts": ES._epoch_day(today) + 660, "tse": 51220.0,   # +11min -> 次の10分バケット
             "pts": None, "adr_yen": None, "adr_usd": None},
        ],
    }
    latest = PE._latest_tse_price_from_adr_pts(adr_pts, today=today)
    check("latest-tse: returns the most recent today's tse value (ignoring None rows)",
          latest is not None and latest["price"] == 51220.0)
    check("latest-tse: no rows for today -> None",
          PE._latest_tse_price_from_adr_pts(adr_pts, today="2099-01-01") is None)
    check("latest-tse: None adr_pts -> None (fail-soft)",
          PE._latest_tse_price_from_adr_pts(None, today=today) is None)

    ohlc = PE._today_tse_ohlc_from_adr_pts(adr_pts, today=today)
    check("tse-ohlc: buckets into two 10-min groups (09:00 and 09:10)",
          {p["time"] for p in ohlc} == {"09:00", "09:10"})
    b0900 = next(p for p in ohlc if p["time"] == "09:00")
    check("tse-ohlc: 09:00 bucket open/high/low/close from the two tse values in it",
          b0900["price_open"] == 50500.0 and b0900["price_high"] == 50750.0
          and b0900["price_low"] == 50500.0 and b0900["price_close"] == 50750.0)
    check("tse-ohlc: price_volume is always None (feed has no volume, not fabricated)",
          all(p["price_volume"] is None for p in ohlc))

    # --- intraday_today_series(adr_pts=...) 統合: Yahooが本日データ無しの時だけ差し替え ---
    snaps = []
    out_fallback = PE.intraday_today_series(snaps, price_intraday_stale, today=today,
                                            adr_pts=adr_pts)
    check("intraday-today(adr_pts fallback): Yahoo has no volume today -> price comes from adr_pts",
          {p["time"] for p in out_fallback["price"]} == {"09:00", "09:10"}
          and all(p["price_volume"] is None for p in out_fallback["price"]))

    out_healthy = PE.intraday_today_series(snaps, price_intraday_healthy, today=today,
                                           adr_pts=adr_pts)
    check("intraday-today(adr_pts fallback): Yahoo has real volume today -> adr_pts NOT used "
         "(existing behavior unchanged)",
          len(out_healthy["price"]) == 1 and out_healthy["price"][0]["price_volume"] == 12345)

    out_no_adr = PE.intraday_today_series(snaps, price_intraday_stale, today=today, adr_pts=None)
    check("intraday-today(adr_pts fallback): adr_pts omitted -> old Yahoo-only behavior unchanged "
         "(lone zero-volume bucket kept as-is, not fabricated away)",
          len(out_no_adr["price"]) == 1 and out_no_adr["price"][0]["price_volume"] == 0)


def test_public_export_live_price_staleness_and_trading_hours():
    """★2026-08-20追加(ユーザー提案「live_price_bridgeの死活監視」への対応)。
    live_price_staleness_minutes()(fail-soft・分単位の経過時間)と_is_trading_hours()
    (取引時間帯の簡易判定・土日除外)を検証する。"""
    now = dt.datetime(2026, 8, 20, 10, 5, 0)   # 木曜 10:05 JST(取引時間内)
    check("staleness: 3 minutes ago -> 3.0",
          PE.live_price_staleness_minutes("2026-08-20T10:02:00", now=now) == 3.0)
    check("staleness: exactly now -> 0.0",
          PE.live_price_staleness_minutes("2026-08-20T10:05:00", now=now) == 0.0)
    check("staleness: None/empty generated_at -> None (fail-soft)",
          PE.live_price_staleness_minutes(None, now=now) is None
          and PE.live_price_staleness_minutes("", now=now) is None)
    check("staleness: unparseable string -> None (fail-soft, no crash)",
          PE.live_price_staleness_minutes("not-a-date", now=now) is None)

    check("trading-hours: Thu 10:05 (morning session) -> True",
          PE._is_trading_hours(now=dt.datetime(2026, 8, 20, 10, 5)) is True)
    check("trading-hours: Thu 13:00 (afternoon session) -> True",
          PE._is_trading_hours(now=dt.datetime(2026, 8, 20, 13, 0)) is True)
    check("trading-hours: Thu 11:45 (lunch break) -> False",
          PE._is_trading_hours(now=dt.datetime(2026, 8, 20, 11, 45)) is False)
    check("trading-hours: Thu 08:59 (before open) -> False",
          PE._is_trading_hours(now=dt.datetime(2026, 8, 20, 8, 59)) is False)
    check("trading-hours: Thu 15:30 (at/after close) -> False",
          PE._is_trading_hours(now=dt.datetime(2026, 8, 20, 15, 30)) is False)
    check("trading-hours: Saturday 10:05 -> False (weekend excluded)",
          PE._is_trading_hours(now=dt.datetime(2026, 8, 22, 10, 5)) is False)


def test_public_export_market_session_label():
    """★2026-08-21追加(ユーザー依頼「公開ダッシュボードのAI考察では、日本市場の
    開場時間帯を考慮した考察をするように」)。market_session_label()
    (_is_trading_hours()より粒度細かい6区分の市場状態ラベル)を検証する。"""
    check("session: Thu 08:59 -> 寄り付き前",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 8, 59)) == "寄り付き前")
    check("session: Thu 09:00 (open) -> 前場中",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 9, 0)) == "前場中")
    check("session: Thu 10:05 -> 前場中",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 10, 5)) == "前場中")
    check("session: Thu 11:30 (lunch boundary) -> 昼休み",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 11, 30)) == "昼休み")
    check("session: Thu 11:45 -> 昼休み",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 11, 45)) == "昼休み")
    check("session: Thu 12:30 (afternoon open) -> 後場中",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 12, 30)) == "後場中")
    check("session: Thu 13:00 -> 後場中",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 13, 0)) == "後場中")
    check("session: Thu 15:30 (at/after close) -> 取引終了後",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 15, 30)) == "取引終了後")
    check("session: Thu 22:00 (night) -> 取引終了後",
          PE.market_session_label(now=dt.datetime(2026, 8, 20, 22, 0)) == "取引終了後")
    check("session: Saturday 10:05 -> 休場(土日) (weekend excluded regardless of time-of-day)",
          PE.market_session_label(now=dt.datetime(2026, 8, 22, 10, 5)) == "休場(土日)")
    check("session: Sunday 15:00 -> 休場(土日)",
          PE.market_session_label(now=dt.datetime(2026, 8, 23, 15, 0)) == "休場(土日)")


def test_public_export_next_commentary_failure_streak():
    """★2026-08-20追加(ユーザー提案「AI考察生成の失敗が静かに握りつぶされない
    ように」への対応)。next_commentary_failure_streak()(純関数・ファイルI/O自体は
    run_once._update_commentary_failure_streak()側の責務)を検証する。"""
    check("streak: success always resets to 0 regardless of prior streak",
          PE.next_commentary_failure_streak(5, succeeded=True) == 0)
    check("streak: first failure from 0 -> 1",
          PE.next_commentary_failure_streak(0, succeeded=False) == 1)
    check("streak: failure increments prior streak",
          PE.next_commentary_failure_streak(2, succeeded=False) == 3)
    check("streak: None prior streak treated as 0 (fail-soft)",
          PE.next_commentary_failure_streak(None, succeeded=False) == 1)


def test_public_export_next_lock_busy_streak():
    """★2026-08-21追加(おにや提案・連携ログ2026-08-21 01:38投稿=エンジニアの深堀質問③
    「analyzeロック長時間占有の自動検知は無いのでは」への回答で発覚)。
    next_lock_busy_streak()(純関数・ファイルI/O自体はrun_once._update_lock_busy_streak()
    側の責務)を検証する。next_commentary_failure_streakと同型のロジックだが、
    引数の意味(succeeded↔was_busy)が反転している点を明示的に確認する。"""
    check("lock-streak: not-busy(取得成功) always resets to 0 regardless of prior streak",
          PE.next_lock_busy_streak(5, was_busy=False) == 0)
    check("lock-streak: first busy from 0 -> 1",
          PE.next_lock_busy_streak(0, was_busy=True) == 1)
    check("lock-streak: busy increments prior streak",
          PE.next_lock_busy_streak(2, was_busy=True) == 3)
    check("lock-streak: None prior streak treated as 0 (fail-soft)",
          PE.next_lock_busy_streak(None, was_busy=True) == 1)


def test_public_export_board_score_daily_series():
    """★2026-08-20追加(ユーザー提案「灼熱/阿鼻叫喚メーターに推移スパークラインを」)。
    board_score_daily_series()(history.jsonl相当の行から日別最終値を拾う純関数)を
    検証する。"""
    today = "2026-08-20"
    history = [
        {"generated_at": "2026-08-18T09:00:00", "board": {"overheat_score": 10, "capitulation_score": 0}},
        {"generated_at": "2026-08-18T15:30:00", "board": {"overheat_score": 22, "capitulation_score": 5}},
        {"generated_at": "2026-08-19T09:00:00", "board": {"overheat_score": 8, "capitulation_score": 0}},
        {"generated_at": "2026-08-19T15:30:00", "board": {"overheat_score": 30, "capitulation_score": 12}},
        # 当日分(today以降)は除外されるべき
        {"generated_at": "2026-08-20T09:00:00", "board": {"overheat_score": 99, "capitulation_score": 99}},
    ]
    series = PE.board_score_daily_series(history, days=14, today=today)
    check("board_history: 2 distinct prior days returned", len(series) == 2)
    check("board_history: each day's LAST record wins (not first)",
          series[0]["date"] == "2026-08-18" and series[0]["overheat_score"] == 22
          and series[1]["date"] == "2026-08-19" and series[1]["overheat_score"] == 30)
    check("board_history: today's own rows excluded",
          all(p["date"] != today for p in series))
    check("board_history: days= truncates to most recent N",
          len(PE.board_score_daily_series(history, days=1, today=today)) == 1)
    check("board_history: empty history -> []",
          PE.board_score_daily_series([], today=today) == [])
    check("board_history: missing generated_at row skipped, no crash",
          PE.board_score_daily_series([{"board": {"overheat_score": 1}}], today=today) == [])


def test_public_export_signal_cards_daily_series():
    """★2026-08-25追加(ユーザー指摘「公開用ダッシュボード(streamlit版)は直ってないのでは」
    =画像版のみに実装していたシグナル発火状況(9指標)の推移スパークラインをstreamlit版にも
    追加するにあたり、board_score_daily_series()と同じ「読み取り専用モジュールへ集約する」
    設計へ揃えたsignal_cards_daily_series()を検証する。"""
    today = "2026-08-20"
    history = [
        {"generated_at": "2026-08-18T09:00:00",
         "signal_cards": [{"name": "ネームド集中", "value": 0.05}]},
        {"generated_at": "2026-08-18T15:30:00",
         "signal_cards": [{"name": "ネームド集中", "value": 0.12}]},
        {"generated_at": "2026-08-19T15:30:00",
         "signal_cards": [{"name": "ネームド集中", "value": 0.30}]},
        # 当日分(today以降)は除外されるべき
        {"generated_at": "2026-08-20T09:00:00",
         "signal_cards": [{"name": "ネームド集中", "value": 0.99}]},
    ]
    series = PE.signal_cards_daily_series(history, days=14, today=today)
    check("signal_cards_history: 2 distinct prior days returned", len(series) == 2)
    check("signal_cards_history: each day's LAST record wins (not first)",
          series[0]["date"] == "2026-08-18" and series[0]["ネームド集中"] == 0.12
          and series[1]["date"] == "2026-08-19" and series[1]["ネームド集中"] == 0.30)
    check("signal_cards_history: today's own rows excluded",
          all(p["date"] != today for p in series))
    check("signal_cards_history: days= truncates to most recent N",
          len(PE.signal_cards_daily_series(history, days=1, today=today)) == 1)
    check("signal_cards_history: empty history -> []",
          PE.signal_cards_daily_series([], today=today) == [])
    check("signal_cards_history: row without signal_cards skipped, no crash",
          PE.signal_cards_daily_series([{"generated_at": "2026-08-18T09:00:00"}], today=today) == [])


# ============================================================================
# news_fetch.py(★2026-08-27追加・ユーザー依頼「24時間以内のキオクシアに関係
# しそうなニュースの要約を公開ダッシュボードに」「検索頻度は10分毎・新ニュースが
# 出たら更新とリンクを」)
# ============================================================================
_NEWS_RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel>
<item>
  <title>キオクシア、通期見通しを上方修正</title>
  <link>https://news.example.com/a</link>
  <pubDate>{recent}</pubDate>
  <source url="https://nikkei.com">日本経済新聞</source>
</item>
<item>
  <title>半導体市況、回復基調続く</title>
  <link>https://news.example.com/b</link>
  <pubDate>{stale}</pubDate>
  <source url="https://reuters.com">Reuters</source>
</item>
<item>
  <title>タイトルもリンクも無い壊れた項目は無視される</title>
  <pubDate>{recent}</pubDate>
</item>
</channel></rss>"""


def test_news_fetch_pure_functions():
    """news_fetch.pyの純関数群(parse_rss_xml/_parse_rfc822/filter_last_24h/
    dedup_links/find_new_items/build_rss_url)を検証する。ネットワーク非依存。"""
    import email.utils as eut
    now = dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    recent = eut.format_datetime(now - dt.timedelta(hours=2))   # 24h以内
    stale = eut.format_datetime(now - dt.timedelta(hours=30))   # 24h超(除外されるべき)
    xml_text = _NEWS_RSS_FIXTURE.format(recent=recent, stale=stale)

    # ---- build_rss_url ----
    import urllib.parse as _urlp
    url = NF.build_rss_url("キオクシア")
    check("news: build_rss_url uses google news rss host", "news.google.com/rss/search" in url)
    check("news: build_rss_url url-encodes the query (round-trips back to original)",
          "キオクシア" not in url and _urlp.unquote(url).find("キオクシア") != -1)
    check("news: build_rss_url falls back to config.NEWS_SEARCH_QUERY when omitted",
          config.NEWS_SEARCH_QUERY in _urlp.unquote(NF.build_rss_url()))

    # ---- parse_rss_xml ----
    items = NF.parse_rss_xml(xml_text)
    check("news: parse_rss_xml extracts title/link/source for well-formed items", len(items) == 2)
    check("news: parse_rss_xml drops items missing title/link (3rd fixture item)",
          all(it["link"] in ("https://news.example.com/a", "https://news.example.com/b")
             for it in items))
    check("news: parse_rss_xml keeps source", items[0]["source"] == "日本経済新聞")
    check("news: parse_rss_xml malformed xml -> [] (fail-soft, no crash)",
          NF.parse_rss_xml("<not><valid") == [])
    check("news: parse_rss_xml empty string -> []", NF.parse_rss_xml("") == [])
    check("news: parse_rss_xml None -> []", NF.parse_rss_xml(None) == [])

    # ---- _parse_rfc822 ----
    check("news: _parse_rfc822 parses a valid RFC822 date",
          NF._parse_rfc822(recent) is not None)
    check("news: _parse_rfc822 unparsable string -> None (fail-soft)",
          NF._parse_rfc822("not a date") is None)
    check("news: _parse_rfc822 empty/None -> None", NF._parse_rfc822("") is None
          and NF._parse_rfc822(None) is None)

    # ---- filter_last_24h ----
    within = NF.filter_last_24h(items, now_utc=now.replace(tzinfo=None))
    check("news: filter_last_24h keeps only the recent item", len(within) == 1
          and within[0]["link"] == "https://news.example.com/a")
    check("news: filter_last_24h drops items with no published (fail-soft, no fabrication)",
          NF.filter_last_24h([{"title": "x", "link": "y", "source": None, "published": None}],
                             now_utc=now.replace(tzinfo=None)) == [])
    check("news: filter_last_24h empty input -> []", NF.filter_last_24h([]) == [])

    # ---- dedup_links ----
    dup = [{"title": "a", "link": "https://x/1"}, {"title": "a again", "link": "https://x/1"},
           {"title": "b", "link": "https://x/2"}]
    deduped = NF.dedup_links(dup)
    check("news: dedup_links keeps first occurrence per link, drops repeats", len(deduped) == 2
          and deduped[0]["title"] == "a")

    # ---- find_new_items ----
    all_items = [{"link": "https://x/1"}, {"link": "https://x/2"}, {"link": "https://x/3"}]
    fresh = NF.find_new_items(all_items, seen_links=["https://x/1", "https://x/2"])
    check("news: find_new_items returns only unseen links", len(fresh) == 1
          and fresh[0]["link"] == "https://x/3")
    check("news: find_new_items with no seen_links returns everything",
          len(NF.find_new_items(all_items, seen_links=None)) == 3)

    # ---- classify_item_topic/dedup_similar_titles/balance_items_by_topic
    # (★2026-08-27追加・ユーザー依頼「エヌビディアやサンディスクとかも、キオクシアに
    # 影響するニュースになります。キーワードを拡張して取り込むように」の実データ検証で
    # 発覚した「関連企業の大型ニュースが対象銘柄自身の記事を押し出す」副作用への対策)----
    kw_primary = ["キオクシア", "285A"]
    kw_related = {"エヌビディア": ["エヌビディア", "NVIDIA"], "サンディスク": ["サンディスク", "SanDisk"]}
    check("news: classify_item_topic detects primary keyword",
          NF.classify_item_topic("キオクシア、決算発表", kw_primary, kw_related) == "primary")
    check("news: classify_item_topic detects related group (case-insensitive)",
          NF.classify_item_topic("米国株：nvidia決算好調", kw_primary, kw_related) == "エヌビディア")
    check("news: classify_item_topic detects second related group",
          NF.classify_item_topic("SanDiskがフラッシュ新製品", kw_primary, kw_related) == "サンディスク")
    check("news: classify_item_topic falls back to other when nothing matches",
          NF.classify_item_topic("無関係の見出し", kw_primary, kw_related) == "other")
    check("news: classify_item_topic uses config defaults when omitted",
          NF.classify_item_topic("キオクシア、決算発表") == "primary")

    dup_titles = [
        {"title": "AAAAAAAAAA米エヌビディア四半期最高益", "link": "https://x/n1"},
        {"title": "AAAAAAAAAA米エヌビディア四半期最高益", "link": "https://x/n2"},  # 完全同一(別配信社)
        {"title": "AAAAAAAAAA米エヌビディア四半期最高益だが詳細は別", "link": "https://x/n3"},  # 先頭prefix_len文字一致
        {"title": "キオクシア、岩手に新製造棟", "link": "https://x/k1"},  # 先頭が異なる
    ]
    deduped_titles = NF.dedup_similar_titles(dup_titles, prefix_len=20)
    check("news: dedup_similar_titles drops near-identical wire-copy headlines",
          len(deduped_titles) == 2 and deduped_titles[0]["link"] == "https://x/n1"
          and deduped_titles[1]["link"] == "https://x/k1")

    balance_items = (
        [{"title": f"キオクシア関連ニュース{i}", "link": f"https://x/k{i}"} for i in range(5)]
        + [{"title": f"米エヌビディア決算関連{i}", "link": f"https://x/n{i}"} for i in range(10)]
        + [{"title": f"サンディスク新製品{i}", "link": f"https://x/s{i}"} for i in range(5)]
    )
    balanced = NF.balance_items_by_topic(balance_items, kw_primary, kw_related,
                                         max_per_related_group=3)
    check("news: balance_items_by_topic keeps ALL primary(kioxia)-topic items uncapped",
          sum(1 for it in balanced if it["link"].startswith("https://x/k")) == 5)
    check("news: balance_items_by_topic caps each related group at max_per_related_group",
          sum(1 for it in balanced if it["link"].startswith("https://x/n")) == 3
          and sum(1 for it in balanced if it["link"].startswith("https://x/s")) == 3)
    check("news: balance_items_by_topic preserves original relative order",
          balanced[0]["link"] == "https://x/k0")
    check("news: balance_items_by_topic uses config.NEWS_MAX_PER_RELATED_GROUP when omitted",
          len(NF.balance_items_by_topic(balance_items, kw_primary, kw_related))
          == 5 + min(10, config.NEWS_MAX_PER_RELATED_GROUP) + min(5, config.NEWS_MAX_PER_RELATED_GROUP))


def test_news_fetch_collect_news_io():
    """news_fetch.collect_news()のI/O部分をモック(get_fn/summarizer差し替え)で検証する
    (実ネットワーク接続・実LLM呼び出しはしない)。状態ファイルは一時ディレクトリへ
    退避してから実行し、本番のnews_seen_state.jsonを汚さない
    (public_insight.pyのLOG_PATH退避と同じ分離パターン)。"""
    import tempfile
    saved_path = config.NEWS_SEEN_STATE_PATH
    config.NEWS_SEEN_STATE_PATH = os.path.join(tempfile.mkdtemp(), "news_seen_state.json")
    try:
        now = dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
        import email.utils as eut
        recent = eut.format_datetime(now - dt.timedelta(hours=1))
        xml_text = f"""<?xml version="1.0"?><rss><channel>
        <item><title>初回ニュース</title><link>https://x/first</link>
        <pubDate>{recent}</pubDate><source>テスト</source></item>
        </channel></rss>"""

        def _get_fn(url, timeout=None, headers=None):
            class _R:
                def raise_for_status(self): pass
                text = xml_text
            return _R()

        summarizer_calls = []

        def _summarizer(items):
            summarizer_calls.append(len(items))
            return {"text": "テスト要約です。投資助言ではありません。",
                   "generated_at": "2026-08-27T12:00:00"}

        # 1回目: 新着1件 -> 要約が呼ばれる
        out1 = NF.collect_news(now_utc=now.replace(tzinfo=None),
                               get_fn=_get_fn, summarizer=_summarizer)
        check("collect_news: returns items on success", out1 is not None
              and len(out1["items"]) == 1)
        check("collect_news: has_new True on first sight", out1["has_new"] is True)
        check("collect_news: summary generated (1 LLM call)", len(summarizer_calls) == 1
              and out1["summary"]["text"].startswith("テスト要約"))

        # 2回目: 同じ記事のみ(新着なし) -> 要約は呼ばれず、前回の要約を再利用
        out2 = NF.collect_news(now_utc=now.replace(tzinfo=None),
                               get_fn=_get_fn, summarizer=_summarizer)
        check("collect_news: has_new False on repeat (no new article)", out2["has_new"] is False)
        check("collect_news: summarizer NOT called again when nothing new",
              len(summarizer_calls) == 1)
        check("collect_news: previous summary carried over unchanged",
              out2["summary"]["text"] == out1["summary"]["text"])

        # fetch失敗 -> None(fail-soft)
        def _failing_get_fn(url, timeout=None, headers=None):
            raise RuntimeError("boom (simulated network failure)")
        out_fail = NF.collect_news(now_utc=now.replace(tzinfo=None),
                                   get_fn=_failing_get_fn, summarizer=_summarizer)
        check("collect_news: fetch failure -> None (fail-soft)", out_fail is None)

        # 記事0件(空フィード) -> summaryはNoneへ(古い窓の要約を残さない)
        def _empty_get_fn(url, timeout=None, headers=None):
            class _R:
                def raise_for_status(self): pass
                text = "<rss><channel></channel></rss>"
            return _R()
        out_empty = NF.collect_news(now_utc=now.replace(tzinfo=None),
                                    get_fn=_empty_get_fn, summarizer=_summarizer)
        check("collect_news: no items -> summary cleared to None", out_empty is not None
              and out_empty["items"] == [] and out_empty["summary"] is None)
    finally:
        config.NEWS_SEEN_STATE_PATH = saved_path


def test_public_export_signal_state_changes():
    """★2026-08-20追加(ユーザー提案「9指標の状態変化が分かるように」)。
    signal_state_changes()(現在のカードと直近取引日の最終スナップショットを比較し、
    状態(OK/警戒/発火)が変わったものだけ返す純関数)を検証する。"""
    today = "2026-08-20"
    history = [
        {"generated_at": "2026-08-19T09:00:00",
         "signal_cards": [{"name": "A", "state": "OK"}, {"name": "B", "state": "OK"}]},
        {"generated_at": "2026-08-19T15:30:00",
         "signal_cards": [{"name": "A", "state": "OK"}, {"name": "B", "state": "警戒"}]},
    ]
    current = [{"name": "A", "state": "発火"}, {"name": "B", "state": "警戒"}]
    changes = PE.signal_state_changes(current, history, today=today)
    check("signal_changes: only A changed (OK->発火), B unchanged (警戒->警戒)",
          len(changes) == 1 and changes[0]["name"] == "A"
          and changes[0]["from"] == "OK" and changes[0]["to"] == "発火")
    check("signal_changes: uses the LAST record of the most recent prior day",
          changes[0]["compared_date"] == "2026-08-19")
    check("signal_changes: no prior history -> [] (fail-soft, not mis-flagged as change)",
          PE.signal_state_changes(current, [], today=today) == [])
    check("signal_changes: card with no prior match is skipped (not a false change)",
          PE.signal_state_changes([{"name": "C", "state": "発火"}], history, today=today) == [])


def test_public_export_previous_deltas():
    """★2026-08-20追加(ユーザー提案「AI考察の前回比較を視覚的なバッジでも」)。
    previous_deltas()(rec['previous']と現在値の差分を計算する純関数)を検証する。"""
    rec = {
        "price": {"last": 52950.0}, "board": {"bull_ratio": 0.492, "bear_ratio": 0.422},
        "previous": {"generated_at": "2026-08-20T17:07:48", "price_last": 52950.0,
                    "bull_ratio": 0.491, "bear_ratio": 0.424, "post_count_today": 7015},
    }
    d = PE.previous_deltas(rec)
    check("prev_deltas: price unchanged -> 0.0", d["price_last"] == 0.0)
    check("prev_deltas: bull_ratio delta computed", round(d["bull_ratio"], 3) == 0.001)
    check("prev_deltas: bear_ratio delta computed", round(d["bear_ratio"], 3) == -0.002)
    check("prev_deltas: previous_generated_at passed through",
          d["previous_generated_at"] == "2026-08-20T17:07:48")
    check("prev_deltas: no 'previous' key -> all None (fail-soft)",
          PE.previous_deltas({"price": {"last": 100}}) ==
          {"price_last": None, "bull_ratio": None, "bear_ratio": None,
           "post_count_today": None, "previous_generated_at": None})
    check("prev_deltas: empty/None rec -> no crash", PE.previous_deltas(None)["price_last"] is None)


def test_public_export_sentiment_last_24h_10min():
    """★2026-08-20追加・同日中に再設計(ユーザー指示「本日のセンチメント推移は、過去
    24時間の10分毎のセンチメントの推移に」→ユーザー指摘「投稿量は投稿時刻で
    ばらけさせるべきでは」)。sentiment_last_24h_10min(raw_rows, analyzed_rows, now)
    (各行の実投稿時刻"ts"でバケット化する版)を検証する:
    (a)過去24時間窓のフィルタ(窓外・未来は除外) (b)post_countはraw_rows(全件)を
    tsでバケット化した件数 (c)bull/bear比率はanalyzed_rowsのmeaningful行のみを
    tsでバケット化した比率 (d)収集がまとまって届いても投稿時刻でばらける
    (=旧・収集時刻ベース設計で起きていた実障害の再発防止) (e)meaningful行が
    無いバケットはbull/bear比率がNone(捏造しない)。"""
    now = dt.datetime(2026, 8, 20, 10, 5, 0)
    window_start = now - dt.timedelta(hours=24)  # 2026-08-19 10:05:00

    raw_rows = [
        {"ts": "2026-08-19T10:00:00"},  # 窓外
        {"ts": "2026-08-19T10:10:00"},
        {"ts": "2026-08-19T10:12:00"},  # 同バケット(10:10台)
        {"ts": "2026-08-20T09:58:00"},
        {"ts": "2026-08-20T10:07:00"},  # 窓外(未来)
        {"ts": "not-a-date"},           # 壊れたts -> skip
    ]
    analyzed_rows = [
        {"ts": "2026-08-19T10:10:00", "meaningful": True, "sentiment": "bullish"},
        {"ts": "2026-08-19T10:11:00", "meaningful": True, "sentiment": "bearish"},
        {"ts": "2026-08-19T10:12:00", "meaningful": False, "sentiment": "bullish"},  # not meaningful -> excluded
        {"ts": "2026-08-20T09:58:00", "meaningful": True, "sentiment": "neutral"},
    ]

    out = PE.sentiment_last_24h_10min(raw_rows, analyzed_rows, now=now)
    check("sent24h: 2 buckets (window excludes before-start and after-now raw/analyzed rows)",
          [p["time"] for p in out] == ["8/19 10:10", "8/20 09:50"])

    b0 = next(p for p in out if p["time"] == "8/19 10:10")
    check("sent24h: post_count = raw_rows count in that bucket (2), not a delta of any counter",
          b0["post_count"] == 2)
    check("sent24h: bull/bear ratio computed only from meaningful=True analyzed rows (1 bull, 1 bear -> 0.5/0.5)",
          b0["bull_ratio"] == 0.5 and b0["bear_ratio"] == 0.5)

    b1 = next(p for p in out if p["time"] == "8/20 09:50")
    check("sent24h: neutral-only meaningful bucket -> bull_ratio=0.0, bear_ratio=0.0 (not None; a real 0/1)",
          b1["bull_ratio"] == 0.0 and b1["bear_ratio"] == 0.0)

    # ★核心の回帰確認: 収集がまとまって届いても(=raw_rowsに大量の行が一括で
    # 追記されても)、各行が持つ実投稿時刻できちんとばらけて計上されること
    # (旧・収集時刻ベース設計だと、この全件が「収集が完了した1バケット」へ
    # まるごと計上され、無関係な時刻に不自然なスパイクを生んでいた実障害)。
    burst_raw = (
        [{"ts": "2026-08-19T20:00:00"}] * 30 +   # 20:00台に実際に投稿された分
        [{"ts": "2026-08-19T20:30:00"}] * 50 +   # 20:30台に実際に投稿された分
        [{"ts": "2026-08-19T21:00:00"}] * 40     # 21:00台に実際に投稿された分
    )  # 実際には全て同じ1回の収集run(例: 22:58台)で一括取得された想定でも、
       # tsが異なれば別々のバケットへ正しく配分されるはず。
    out_burst = PE.sentiment_last_24h_10min(burst_raw, [], now=now)
    counts = {p["time"]: p["post_count"] for p in out_burst}
    check("sent24h: a single collection burst is spread across its actual post-time buckets, "
          "not lumped into one",
          counts.get("8/19 20:00") == 30 and counts.get("8/19 20:30") == 50
          and counts.get("8/19 21:00") == 40)
    check("sent24h: no bucket carries the full burst total (120) - proves it's not collection-time-based",
          all(c != 120 for c in counts.values()))

    # 24時間ちょうど離れた同一HH:MMの2点が衝突しないこと(バケットが実datetime基準
    # でソートされるため、文字列キー衝突の懸念自体がそもそも起きない設計)。
    now2 = dt.datetime(2026, 8, 20, 14, 35, 0)
    raw2 = [{"ts": "2026-08-19T14:37:00"}, {"ts": "2026-08-20T14:32:00"}]  # 窓境界(14:35)より後の時刻を選ぶ
    out2 = PE.sentiment_last_24h_10min(raw2, [], now=now2)
    check("sent24h: 24h-apart same-HH:MM points are 2 distinct buckets (not merged)",
          [p["time"] for p in out2] == ["8/19 14:30", "8/20 14:30"])

    check("sent24h: empty rows -> []", PE.sentiment_last_24h_10min([], [], now=now) == [])
    check("sent24h: malformed ts skipped, no crash",
          PE.sentiment_last_24h_10min([{"ts": "not-a-date"}], [], now=now) == [])
    check("sent24h: no leaked per-post fields (text/author/id) in the output",
          all(set(p.keys()) == {"time", "bull_ratio", "bear_ratio", "post_count"} for p in out))


def test_public_dashboard_today_time_buckets():
    """★2026-08-20追加(ユーザー指示「本日の推移のチャートは、昼休みは詰めて表示
    しましょう」)。_today_time_buckets()(前場/後場の想定10分バケット・固定
    テンプレ)と_effective_today_buckets()(テンプレ+実データの和集合)を検証する。"""
    buckets = PD._TODAY_TIME_BUCKETS
    check("today_buckets: no reserved empty slot at lunch boundary (11:30 excluded)",
          "11:30" not in buckets)
    check("today_buckets: last morning slot is 11:20, adjacent to 12:30 (no gap)",
          buckets.index("12:30") == buckets.index("11:20") + 1)
    check("today_buckets: afternoon close 15:30 still included (2026-08-20 fix must not regress)",
          buckets[-1] == "15:30")
    check("today_buckets: 34 total slots (15 morning[9:00-11:20] + 19 afternoon[12:30-15:30])",
          len(buckets) == 34)

    # _effective_today_buckets: テンプレに無い実データ時刻(例: 万一11:30に実
    # ティックが乗った日)も脱落させず和集合へ追加されることを確認。
    eff = PD._effective_today_buckets(["09:00", "11:30", "12:30"])
    check("effective_buckets: template-absent actual time (11:30) still included",
          "11:30" in eff)
    check("effective_buckets: chronological order maintained even with the union",
          eff.index("11:20") < eff.index("11:30") < eff.index("12:30"))
    check("effective_buckets: empty actual_times falls back to the template unchanged",
          PD._effective_today_buckets([]) == buckets)
    check("effective_buckets: None actual_times -> no crash, template unchanged",
          PD._effective_today_buckets(None) == buckets)


def test_public_dashboard_today_time_buckets_60s():
    """★2026-08-21追加(ユーザー依頼「板の買い・売り総計グラフの横軸は、市場が
    開いている時間としてください」)。_today_time_buckets_60s()(60秒粒度の固定
    テンプレ)と_effective_board_totals_buckets()(テンプレ+実データの和集合)・
    _TODAY_TIME_BUCKETS_60S_SET(メンバーシップ判定用)を検証する。"""
    buckets = PD._TODAY_TIME_BUCKETS_60S
    check("today_buckets_60s: starts at market open (09:00)",
          buckets[0] == "09:00")
    check("today_buckets_60s: no lunch-boundary slot (11:30 excluded, same as 10min版)",
          "11:30" not in buckets)
    check("today_buckets_60s: last morning slot 11:20 adjacent to 12:30 (no gap)",
          buckets.index("12:30") == buckets.index("11:20") + 1)
    check("today_buckets_60s: ends at the close (15:30)",
          buckets[-1] == "15:30")
    check("today_buckets_60s: 141 morning[9:00-11:20] + 181 afternoon[12:30-15:30] = 322 slots",
          len(buckets) == 322)
    check("today_buckets_60s_set: matches the list contents (membership check helper)",
          PD._TODAY_TIME_BUCKETS_60S_SET == set(buckets))

    # ★2026-08-21修正(ユーザー指摘「横軸の秒の単位は不要」): ラベル形式を
    # "HH:MM:SS"から"HH:MM"へ単純化(60秒バケットは元々分単位でしか区別が
    # 無かったため情報量の損失は無い)。
    eff = PD._effective_board_totals_buckets(["09:00", "11:25", "12:30"])
    check("effective_board_totals_buckets: template-absent actual time still included",
          "11:25" in eff)
    check("effective_board_totals_buckets: chronological order maintained with the union",
          eff.index("11:20") < eff.index("11:25") < eff.index("12:30"))
    check("effective_board_totals_buckets: empty actual_times falls back to the template unchanged",
          PD._effective_board_totals_buckets([]) == buckets)
    check("effective_board_totals_buckets: None actual_times -> no crash, template unchanged",
          PD._effective_board_totals_buckets(None) == buckets)


def test_public_export_kabu_tick_today_summary():
    """★2026-08-20追加(ユーザー指示「公開版ではYahooでなく自己取得のkabuデータを
    使う」への対応)。株取引API_プロト1の285Aティックcsv(time,price,vwap,volume,
    bid,ask,tickvol・volumeは当日累積出来高)を集計するkabu_tick_today_summary()
    を検証する。csv.DictReader相当のdictリストを直接渡す(ファイルI/O自体は
    live_price_bridge.py側の責務・ここでは純関数部分だけを対象にする)。"""
    today = "2026-08-20"
    rows = [
        {"time": f"{today} 09:00:00.100", "price": "50500.0", "vwap": "50500.0",
         "volume": "367900.0", "bid": "50510.0", "ask": "50500.0", "tickvol": ""},
        {"time": f"{today} 09:03:00.200", "price": "50750.0", "vwap": "50600.0",
         "volume": "500000.0", "bid": "50760.0", "ask": "50750.0", "tickvol": "132100.0"},
        {"time": f"{today} 09:04:59.900", "price": "50300.0", "vwap": "50550.0",
         "volume": "700000.0", "bid": "50310.0", "ask": "50300.0", "tickvol": "200000.0"},
        {"time": f"{today} 09:11:00.000", "price": "50900.0", "vwap": "50700.0",
         "volume": "900000.0", "bid": "50910.0", "ask": "50900.0", "tickvol": "200000.0"},
        # 前日分=本日集計に混入しないこと
        {"time": "2026-08-19 15:29:00.000", "price": "99999.0", "vwap": "99999.0",
         "volume": "12345.0", "bid": "99999.0", "ask": "99999.0", "tickvol": "1.0"},
        # 不正な行(price欠損)=無視されること(fail-soft)
        {"time": f"{today} 09:12:00.000", "price": "", "vwap": "50700.0",
         "volume": "950000.0", "bid": "", "ask": "", "tickvol": "50000.0"},
    ]
    out = PE.kabu_tick_today_summary(rows, today=today)
    check("kabu-tick: 2 buckets formed (09:00 and 09:10)",
          {p["time"] for p in out["price_pts"]} == {"09:00", "09:10"})
    b0900 = next(p for p in out["price_pts"] if p["time"] == "09:00")
    check("kabu-tick: 09:00 bucket open = first tick's price",
          b0900["price_open"] == 50500.0)
    check("kabu-tick: 09:00 bucket high/low across the 3 ticks in it",
          b0900["price_high"] == 50750.0 and b0900["price_low"] == 50300.0)
    check("kabu-tick: 09:00 bucket close = last tick's price in it",
          b0900["price_close"] == 50300.0)
    check("kabu-tick: 09:00 bucket volume = cumulative-volume diff, starting from 0 "
         "(700000 - 0, since these are the first 3 ticks of the day)",
          b0900["price_volume"] == 700000.0)
    b0910 = next(p for p in out["price_pts"] if p["time"] == "09:10")
    check("kabu-tick: 09:10 bucket volume = cumulative diff vs previous bucket (900000-700000)",
          b0910["price_volume"] == 200000.0)
    check("kabu-tick: day_bar open/high/low/close/volume over all of today's ticks",
          out["day_bar"] == {"date": today, "price_open": 50500.0, "price_high": 50900.0,
                             "price_low": 50300.0, "price_close": 50900.0,
                             "price_volume": 900000.0})
    check("kabu-tick: last price/time from the most recent valid tick",
          out["last"] == 50900.0 and out["last_time"] == f"{today} 09:11:00.000")

    empty = PE.kabu_tick_today_summary([], today=today)
    check("kabu-tick: no rows -> price_pts=[]/day_bar=None/last=None (fail-soft)",
          empty == {"price_pts": [], "day_bar": None, "last": None, "last_time": None})

    only_other_day = PE.kabu_tick_today_summary(
        [{"time": "2026-08-19 10:00:00.000", "price": "1.0", "volume": "1.0"}], today=today)
    check("kabu-tick: rows exist but none match today -> same empty result",
          only_other_day["last"] is None)


def _board_row(today, hms, buy_qtys=None, sell_qtys=None, under_buy=None, over_sell=None,
               market_buy=None, market_sell=None):
    """test_public_export_board_totals_60s_series用のfixtureヘルパ。
    buy_qtys/sell_qtysは10個の数量リスト(既定は全て0)。under_buy等をNoneのままにすると
    その列自体をdictに含めない(=拡張前の記録行を模す)。"""
    buy_qtys = buy_qtys or [0] * 10
    sell_qtys = sell_qtys or [0] * 10
    row = {"time": f"{today} {hms}"}
    for i in range(10):
        row[f"buy{i+1}qty"] = str(buy_qtys[i])
        row[f"sell{i+1}qty"] = str(sell_qtys[i])
    if under_buy is not None:
        row["under_buy_qty"] = str(under_buy)
    if over_sell is not None:
        row["over_sell_qty"] = str(over_sell)
    if market_buy is not None:
        row["market_buy_qty"] = str(market_buy)
    if market_sell is not None:
        row["market_sell_qty"] = str(market_sell)
    return row


def test_public_export_intraday_today_high_low():
    """★2026-08-21追加(ユーザー依頼「本日の価格推移のところに、最高値、最安値を
    書くようにしましょう」)。intraday_today_high_low()を検証する。"""
    pts = [
        {"time": "09:00", "price_open": 100, "price_high": 105, "price_low": 98, "price_close": 102},
        {"time": "09:10", "price_open": 102, "price_high": 110, "price_low": 101, "price_close": 108},
        {"time": "09:20", "price_open": 108, "price_high": 109, "price_low": 90, "price_close": 95},
        {"time": "09:30", "price_open": 95, "price_high": None, "price_low": None, "price_close": None},
    ]
    hi, lo = PE.intraday_today_high_low(pts)
    check("high_low: high is max across points (ignoring None)", hi == 110)
    check("high_low: low is min across points (ignoring None)", lo == 90)
    check("high_low: empty list -> (None, None)", PE.intraday_today_high_low([]) == (None, None))
    check("high_low: None input -> (None, None)", PE.intraday_today_high_low(None) == (None, None))
    all_none = [{"time": "09:00", "price_high": None, "price_low": None}]
    check("high_low: all-None points -> (None, None) (no fabrication)",
          PE.intraday_today_high_low(all_none) == (None, None))


def test_public_export_intraday_today_sentiment_10min():
    """★2026-08-21追加(ユーザー依頼「過去24時間センチメント推移を『本日の
    センチメント推移』に変更・本日の価格推移/板の総計と横軸を揃える」)。
    intraday_today_sentiment_10min()を検証する。"""
    pts = [
        # 09:03と09:07は同じ10分バケット(09:00)。bull/bearは最後(09:07)の値・
        # post_countは合計(5+3=8)になること。
        {"time": "09:03", "bull_ratio": 0.4, "bear_ratio": 0.3, "post_count": 5},
        {"time": "09:07", "bull_ratio": 0.5, "bear_ratio": 0.2, "post_count": 3},
        # 09:14は別バケット(09:10)。
        {"time": "09:14", "bull_ratio": 0.6, "bear_ratio": 0.1, "post_count": 2},
        # post_countがNoneの点=このバケット単独ならNoneのまま(捏造しない)。
        {"time": "10:05", "bull_ratio": 0.3, "bear_ratio": 0.4, "post_count": None},
    ]
    out = PE.intraday_today_sentiment_10min(pts)
    check("today-sentiment-10min: 3 buckets (09:00/09:10/10:00)",
          [p["time"] for p in out] == ["09:00", "09:10", "10:00"])
    b0 = out[0]
    check("today-sentiment-10min: bull/bear take last value in bucket",
          b0["bull_ratio"] == 0.5 and b0["bear_ratio"] == 0.2)
    check("today-sentiment-10min: post_count summed within bucket",
          b0["post_count"] == 8)
    check("today-sentiment-10min: single-point bucket passthrough",
          out[1]["bull_ratio"] == 0.6 and out[1]["post_count"] == 2)
    check("today-sentiment-10min: all-None post_count in bucket -> None (no fabrication)",
          out[2]["post_count"] is None)
    check("today-sentiment-10min: empty input -> empty list",
          PE.intraday_today_sentiment_10min([]) == [])
    check("today-sentiment-10min: None input -> empty list",
          PE.intraday_today_sentiment_10min(None) == [])
    check("today-sentiment-10min: short/invalid time skipped",
          PE.intraday_today_sentiment_10min([{"time": "9", "bull_ratio": 1}]) == [])


def test_public_export_sentiment_today_from_last_24h():
    """★2026-08-21追加(ユーザー指摘「投稿量は取得時刻でなく投稿時刻でならす
    ことにしたはずです」)。sentiment_today_from_last_24h()を検証する。"""
    today = "2026-08-21"
    pts_24h = [
        {"time": "8/20 19:30", "bull_ratio": 0.5, "bear_ratio": 0.3, "post_count": 10},
        {"time": "8/21 09:00", "bull_ratio": 0.4, "bear_ratio": 0.4, "post_count": 20},
        {"time": "8/21 12:30", "bull_ratio": 0.45, "bear_ratio": 0.35, "post_count": 30},
        {"time": "8/21 19:20", "bull_ratio": 0.6, "bear_ratio": 0.2, "post_count": 5},
    ]
    out = PE.sentiment_today_from_last_24h(pts_24h, today=today)
    check("sentiment-today: only today's(8/21) entries kept",
          [p["time"] for p in out] == ["09:00", "12:30", "19:20"])
    check("sentiment-today: date prefix stripped to HH:MM",
          out[0]["time"] == "09:00")
    check("sentiment-today: other fields passthrough unchanged",
          out[1]["bull_ratio"] == 0.45 and out[1]["post_count"] == 30)
    check("sentiment-today: no post_count re-aggregation (each bucket already ts-based)",
          out[0]["post_count"] == 20)
    check("sentiment-today: empty input -> empty list",
          PE.sentiment_today_from_last_24h([], today=today) == [])
    check("sentiment-today: no matching day -> empty list",
          PE.sentiment_today_from_last_24h(
              [{"time": "8/19 10:00", "bull_ratio": 0.5, "bear_ratio": 0.5, "post_count": 1}],
              today=today) == [])


def test_public_export_detect_series_outliers():
    """★2026-08-21追加(ユーザー依頼「改善提案②=異常値の自動検出」)。
    detect_series_outliers()を検証する。"""
    # 12:30に本日実際に起きた投稿量スパイク(3,979 vs 前後120台)を模した回帰テスト。
    pts = [{"time": t, "post_count": v} for t, v in [
        ("12:00", 138), ("12:10", 158), ("12:20", 179), ("12:30", 3979),
        ("12:40", 131), ("12:50", 134), ("13:00", 117),
    ]]
    flagged = PE.detect_series_outliers(pts, "post_count")
    check("outliers: the 3,979 spike bucket is flagged", flagged == ["12:30"])

    # 通常の緩やかな変動(全て同程度)は誤検知しないこと。
    normal_pts = [{"time": f"09:{i:02d}", "post_count": 100 + i} for i in range(0, 50, 10)]
    check("outliers: normal gently-varying series -> no false positive",
          PE.detect_series_outliers(normal_pts, "post_count") == [])

    # 閑散区間(値がほぼ0)は誤検知しないこと(min_median未満は判定を見送る)。
    quiet_pts = [{"time": f"09:{i:02d}", "post_count": v} for i, v in
                enumerate([0, 1, 0, 0, 1, 0, 3, 0, 1, 0, 0])]
    check("outliers: near-zero quiet period -> no false positive (median<min_median)",
          PE.detect_series_outliers(quiet_pts, "post_count") == [])

    # None値はスキップされること。
    with_none = [{"time": "a", "post_count": 100}, {"time": "b", "post_count": None},
                {"time": "c", "post_count": 100}, {"time": "d", "post_count": 100},
                {"time": "e", "post_count": 100}]
    check("outliers: None values are skipped without error",
          PE.detect_series_outliers(with_none, "post_count") == [])

    check("outliers: empty input -> empty list",
          PE.detect_series_outliers([], "post_count") == [])


def test_public_export_board_totals_60s_series():
    """★2026-08-21追加(ユーザー依頼「板の買い・売り総計(成行を含めた全価格帯)の
    推移を60秒平均・60秒毎更新の折れ線グラフで」。おにや10:42投稿で仕様確定・
    トレPJ10:47投稿で記録側にover_sell_qty/under_buy_qty/market_sell_qty/
    market_buy_qtyの4列を追加)。board_totals_60s_series()を検証する。"""
    today = "2026-08-21"
    rows = [
        # 11:30:05と11:30:40は同じ分バケット(11:30)・平均される想定。
        _board_row(today, "11:30:05.100", buy_qtys=[100] * 10, sell_qtys=[50] * 10,
                  under_buy=1000, over_sell=2000, market_buy=10, market_sell=20),
        _board_row(today, "11:30:40.300", buy_qtys=[200] * 10, sell_qtys=[150] * 10,
                  under_buy=3000, over_sell=4000, market_buy=30, market_sell=40),
        # 11:31:10は別バケット(11:31)。
        _board_row(today, "11:31:10.000", buy_qtys=[50] * 10, sell_qtys=[50] * 10,
                  under_buy=500, over_sell=500, market_buy=5, market_sell=5),
        # 拡張前の行(新4列が無い)=この指標から除外されること(部分合計を捏造しない)。
        _board_row(today, "09:05:00.000", buy_qtys=[999] * 10, sell_qtys=[999] * 10),
        # 前日分=混入しないこと。
        _board_row("2026-08-20", "15:00:00.000", buy_qtys=[1] * 10, sell_qtys=[1] * 10,
                  under_buy=1, over_sell=1, market_buy=1, market_sell=1),
        # ★2026-08-21修正(ユーザー指摘「15:30近辺が跳ね上がっている」)の回帰テスト:
        # 大引け板寄せ(15:30以降)は連続売買の板状態でなく実測でも桁違いに巨大な
        # 値になる(buy1qty等)ため、この指標からは除外されること。
        _board_row(today, "15:30:00.476", buy_qtys=[472500] + [0] * 9,
                  sell_qtys=[474300] + [0] * 9,
                  under_buy=1115300, over_sell=1077100, market_buy=371300, market_sell=271600),
        _board_row(today, "15:35:00.000", buy_qtys=[1] * 10, sell_qtys=[1] * 10,
                  under_buy=1, over_sell=1, market_buy=1, market_sell=1),
    ]
    out = PE.board_totals_60s_series(rows, today=today)
    # ★2026-08-21修正(ユーザー指摘「横軸の秒の単位は不要」): バケットキーを
    # "HH:MM:SS"から"HH:MM"へ単純化(分単位でしか区別が無かったため情報量の
    # 損失は無い)。
    check("board-totals: 2 buckets formed (11:30 and 11:31; pre-extension/"
         "other-day/post-15:30 rows excluded)",
          [p["time"] for p in out] == ["11:30", "11:31"])
    check("board-totals: 15:30 closing-itayose row excluded (no spike bucket)",
          "15:30" not in [p["time"] for p in out])
    check("board-totals: 15:35(post-close) also excluded",
          "15:35" not in [p["time"] for p in out])

    b1 = out[0]
    # 行1: buy=100*10+1000+10=2010 / sell=50*10+2000+20=2520
    # 行2: buy=200*10+3000+30=5030 / sell=150*10+4000+40=5540
    # 平均: buy=(2010+5030)/2=3520.0 / sell=(2520+5540)/2=4030.0
    check("board-totals: 11:30 bucket averages buy_total across its 2 rows",
          b1["buy_total"] == 3520.0)
    check("board-totals: 11:30 bucket averages sell_total across its 2 rows",
          b1["sell_total"] == 4030.0)

    b2 = out[1]
    # 行3(唯一): buy=50*10+500+5=1005 / sell=50*10+500+5=1005
    check("board-totals: 11:31 bucket (single row) buy_total/sell_total",
          b2["buy_total"] == 1005.0 and b2["sell_total"] == 1005.0)

    check("board-totals: no rows -> empty list (fail-soft)",
          PE.board_totals_60s_series([], today=today) == [])
    check("board-totals: only pre-extension/other-day rows -> empty list",
          PE.board_totals_60s_series(rows[3:], today=today) == [])


def test_public_export_board_totals_60s_series_itayose_shape_detection():
    """★2026-08-21追加(ユーザー依頼「改善提案①=開場直後の板寄せ希薄化リスクに
    先回りで対応」)。時刻境界に頼らず板の"形"(最良気配だけ突出)でitayose行を
    検出・除外することを検証する。実測(2026-08-21 09:01:41)の寄り付き板寄せ値
    (buy1qty=267,100 vs buy2〜10合計=5,600・約48倍)を模した回帰テスト。"""
    today = "2026-08-21"
    # 寄り付き板寄せ(09:01台・大引けの15:30より十分前=時刻境界では捕捉できない)。
    itayose_open = _board_row(
        today, "09:01:41.791",
        buy_qtys=[267100, 200, 1700, 300, 1700, 300, 1000, 200, 100, 100],
        sell_qtys=[271600, 500, 600, 700, 1300, 1300, 1200, 200, 1700, 200],
        under_buy=100, over_sell=100, market_buy=10, market_sell=10)
    # 直後の通常の連続売買(実測: 267,100 -> 100へ急落・比率は正常範囲に戻る)。
    normal_after = _board_row(
        today, "09:02:00.000",
        buy_qtys=[100, 200, 1700, 300, 1700, 300, 1000, 200, 100, 100],
        sell_qtys=[200, 500, 600, 700, 1300, 1300, 1200, 200, 1700, 200],
        under_buy=100, over_sell=100, market_buy=10, market_sell=10)
    out = PE.board_totals_60s_series([itayose_open, normal_after], today=today)
    check("itayose-shape: opening itayose bucket(09:01) excluded entirely "
          "(its only row was itayose-shaped)",
          "09:01" not in [p["time"] for p in out])
    check("itayose-shape: normal row(09:02) kept",
          "09:02" in [p["time"] for p in out])

    # 通常時の緩やかな偏り(比率1〜3倍程度)は誤って除外しないことも確認する。
    mild_imbalance = _board_row(
        today, "10:00:00.000",
        buy_qtys=[2000, 500, 500, 500, 500, 500, 500, 500, 500, 500],
        sell_qtys=[1000, 500, 500, 500, 500, 500, 500, 500, 500, 500],
        under_buy=100, over_sell=100, market_buy=10, market_sell=10)
    out2 = PE.board_totals_60s_series([mild_imbalance], today=today)
    check("itayose-shape: mild imbalance (ratio~1x, not itayose-shaped) not excluded",
          "10:00" in [p["time"] for p in out2])


def test_public_export_prev_close_from_price_sentiment_series():
    """★2026-08-20追加(ユーザー依頼「株価の更新が遅すぎる。60秒ごとの更新時に、
    その時の株価になるように」への対応)。公開ダッシュボード自身が直接ライブ取得
    する際の変化率計算に使う_prev_close_from_price_sentiment_series()を検証する
    (fetch_live_price_header()自体はネットワークI/O込みのため、ここではその純関数
    部分だけを対象にする=既存のload_public_latest_from_url等と同じ分離方針)。"""
    pss = [
        {"date": "2026-08-17", "price_close": 61840.0},
        {"date": "2026-08-19", "price_close": 49950.0},
        {"date": "2026-08-20", "price_close": 99999.0},   # 本日分=無視されるはず
    ]
    check("prev-close(pss): returns the latest date strictly before `today`",
          PE._prev_close_from_price_sentiment_series(pss, today="2026-08-20") == 49950.0)
    check("prev-close(pss): no prior dates -> None (fail-soft)",
          PE._prev_close_from_price_sentiment_series(
              [{"date": "2026-08-20", "price_close": 1.0}], today="2026-08-20") is None)
    check("prev-close(pss): empty/None series -> None (fail-soft)",
          PE._prev_close_from_price_sentiment_series([], today="2026-08-20") is None
          and PE._prev_close_from_price_sentiment_series(None, today="2026-08-20") is None)
    check("prev-close(pss): entries with price_close=None are ignored",
          PE._prev_close_from_price_sentiment_series(
              [{"date": "2026-08-19", "price_close": None},
               {"date": "2026-08-18", "price_close": 61840.0}],
              today="2026-08-20") == 61840.0)


def test_public_export_parse_public_json_csv():
    """★2026-08-19追加(ユーザー依頼: 公開ダッシュボードをStreamlit Community Cloud
    へデプロイ)。Google Sheetsの「ウェブに公開」CSV書き出し(json_blobタブ)を
    パースする_parse_public_json_csv()を検証する。RFC4180のCSVクォート規則
    (セル内カンマ・改行・引用符のエスケープ)を実際のGoogle Sheets CSV出力と
    同じ形で再現し、素朴な文字列分割では壊れるが正しいCSVパーサーなら壊れない
    ことを確認する。"""
    import json
    rec = {"symbol": "285A", "price": {"last": 49950.0}, "note": 'has "quotes", commas'}
    json_text = json.dumps(rec, ensure_ascii=False)
    # Google Sheetsの「ウェブに公開」CSVは各フィールドをダブルクォートで囲み、
    # セル内のダブルクォートは""へエスケープする(RFC4180)。
    csv_text = '"' + json_text.replace('"', '""') + '","2026-08-19T22:42:53"\r\n'
    out = PE._parse_public_json_csv(csv_text)
    check("parse_public_json_csv: round-trips through CSV quoting/escaping correctly",
          out == rec)
    check("parse_public_json_csv: empty text -> None (fail-soft)",
          PE._parse_public_json_csv("") is None)
    check("parse_public_json_csv: garbage CSV -> None (fail-soft)",
          PE._parse_public_json_csv("not,valid,json,in,here\n") is None)
    check("parse_public_json_csv: None input -> None (fail-soft)",
          PE._parse_public_json_csv(None) is None)


def test_public_export_parse_visit_counter_response():
    """★2026-08-20追加(ユーザー依頼: 公開サイトの閲覧者数を集計して表示)。
    閲覧数カウンター用Apps Script Web Appの応答本文をパースする
    _parse_visit_counter_response()を検証する(record_visit()から分離した
    純関数)。"""
    check("parse_visit_counter: valid response -> int count",
          PE._parse_visit_counter_response('{"count": 42}') == 42)
    check("parse_visit_counter: count missing -> None (fail-soft)",
          PE._parse_visit_counter_response('{"other": 1}') is None)
    check("parse_visit_counter: invalid json -> None (fail-soft)",
          PE._parse_visit_counter_response("not json") is None)
    check("parse_visit_counter: empty/None input -> None (fail-soft)",
          PE._parse_visit_counter_response("") is None and
          PE._parse_visit_counter_response(None) is None)

    check("record_visit: no url -> None without raising (fail-soft)",
          PE.record_visit(None) is None and PE.record_visit("") is None)


def test_public_export_validate_no_leak():
    import json
    good = PE.build_public_record(_pe_sample_S(), None,
                                  [{"date": "2026-08-10", "post_count": 100,
                                    "bear_ratio": 0.3, "bull_ratio": 0.4}])
    check("pe-leak: clean record passes (0 errors)", PE.validate_no_leak(good) == [])

    # トップレベルの辞書に個別投稿を模したキーを直接混入
    dirty = json.loads(json.dumps(good))  # deep copy
    dirty["board"]["leaked_sample"] = {
        "text": "買います!", "user": "太郎", "author": "hash_abc123",
        "id": 987654, "votes_yes": 12,
    }
    errs = PE.validate_no_leak(dirty)
    check("pe-leak: detects all 5 injected keys", len(errs) >= 5)
    for key in ("text", "user", "author", "id", "votes_yes"):
        check(f"pe-leak: reports key '{key}'",
              any(f"leaked key '{key}'" in e for e in errs))

    # リストの中(投稿1件分の辞書)に紛れ込むケースも検出できること
    dirty2 = json.loads(json.dumps(good))
    dirty2["trend_14d"] = dirty2["trend_14d"] + [
        {"date": "2026-08-11", "post_count": 3, "author": "someone", "id": 1}]
    errs2 = PE.validate_no_leak(dirty2)
    check("pe-leak: detects leak nested inside list-of-dicts",
          any("author" in e for e in errs2) and any("trend_14d[" in e for e in errs2))

    # キー名の"部分一致"では誤検知しない(post_count_today 等の正規キーはOK)
    check("pe-leak: no false positive on legit aggregate keys",
          PE.validate_no_leak({"board": {"post_count_today": 10, "posts_per_hour": 1.0}}) == [])

    # --- 拡張スキーマ(price_sentiment_series / ai_commentary)に対する検証 ---
    # price_sentiment_series の中に個別投稿ぽいキーを注入 -> 検出されること
    dirty3 = json.loads(json.dumps(good))
    dirty3["price_sentiment_series"] = [
        {"date": "2026-08-10", "price_close": 4250.0, "bear_ratio": 0.3, "bull_ratio": 0.4,
         "author": "someone", "text": "個別コメント混入", "user_id": "u123"}]
    errs3 = PE.validate_no_leak(dirty3)
    check("pe-leak: detects leak inside price_sentiment_series (author)",
          any("author" in e and "price_sentiment_series[" in e for e in errs3))
    check("pe-leak: detects leak inside price_sentiment_series (text)",
          any("leaked key 'text'" in e and "price_sentiment_series[" in e for e in errs3))
    check("pe-leak: detects leak inside price_sentiment_series (user_id)",
          any("user_id" in e and "price_sentiment_series[" in e for e in errs3))

    # ai_commentary.text は唯一の意図的な例外(公開用考察文そのもの)-> 検出されない
    clean_commentary = json.loads(json.dumps(good))
    clean_commentary["ai_commentary"] = {
        "text": "掲示板の集計センチメントデータに基づく分析であり、投資助言ではありません。"
                "強気比率24.9%、弱気比率11.9%でした。",
        "generated_at": "2026-08-16T15:00:00",
    }
    check("pe-leak: ai_commentary.text (exact intended path) is exempt",
          PE.validate_no_leak(clean_commentary) == [])

    # しかし ai_commentary 以外の場所に出た"text"キーは引き続き検出される
    # (例外はパス完全一致のみ・キー名だけでの広範な免除ではないことの確認)
    dirty4 = json.loads(json.dumps(good))
    dirty4["board"]["text"] = "これは個別投稿由来のtextであってai_commentaryではない"
    errs4 = PE.validate_no_leak(dirty4)
    check("pe-leak: 'text' key OUTSIDE ai_commentary.text is still detected",
          any("leaked key 'text'" in e and "board.text" in e for e in errs4))

    # ai_commentary 自体の中に個別投稿ぽい別キー(text以外)が混ざれば検出される
    # (例外は "text" というキー名かつパス完全一致の時だけ・ai_commentary配下を丸ごと
    # 免除するわけではないことの確認)
    dirty5 = json.loads(json.dumps(good))
    dirty5["ai_commentary"] = {"text": "正常な考察文", "author": "leaked_author_hash"}
    errs5 = PE.validate_no_leak(dirty5)
    check("pe-leak: ai_commentary.text still exempt when sibling key present",
          not any("ai_commentary.text" in e for e in errs5))
    check("pe-leak: ai_commentary.author (non-exempt sibling) still detected",
          any("leaked key 'author'" in e and "ai_commentary.author" in e for e in errs5))

    # ai_commentary.text が文字列でない(dict化されている)場合は例外を適用しない
    # (中に個別投稿ぽいキーが紛れていれば検出できるようにする=値の型まで見た防御)
    dirty6 = json.loads(json.dumps(good))
    dirty6["ai_commentary"] = {"text": {"author": "smuggled_via_nested_text"}}
    errs6 = PE.validate_no_leak(dirty6)
    check("pe-leak: ai_commentary.text as non-string still recurses & detects nested leak",
          any("leaked key 'author'" in e for e in errs6))


def test_public_export_write_atomic_and_leak_abort():
    import tempfile, os as _os, json
    d = tempfile.mkdtemp()
    orig_dir, orig_latest, orig_hist = (config.PUBLIC_EXPORT_DIR,
                                        config.PUBLIC_EXPORT_LATEST_PATH,
                                        config.PUBLIC_EXPORT_HISTORY_PATH)
    config.PUBLIC_EXPORT_DIR = d
    config.PUBLIC_EXPORT_LATEST_PATH = _os.path.join(d, "latest.json")
    config.PUBLIC_EXPORT_HISTORY_PATH = _os.path.join(d, "history.jsonl")
    try:
        S = _pe_sample_S()
        rec = PE.write_public_export(S, None, [])
        check("pe-write: latest.json created", _os.path.exists(config.PUBLIC_EXPORT_LATEST_PATH))
        loaded = json.load(open(config.PUBLIC_EXPORT_LATEST_PATH, encoding="utf-8"))
        check("pe-write: latest.json content matches returned record", loaded == rec)
        check("pe-write: written file has no leak", PE.validate_no_leak(loaded) == [])

        PE.write_public_export(S, None, [])  # 2回目
        hist_lines = open(config.PUBLIC_EXPORT_HISTORY_PATH, encoding="utf-8").read().strip().split("\n")
        check("pe-write: history.jsonl append-only (2 lines after 2 writes)", len(hist_lines) == 2)

        # price_sentiment_series + ai_commentary を伴う書き込みも問題なく成功し、
        # そのままlatest.jsonへ往復すること(**kw経由でbuild_public_recordへ渡る)。
        pss = [{"date": "2026-08-10", "price_close": 4250.0, "bear_ratio": 0.25, "bull_ratio": 0.55}]
        rec_c = PE.write_public_export(
            S, None, [], price_sentiment_series=pss,
            ai_commentary={"text": "集計値に基づく客観的な考察文であり投資助言ではない。",
                          "generated_at": "2026-08-16T15:10:00"})
        loaded_c = json.load(open(config.PUBLIC_EXPORT_LATEST_PATH, encoding="utf-8"))
        check("pe-write: price_sentiment_series round-trips", loaded_c["price_sentiment_series"] == pss)
        check("pe-write: ai_commentary round-trips",
              loaded_c["ai_commentary"]["text"] == rec_c["ai_commentary"]["text"])
        check("pe-write: with commentary still passes leak validation", PE.validate_no_leak(loaded_c) == [])

        # build_public_record をモンキーパッチしてわざと漏洩レコードを生成させ、
        # write_public_export が書き込みを中止する(fail-closed)ことを確認する。
        orig_build = PE.build_public_record

        def _dirty_build(*a, **kw):
            r = orig_build(*a, **kw)
            r["board"]["leak"] = {"author": "should-never-be-written"}
            return r

        PE.build_public_record = _dirty_build
        try:
            raised = False
            try:
                PE.write_public_export(S, None, [])
            except ValueError:
                raised = True
            check("pe-write: raises ValueError on leak", raised)
            hist_lines2 = open(config.PUBLIC_EXPORT_HISTORY_PATH,
                              encoding="utf-8").read().strip().split("\n")
            check("pe-write: history NOT appended when leak detected (still 3 lines)",
                  len(hist_lines2) == 3)
        finally:
            PE.build_public_record = orig_build
    finally:
        config.PUBLIC_EXPORT_DIR, config.PUBLIC_EXPORT_LATEST_PATH, \
            config.PUBLIC_EXPORT_HISTORY_PATH = orig_dir, orig_latest, orig_hist


def test_public_export_commentary_daily_gate():
    """AI考察の1日1回ゲート(public_export._commentary_already_generated_today /
    should_generate_commentary_today)が正しく機能することを検証する
    (実LLM呼び出しなし・実ファイルI/Oなし=load_public_latest/load_public_historyをスタブ化)。
    同日2回目はスキップ・日付が変われば再び生成対象になることを確認する。"""
    today = "2026-08-16"
    yesterday = "2026-08-15"

    # ---- 純関数: _commentary_already_generated_today ----
    latest_today = {"ai_commentary": {"text": "x", "generated_at": f"{today}T10:00:00"}}
    check("gate-pure: latest has today's commentary -> already done",
          PE._commentary_already_generated_today(latest_today, [], today) is True)

    latest_yesterday = {"ai_commentary": {"text": "x", "generated_at": f"{yesterday}T10:00:00"}}
    check("gate-pure: latest has yesterday's commentary only -> not done today",
          PE._commentary_already_generated_today(latest_yesterday, [], today) is False)

    # latest.json は commentary無しrunで上書きされるとai_commentaryキー自体が消える。
    # その場合は history.jsonl(append-only)の最新commentary行へフォールバックする。
    history_rows = [
        {"generated_at": f"{yesterday}T09:00:00"},                                   # commentary無し行
        {"ai_commentary": {"text": "x", "generated_at": f"{today}T09:00:00"}},        # 今日生成した行
        {"generated_at": f"{today}T10:00:00"},                                        # 直後のcommentary無しrun
    ]
    check("gate-pure: latest has no ai_commentary key -> falls back to history (finds today)",
          PE._commentary_already_generated_today({}, history_rows, today) is True)
    check("gate-pure: latest=None also falls back to history",
          PE._commentary_already_generated_today(None, history_rows, today) is True)

    history_rows_old = [
        {"ai_commentary": {"text": "x", "generated_at": f"{yesterday}T09:00:00"}},
    ]
    check("gate-pure: history's latest commentary is yesterday -> not done today",
          PE._commentary_already_generated_today({}, history_rows_old, today) is False)

    check("gate-pure: no latest, no history -> not done (first time ever)",
          PE._commentary_already_generated_today(None, [], today) is False)

    # ---- I/O込み: should_generate_commentary_today (load_public_latest/history をスタブ化) ----
    orig_load_latest = PE.load_public_latest
    orig_load_history = PE.load_public_history
    try:
        PE.load_public_latest = lambda: {"ai_commentary": {"generated_at": f"{today}T12:00:00"}}
        PE.load_public_history = lambda: []
        check("gate-io: today already generated (latest.json) -> should_generate=False",
              PE.should_generate_commentary_today(today=today) is False)

        PE.load_public_latest = lambda: {}
        PE.load_public_history = lambda: [
            {"ai_commentary": {"generated_at": f"{yesterday}T12:00:00"}}]
        check("gate-io: only yesterday generated -> should_generate=True (today's 1st run)",
              PE.should_generate_commentary_today(today=today) is True)

        # 同日2回目: 直前のrunで生成済みのhistory行が積まれている状態を模す -> スキップ
        PE.load_public_latest = lambda: {}
        PE.load_public_history = lambda: [
            {"ai_commentary": {"generated_at": f"{yesterday}T12:00:00"}},
            {"ai_commentary": {"generated_at": f"{today}T09:00:00"}}]
        check("gate-io: today already generated earlier today -> should_generate=False (2nd run)",
              PE.should_generate_commentary_today(today=today) is False)

        # 日付が変われば(翌日)再び生成対象になる
        tomorrow = "2026-08-17"
        check("gate-io: date rolls over to tomorrow -> should_generate=True again",
              PE.should_generate_commentary_today(today=tomorrow) is True)
    finally:
        PE.load_public_latest = orig_load_latest
        PE.load_public_history = orig_load_history


def test_run_once_public_export_refresh_step_gate():
    """PUBLIC_EXPORT_AUTO_REFRESH(既定True)がFalse時、run_once._run_public_export_refresh_step()
    が public_export._build_from_live_data() を一切呼ばずにスキップすること、Trueの時は
    呼ぶことを検証する(実データ書込み・実LLM呼び出しなし=スタブ化)。"""
    orig_auto = config.PUBLIC_EXPORT_AUTO_REFRESH
    orig_build = PE._build_from_live_data
    orig_gate = PE.should_generate_commentary_today
    orig_streak = RO._update_commentary_failure_streak
    calls = []
    PE._build_from_live_data = lambda **kw: (calls.append(kw) or {"generated_at": "x"})
    PE.should_generate_commentary_today = lambda *a, **kw: False
    RO._update_commentary_failure_streak = lambda succeeded: 0
    try:
        config.PUBLIC_EXPORT_AUTO_REFRESH = False
        RO._run_public_export_refresh_step()
        check("pexp-gate: PUBLIC_EXPORT_AUTO_REFRESH=False -> _build_from_live_data NOT called",
              len(calls) == 0)

        config.PUBLIC_EXPORT_AUTO_REFRESH = True
        RO._run_public_export_refresh_step()
        check("pexp-gate: PUBLIC_EXPORT_AUTO_REFRESH=True -> _build_from_live_data called once",
              len(calls) == 1)
    finally:
        config.PUBLIC_EXPORT_AUTO_REFRESH = orig_auto
        PE._build_from_live_data = orig_build
        PE.should_generate_commentary_today = orig_gate
        RO._update_commentary_failure_streak = orig_streak


def test_run_once_public_export_refresh_commentary_wiring():
    """PUBLIC_EXPORT_COMMENTARY_DAILYのON/OFFと should_generate_commentary_today() の
    戻り値が、_build_from_live_data(with_commentary=...) へ正しく伝播することを検証する
    (実データ書込み・実LLM呼び出しなし=両方スタブ化)。★2026-08-19: この1日1回ゲートは
    PUBLIC_INSIGHT_BACKEND=="claude"(課金経路)の時だけ適用される設計になったため、
    このテストではbackendを明示的に"claude"へ固定して検証する(既定の"lmstudio"での
    ゲートバイパス挙動は test_run_once_public_export_refresh_lmstudio_bypasses_gate
    で別途検証)。"""
    orig_auto = config.PUBLIC_EXPORT_AUTO_REFRESH
    orig_daily = config.PUBLIC_EXPORT_COMMENTARY_DAILY
    orig_backend = config.PUBLIC_INSIGHT_BACKEND
    orig_build = PE._build_from_live_data
    orig_gate = PE.should_generate_commentary_today
    # ★2026-08-20追加: _run_public_export_refresh_step()にAI考察連続失敗の
    # ストリーク更新(_update_commentary_failure_streak)を組み込んだため、実データ
    # ディレクトリのconfig.AI_COMMENTARY_FAILURE_STATE_PATHへ書き込んでしまわない
    # よう無害化する(このテストの検証対象はwith_commentaryの伝播であり、ストリーク
    # 更新自体は test_public_export_next_commentary_failure_streak で別途検証済み)。
    orig_streak = RO._update_commentary_failure_streak
    RO._update_commentary_failure_streak = lambda succeeded: 0
    calls = []
    PE._build_from_live_data = lambda **kw: (calls.append(kw) or {"generated_at": "x"})
    try:
        config.PUBLIC_EXPORT_AUTO_REFRESH = True
        config.PUBLIC_INSIGHT_BACKEND = "claude"

        # PUBLIC_EXPORT_COMMENTARY_DAILY=False -> ゲート関数自体を呼ばず常にwith_commentary=False
        config.PUBLIC_EXPORT_COMMENTARY_DAILY = False
        gate_calls = []
        PE.should_generate_commentary_today = lambda *a, **kw: (gate_calls.append(1) or True)
        RO._run_public_export_refresh_step()
        check("pexp-wire: COMMENTARY_DAILY=False -> gate not consulted", len(gate_calls) == 0)
        check("pexp-wire: COMMENTARY_DAILY=False -> with_commentary=False",
              calls[-1]["with_commentary"] is False)

        # PUBLIC_EXPORT_COMMENTARY_DAILY=True かつ gate=True(今日未生成) -> with_commentary=True
        config.PUBLIC_EXPORT_COMMENTARY_DAILY = True
        PE.should_generate_commentary_today = lambda *a, **kw: True
        RO._run_public_export_refresh_step()
        check("pexp-wire: COMMENTARY_DAILY=True & gate=True -> with_commentary=True",
              calls[-1]["with_commentary"] is True)

        # gate=False(今日生成済み) -> with_commentary=False
        PE.should_generate_commentary_today = lambda *a, **kw: False
        RO._run_public_export_refresh_step()
        check("pexp-wire: COMMENTARY_DAILY=True & gate=False -> with_commentary=False",
              calls[-1]["with_commentary"] is False)
    finally:
        config.PUBLIC_EXPORT_AUTO_REFRESH = orig_auto
        config.PUBLIC_EXPORT_COMMENTARY_DAILY = orig_daily
        config.PUBLIC_INSIGHT_BACKEND = orig_backend
        PE._build_from_live_data = orig_build
        PE.should_generate_commentary_today = orig_gate
        RO._update_commentary_failure_streak = orig_streak


def test_run_once_public_export_refresh_lmstudio_bypasses_gate():
    """★2026-08-19追加: PUBLIC_INSIGHT_BACKEND=="lmstudio"(既定)の間は、
    PUBLIC_EXPORT_COMMENTARY_DAILYやshould_generate_commentary_today()のゲート結果に
    関係なく常に with_commentary=True で呼ばれることを検証する(おにや08:57投稿②=
    無料のローカルLLMは毎回再生成してよい・1日1回ゲートは課金経路"claude"専用)。"""
    orig_auto = config.PUBLIC_EXPORT_AUTO_REFRESH
    orig_daily = config.PUBLIC_EXPORT_COMMENTARY_DAILY
    orig_backend = config.PUBLIC_INSIGHT_BACKEND
    orig_build = PE._build_from_live_data
    orig_gate = PE.should_generate_commentary_today
    # ★2026-08-20追加: 実データディレクトリへのストリーク状態ファイル書き込みを
    # 防ぐ(test_run_once_public_export_refresh_commentary_wiringと同じ理由)。
    orig_streak = RO._update_commentary_failure_streak
    RO._update_commentary_failure_streak = lambda succeeded: 0
    calls = []
    gate_calls = []
    PE._build_from_live_data = lambda **kw: (calls.append(kw) or {"generated_at": "x"})
    PE.should_generate_commentary_today = lambda *a, **kw: (gate_calls.append(1) or False)
    try:
        config.PUBLIC_EXPORT_AUTO_REFRESH = True
        config.PUBLIC_INSIGHT_BACKEND = "lmstudio"

        # COMMENTARY_DAILY=Trueでも、gateがFalse(今日生成済み)を返しても、lmstudioなら無視される
        config.PUBLIC_EXPORT_COMMENTARY_DAILY = True
        RO._run_public_export_refresh_step()
        check("pexp-lmstudio: gate function not consulted at all", len(gate_calls) == 0)
        check("pexp-lmstudio: with_commentary=True regardless of gate/daily flag",
              calls[-1]["with_commentary"] is True)

        # COMMENTARY_DAILY=Falseでもlmstudioなら関係なくTrue
        config.PUBLIC_EXPORT_COMMENTARY_DAILY = False
        RO._run_public_export_refresh_step()
        check("pexp-lmstudio: with_commentary=True even when COMMENTARY_DAILY=False",
              calls[-1]["with_commentary"] is True)
    finally:
        config.PUBLIC_EXPORT_AUTO_REFRESH = orig_auto
        config.PUBLIC_EXPORT_COMMENTARY_DAILY = orig_daily
        config.PUBLIC_INSIGHT_BACKEND = orig_backend
        PE._build_from_live_data = orig_build
        PE.should_generate_commentary_today = orig_gate
        RO._update_commentary_failure_streak = orig_streak


def test_run_once_public_export_refresh_runs_before_gsheets_sync():
    """run_once.main() のソース上で、公開エクスポート再生成step(13.5)がGoogle Sheets
    同期step(14)より前に呼ばれる順序が壊れていないことを検証する。main()自体を実行する
    と収集・LLM分析等の重い/ネットワーク依存stepまで走ってしまうため、他stepと同様
    副作用のないソース順序検査で確認する(=依存順序の回帰を軽量に捕捉)。"""
    import inspect
    src = inspect.getsource(RO.main)
    i_export = src.index("_run_public_export_refresh_step()")
    i_gsheets = src.index("_run_gsheets_sync_step()")
    check("order: public_export_refresh call present in main()", i_export >= 0)
    check("order: gsheets_sync call present in main()", i_gsheets >= 0)
    check("order: public_export_refresh runs BEFORE gsheets_sync in main()",
          i_export < i_gsheets)


def test_run_once_catchup_mode():
    """★2026-08-17追加・2026-08-19更新(おにや08:57投稿): catchup=True(高頻度バックログ
    解消+公開更新サイクル)のソース構造を検証する。main()自体を実行すると収集・LLM分析
    まで走ってしまうため、他stepと同様副作用のないソース検査で確認する。catchupは
    (a)analyzeを呼ぶ(collect-onlyとの違い=LLM分析を含む)(b)研究層はcatchup専用の
    分岐で明示的にスキップする(c)公開エクスポート/Sheets同期はフル実行と共通の
    コードパスで実行される(2026-08-19以前はここも早期returnで省いていたが、公開
    更新サイクルの10分化のため変更)、の3点が壊れていないことが要点。"""
    import inspect
    src = inspect.getsource(RO.main)
    i_catchup_param = src.index("catchup=False")
    i_analyze_call = src.index("analyze.analyze(time_budget_sec=")
    # "if collect_only:" は関数内に2箇所ある(①analyzeスキップ判定②snapshot後の早期
    # return)。①より後ろから検索して②(早期return)だけを確実に拾う。
    i_collect_only_return = src.index("if collect_only:\n", i_analyze_call)
    i_catchup_skip = src.index("if catchup:\n")
    # "_run_research_layer()" は関数内に2箇所現れる(①その少し上のコメントでの言及
    # ②実際の呼び出し)。①より後ろから検索して②(実呼び出し)だけを確実に拾う。
    i_research = src.index("_run_research_layer()", i_catchup_skip)
    i_export_call = src.index("_run_public_export_refresh_step()")
    check("catchup: main() accepts catchup parameter", i_catchup_param >= 0)
    check("catchup: analyze() call exists (LLM analysis is NOT skipped for catchup)",
          i_analyze_call >= 0)
    check("catchup: analyze() runs BEFORE the collect_only early-return gate",
          i_analyze_call < i_collect_only_return)
    # ★2026-08-19追加(おにや11:11投稿(a)対応): catchupにはフル実行と同じ40分予算ではなく
    # 短いCATCHUP_ANALYZE_TIME_BUDGET_SEC(既定4分)が明示的に渡されることを確認する
    # (高ボラ日にcatchup自体が10分枠を超過しIgnoreNewで後続トリガーが連鎖スキップされる
    # 問題[2026-08-19朝・47分間公開更新停止]への対策)。
    check("catchup: CATCHUP_ANALYZE_TIME_BUDGET_SEC is referenced for the catchup budget",
          "config.CATCHUP_ANALYZE_TIME_BUDGET_SEC if catchup else None" in src)
    # ★2026-08-19追加(ユーザー承認済み): catchupはnewest_first=catchup(=True)を渡し、
    # 新しい投稿を優先して短い予算でも直近の空気感を公開更新に反映する。
    check("catchup: newest_first=catchup is passed to analyze.analyze()",
          "newest_first=catchup" in src)
    check("catchup: dedicated 'if catchup:' branch exists to skip research layer",
          i_catchup_skip >= 0)
    check("catchup: that branch comes BEFORE the research layer call",
          i_catchup_skip < i_research)
    check("catchup: research layer call comes BEFORE the export/sheets block "
          "(so catchup's skip-branch and the shared export step are correctly ordered)",
          i_research < i_export_call)


def test_run_once_update_commentary_failure_streak_returns_streak():
    """★2026-08-21追加(実障害調査で発覚した回帰防止)。_update_commentary_failure_streak()
    には元々return文が無く、常にNoneを返していた(2026-08-20の新設時から)。呼び出し元
    (_run_public_export_refresh_step)がその戻り値を`streak >= 閾値`で比較するため、
    lmstudio backend(with_commentary=True常時)では毎run TypeErrorが発生し続けていた
    (public_export_refresh自体のtry/exceptで握りつぶされ、latest.json書込み自体は
    先に完了済みのため実害は無かったが、run.logへ無関係なERROR行が混入し、失敗
    ストリークのアラート自体も一度も機能していなかった)。この関数の戻り値が
    int(None でない)であることを直接検証し、同種の「returnし忘れ」の再発を防ぐ。"""
    import tempfile, os as _os
    orig_data_dir = config.DATA_DIR
    config.DATA_DIR = tempfile.mkdtemp()
    state_path = _os.path.join(config.DATA_DIR, "test_commentary_failure_state.json")
    orig_path = config.AI_COMMENTARY_FAILURE_STATE_PATH
    config.AI_COMMENTARY_FAILURE_STATE_PATH = state_path
    try:
        s1 = RO._update_commentary_failure_streak(succeeded=False)
        s2 = RO._update_commentary_failure_streak(succeeded=False)
        s3 = RO._update_commentary_failure_streak(succeeded=True)
        check("commentary-failure-streak: returns int, not None (2026-08-20新設時の"
              "returnし忘れバグの回帰防止)", isinstance(s1, int) and isinstance(s2, int))
        check("commentary-failure-streak: increments across failures", (s1, s2) == (1, 2))
        check("commentary-failure-streak: resets to 0 on success", s3 == 0)
        # 呼び出し元と同じ比較(streak >= threshold)がNoneTypeErrorを起こさないことも確認。
        check("commentary-failure-streak: threshold comparison does not raise TypeError",
              (s1 >= config.AI_COMMENTARY_FAILURE_WARN_THRESHOLD) in (True, False))
    finally:
        config.DATA_DIR = orig_data_dir
        config.AI_COMMENTARY_FAILURE_STATE_PATH = orig_path


def test_run_once_update_lock_busy_streak():
    """★2026-08-21追加(おにや提案・連携ログ2026-08-21 01:38投稿への対応)。
    _update_lock_busy_streak()(I/O部分)を一時ディレクトリで検証する。
    ロジック自体はtest_public_export_next_lock_busy_streakで検証済みのため、
    ここでは①状態ファイルへの読み書きが正しくラウンドトリップすること
    ②busyがLOCK_BUSY_WARN_THRESHOLD以上連続したときrun.logへERROR行が
    出ることの2点に絞る。"""
    import tempfile, os as _os
    orig_data_dir = config.DATA_DIR
    orig_threshold = config.LOCK_BUSY_WARN_THRESHOLD
    config.DATA_DIR = tempfile.mkdtemp()
    config.LOCK_BUSY_WARN_THRESHOLD = 3
    state_path = _os.path.join(config.DATA_DIR, "test_lock_busy_state.json")
    try:
        # busy×2回はまだ閾値未満のためストリークだけ進み、ERRORは出ない想定。
        logged = []
        orig_log = RO._log
        RO._log = lambda msg: logged.append(msg)
        try:
            s1 = RO._update_lock_busy_streak("analyze", state_path, was_busy=True)
            s2 = RO._update_lock_busy_streak("analyze", state_path, was_busy=True)
            s3 = RO._update_lock_busy_streak("analyze", state_path, was_busy=True)
        finally:
            RO._log = orig_log
        check("lock-busy-streak: increments across calls via persisted state file",
              (s1, s2, s3) == (1, 2, 3))
        check("lock-busy-streak: ERROR logged once streak reaches threshold(3)",
              any("ERROR analyze_lock busy 3 times in a row" in m for m in logged))
        check("lock-busy-streak: no ERROR logged for the sub-threshold calls",
              sum("ERROR" in m for m in logged) == 1)

        # 取得成功(busy=False)でストリークがリセットされることを確認。
        s4 = RO._update_lock_busy_streak("analyze", state_path, was_busy=False)
        check("lock-busy-streak: resets to 0 on successful lock acquisition",
              s4 == 0)
    finally:
        config.DATA_DIR = orig_data_dir
        config.LOCK_BUSY_WARN_THRESHOLD = orig_threshold


def test_run_once_analyze_lock():
    """★2026-08-17追加: フル実行とcatchupが同時にLLM分析しないための相互排他ロック
    (_acquire_analyze_lock/_release_analyze_lock)の直接検証。price_fetch.pyの
    _acquire_price_lockと全く同じ設計(排他生成・stale-lock強制解除・timeout時None)
    なので、同型のテスト(並行スレッドでの直列化+stale-lock強制解除)で検証する。"""
    import threading, time as _time, tempfile, os as _os
    orig_data_dir = config.DATA_DIR
    config.DATA_DIR = _os.path.join(tempfile.mkdtemp())
    try:
        max_concurrent = [0]
        active = [0]
        guard = threading.Lock()
        results = []

        def worker():
            lp = RO._acquire_analyze_lock(timeout_sec=5)
            with guard:
                active[0] += 1
                max_concurrent[0] = max(max_concurrent[0], active[0])
            _time.sleep(0.2)
            with guard:
                active[0] -= 1
            RO._release_analyze_lock(lp)
            results.append(lp is not None)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("analyze-lock: mutual exclusion holds (max_concurrent==1)", max_concurrent[0] == 1)
        check("analyze-lock: all 5 threads eventually acquired it", all(results))
        check("analyze-lock: no lock file left behind after release",
              not _os.path.exists(_os.path.join(config.DATA_DIR, ".analyze.lock")))

        # busy状態からのtimeout(None)とstale-lock強制解除の検証
        lock_path = _os.path.join(config.DATA_DIR, ".analyze.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("99999 stale\n")
        got_busy = RO._acquire_analyze_lock(timeout_sec=0.3)
        check("analyze-lock: busy (non-stale) lock -> None within short timeout", got_busy is None)
        old_time = _time.time() - (RO._ANALYZE_LOCK_STALE_SEC + 10)
        _os.utime(lock_path, (old_time, old_time))
        got_stale = RO._acquire_analyze_lock(timeout_sec=5)
        check("analyze-lock: stale lock force-cleared and reacquired", got_stale is not None)
        RO._release_analyze_lock(got_stale)
    finally:
        config.DATA_DIR = orig_data_dir


def test_run_once_parse_raw_rows_resilient():
    """★2026-08-19追加(おにや11:11投稿(c)対応): _parse_raw_rows_resilient()が、
    raw_comments.jsonlの一部行が壊れていても(torn concurrent writeを想定)、
    その行だけをスキップして残りの行は正常にparseすることを検証する
    (旧実装の `[json.loads(l) for l in ...]` は1行でも壊れていると例外で
    研究層のstep全体を丸ごと失敗させていた=おにやが実データで発見した
    JSONDecodeError('Extra data: line 1 column 2 (char 1)')の根本原因)。"""
    import json
    good1 = json.dumps({"id": "y1", "text": "a"}, ensure_ascii=False) + "\n"
    good2 = json.dumps({"id": "y2", "text": "b"}, ensure_ascii=False) + "\n"
    torn = "{\n"   # torn write を模す(書きかけの1行)
    rows = RO._parse_raw_rows_resilient([good1, torn, good2])
    check("parse-raw-resilient: 2 good rows parsed despite 1 torn line",
          len(rows) == 2 and rows[0]["id"] == "y1" and rows[1]["id"] == "y2")
    check("parse-raw-resilient: all-good input -> all rows parsed",
          len(RO._parse_raw_rows_resilient([good1, good2])) == 2)
    check("parse-raw-resilient: empty input -> empty list",
          RO._parse_raw_rows_resilient([]) == [])
    check("parse-raw-resilient: all-torn input -> empty list (no crash)",
          RO._parse_raw_rows_resilient([torn, "{{{\n"]) == [])


def test_run_once_export_lock():
    """★2026-08-19追加: 公開エクスポート+Sheets同期+AI考察生成を包む相互排他ロック
    (_acquire_export_lock/_release_export_lock)の直接検証。_acquire_analyze_lockと
    全く同じ設計(排他生成・stale-lock強制解除・timeout時None)なので同型のテストで
    検証する(並行スレッドでの直列化+stale-lock強制解除)。"""
    import threading, time as _time, tempfile, os as _os
    orig_data_dir = config.DATA_DIR
    config.DATA_DIR = _os.path.join(tempfile.mkdtemp())
    try:
        max_concurrent = [0]
        active = [0]
        guard = threading.Lock()
        results = []

        def worker():
            lp = RO._acquire_export_lock(timeout_sec=5)
            with guard:
                active[0] += 1
                max_concurrent[0] = max(max_concurrent[0], active[0])
            _time.sleep(0.2)
            with guard:
                active[0] -= 1
            RO._release_export_lock(lp)
            results.append(lp is not None)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("export-lock: mutual exclusion holds (max_concurrent==1)", max_concurrent[0] == 1)
        check("export-lock: all 5 threads eventually acquired it", all(results))
        check("export-lock: no lock file left behind after release",
              not _os.path.exists(_os.path.join(config.DATA_DIR, ".export.lock")))

        lock_path = _os.path.join(config.DATA_DIR, ".export.lock")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("99999 stale\n")
        got_busy = RO._acquire_export_lock(timeout_sec=0.3)
        check("export-lock: busy (non-stale) lock -> None within short timeout", got_busy is None)
        old_time = _time.time() - (RO._EXPORT_LOCK_STALE_SEC + 10)
        _os.utime(lock_path, (old_time, old_time))
        got_stale = RO._acquire_export_lock(timeout_sec=5)
        check("export-lock: stale lock force-cleared and reacquired", got_stale is not None)
        RO._release_export_lock(got_stale)
    finally:
        config.DATA_DIR = orig_data_dir


def test_run_once_catchup_runs_export_step():
    """★2026-08-19追加: main()のソース構造検証。catchup(10分毎)は研究層(step4-13)を
    スキップしつつも、公開エクスポート再生成(_run_public_export_refresh_step)と
    Sheets同期(_run_gsheets_sync_step)は(フル実行と共通の同じコードパスで)実行される
    ことを確認する(おにや08:57投稿③=公開更新サイクルの10分化)。main()自体を実行すると
    重い処理まで走るため、他stepと同型の副作用のないソース検査で確認する。"""
    import inspect
    src = inspect.getsource(RO.main)
    i_catchup_skip = src.index('_log("research_layer skipped (catchup:')
    i_export_lock = src.index("export_lock = _acquire_export_lock()")
    i_export_call = src.index("_run_public_export_refresh_step()")
    i_sheets_call = src.index("_run_gsheets_sync_step()")
    # "if collect_only:" は関数内に2箇所ある(①analyzeスキップ判定②snapshot後の早期
    # return)。①の直後から検索して②(早期return)だけを確実に拾う。
    i_collect_only_first = src.index("if collect_only:\n")
    i_collect_only_return = src.index("if collect_only:\n", i_collect_only_first + 1)
    check("catchup-export: catchup explicitly skips research layer (dedicated log line present)",
          i_catchup_skip >= 0)
    check("catchup-export: export lock acquisition present", i_export_lock >= 0)
    check("catchup-export: export lock comes BEFORE public_export_refresh call",
          i_export_lock < i_export_call)
    check("catchup-export: public_export_refresh comes BEFORE gsheets_sync",
          i_export_call < i_sheets_call)
    # collect_only(引数無しの単独early-return)だけがそれらより前でreturnする=
    # catchupはその早期returnを通らずexport/sheetsブロックへ到達する構造になっている
    # ことを、"collect_only or catchup"という複合条件が本文中に無いこと(2026-08-19の
    # リファクタで単独の"if collect_only:"に変わったこと)で確認する。
    check("catchup-export: early-return gate is collect_only-only (catchup no longer bypasses export)",
          "if collect_only or catchup:" not in src)
    check("catchup-export: collect_only early-return still present", i_collect_only_return >= 0)
    check("catchup-export: collect_only early-return comes BEFORE the export block",
          i_collect_only_return < i_export_lock)


# ============================================================================
# public_sheets_sync.py (Phase 2・Google Sheets同期) — 実ネットワークアクセスなし。
# gspread の型には依存しない自前フェイク(WorksheetNotFound という名前の例外クラス)で
# duck-typing 判定(public_sheets_sync._get_or_create_worksheet)を再現する。
# ============================================================================
class WorksheetNotFound(Exception):
    """フェイク用: gspread.exceptions.WorksheetNotFound と同じクラス名にすることで
    public_sheets_sync._get_or_create_worksheet の type(e).__name__ 判定に一致させる
    (実 gspread をimportせずに済む=selftestがgspread未インストール環境でも通る)。"""
    pass


class _FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.clear_calls = 0
        self.updates = []  # [{"values": [...], "range_name": "A1"}, ...]

    def clear(self):
        self.clear_calls += 1

    def update(self, values=None, range_name=None, **kw):
        self.updates.append({"values": values, "range_name": range_name})

    def get_all_values(self):
        # 最後に書かれた values をそのまま「読み戻し」として返す(簡易フェイク)。
        return self.updates[-1]["values"] if self.updates else []


class _FakeSpreadsheet:
    def __init__(self):
        self._sheets = {}

    def worksheet(self, title):
        if title not in self._sheets:
            raise WorksheetNotFound(title)
        return self._sheets[title]

    def add_worksheet(self, title, rows, cols, index=None):
        ws = _FakeWorksheet(title)
        self._sheets[title] = ws
        return ws


class _FakeClient:
    def __init__(self):
        self.sh = _FakeSpreadsheet()
        self.opened_keys = []

    def open_by_key(self, key):
        self.opened_keys.append(key)
        return self.sh


def _pss_sample_record(with_commentary=False, with_pss=True):
    rec = {
        "schema_version": "1.0",
        "symbol": config.SYMBOL,
        "company_name": "キオクシアホールディングス",
        "generated_at": "2026-08-16T15:00:00",
        "price": {"last": 4250.0, "change_pct": -0.8},
        "board": {"post_count_today": 250, "posts_per_hour": 20.5,
                  "bull_ratio": 0.55, "bear_ratio": 0.25, "neutral_ratio": 0.20,
                  "overheat_score": 62.3, "capitulation_score": 18.0},
        "trend_14d": [{"date": "2026-08-09", "post_count": 200,
                      "bear_ratio": 0.2, "bull_ratio": 0.5},
                     {"date": "2026-08-10", "post_count": 250,
                      "bear_ratio": 0.25, "bull_ratio": 0.55}],
        "price_sentiment_series": ([
            {"date": "2026-08-10", "price_close": 4250.0, "bull_ratio": 0.55, "bear_ratio": 0.25},
            {"date": "2026-08-09", "price_close": 4200.0, "bull_ratio": 0.50, "bear_ratio": 0.20},
        ] if with_pss else []),
        "disclaimer": PE.DISCLAIMER,
    }
    if with_commentary:
        rec["ai_commentary"] = {"text": "集計値に基づく客観的な考察文。",
                                "generated_at": "2026-08-16T15:05:00"}
    return rec


def test_public_sheets_sync_build_rows():
    """★2026-08-20: 旧latest/trend/commentaryタブ(Googleサイト向け)は廃止した
    (ユーザー指示・クラウドダッシュボードへの一本化)。以後はjson_blobタブのみ
    検証する。"""
    rec = _pss_sample_record(with_commentary=True)
    rec_no_pss = _pss_sample_record(with_pss=False)

    import json as _json
    blob = PSS.build_json_blob_values(rec)
    check("pss: json_blob is [[text, generated_at]] shape", len(blob) == 1 and len(blob[0]) == 2)
    check("pss: json_blob text round-trips to the exact record",
          _json.loads(blob[0][0]) == rec)
    check("pss: json_blob B1 = generated_at", blob[0][1] == rec["generated_at"])
    check("pss: json_blob works with no-pss record too (no crash)",
          _json.loads(PSS.build_json_blob_values(rec_no_pss)[0][0]) == rec_no_pss)
    check("pss: json_blob empty record -> empty text (no crash)",
          PSS.build_json_blob_values(None) == [["", ""]])
    check("pss: json_blob warns but does not fail on oversized text",
          len(PSS.build_json_blob_values({"generated_at": "x", "pad": "z" * 46000})[0][0]) > 45000)

    # ★2026-08-20緊急追加(実障害の再発防止): sentiment_last_24hをjson_blobへ含めると
    # Sheetsの1セル上限(50,000字)を超過し同期全体が壊れた(TAB_SENTIMENT_24H
    # docstring参照)。以後build_json_blob_valuesはこのキーを常に除外し、
    # build_sentiment_24h_valuesが専用タブ用に個別に組み立てることを検証する。
    rec_with_s24 = dict(rec)
    rec_with_s24["sentiment_last_24h"] = [
        {"time": "8/19 20:00", "bull_ratio": 0.4, "bear_ratio": 0.5, "post_count": 10}] * 144
    blob_s24 = PSS.build_json_blob_values(rec_with_s24)
    parsed_s24 = _json.loads(blob_s24[0][0])
    check("pss: json_blob excludes sentiment_last_24h even when present in record",
          "sentiment_last_24h" not in parsed_s24)
    check("pss: json_blob keeps all other keys intact when stripping sentiment_last_24h",
          parsed_s24 == rec)
    check("pss: build_json_blob_values does not mutate the caller's record",
          "sentiment_last_24h" in rec_with_s24)

    s24_blob = PSS.build_sentiment_24h_values(rec_with_s24)
    check("pss: sentiment_24h is [[text, generated_at]] shape",
          len(s24_blob) == 1 and len(s24_blob[0]) == 2)
    parsed_tab = _json.loads(s24_blob[0][0])
    check("pss: sentiment_24h tab contains the full 144-point series",
          len(parsed_tab["sentiment_last_24h"]) == 144)
    check("pss: sentiment_24h tab carries generated_at for staleness checks",
          parsed_tab["generated_at"] == rec["generated_at"])
    check("pss: sentiment_24h with no sentiment_last_24h in record -> empty list (no crash)",
          _json.loads(PSS.build_sentiment_24h_values(rec)[0][0])["sentiment_last_24h"] == [])
    check("pss: sentiment_24h None record -> no crash",
          _json.loads(PSS.build_sentiment_24h_values(None)[0][0])["sentiment_last_24h"] == [])


def test_public_sheets_sync_write_mocked():
    fake = _FakeClient()
    rec = _pss_sample_record(with_commentary=True)
    ok = PSS.write_to_sheets(rec, client=fake)
    check("pss-write: returns True on mocked success", ok is True)
    check("pss-write: opened correct spreadsheet id",
          fake.opened_keys == [config.GSHEETS_SPREADSHEET_ID])

    sh = fake.sh
    # ★2026-08-20緊急追加: sentiment_last_24hをjson_blobタブへ書くと実障害を
    # 起こしたため(TAB_SENTIMENT_24H docstring参照)、専用のsentiment_24hタブも
    # 毎回あわせて作成・書込みされることを検証する。
    check("pss-write: json_blob + sentiment_24h tabs created",
          set(sh._sheets.keys()) == {"json_blob", "sentiment_24h"})

    # ★2026-08-19追加(クラウドダッシュボード用データ橋渡し): json_blobタブは
    # 毎回A1へrecord全体のJSON文字列を書く(単一セル上書きなのでclear不要)。
    ws_json = sh._sheets["json_blob"]
    check("pss-write: json_blob tab written once at A1",
          len(ws_json.updates) == 1 and ws_json.updates[0]["range_name"] == "A1")
    json_written = ws_json.updates[0]["values"][0][0]
    check("pss-write: json_blob content round-trips to the exact record",
          __import__("json").loads(json_written) == rec)
    check("pss-write: json_blob B1 = generated_at",
          ws_json.updates[0]["values"][0][1] == rec["generated_at"])

    ws_sent = sh._sheets["sentiment_24h"]
    check("pss-write: sentiment_24h tab written once at A1",
          len(ws_sent.updates) == 1 and ws_sent.updates[0]["range_name"] == "A1")
    check("pss-write: sentiment_24h content (no sentiment in this fixture -> empty list)",
          __import__("json").loads(ws_sent.updates[0]["values"][0][0])["sentiment_last_24h"] == [])

    # 2回目の書き込み(別レコード)で内容が上書きされること
    rec2 = _pss_sample_record(with_commentary=True, with_pss=False)
    rec2["board"]["post_count_today"] = 999
    PSS.write_to_sheets(rec2, client=fake)
    check("pss-write: json_blob reflects 2nd record's new value",
          __import__("json").loads(ws_json.updates[-1]["values"][0][0])["board"]["post_count_today"] == 999)


def test_public_sheets_sync_fail_soft_missing_or_broken_latest():
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    orig_latest = config.PUBLIC_EXPORT_LATEST_PATH
    config.PUBLIC_EXPORT_LATEST_PATH = _os.path.join(d, "latest.json")
    # 密閉化: _log を本番 run.log でなく一時ファイルへ向ける(下の"simulated API failure"
    # 等のテスト文言が本番ログを汚さないため。2026-08-17: これが無かったため実際に
    # 本番run.logへ混入し、おにやが誤って本番障害と疑う事故が発生した=public_insight
    # 選テストで見つかった同型バグ)。
    _saved_log = config.LOG_PATH
    config.LOG_PATH = _os.path.join(d, "run.log")
    try:
        fake = _FakeClient()
        # 1) latest.json が存在しない
        ok1 = PSS.write_to_sheets(client=fake)
        check("pss-failsoft: missing latest.json -> False (no exception)", ok1 is False)
        check("pss-failsoft: missing latest.json -> client never touched (no API call)",
              fake.opened_keys == [])

        # 2) latest.json が壊れている(不正JSON)
        with open(config.PUBLIC_EXPORT_LATEST_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json..")
        ok2 = PSS.write_to_sheets(client=fake)
        check("pss-failsoft: broken latest.json -> False (no exception)", ok2 is False)
        check("pss-failsoft: broken latest.json -> client never touched",
              fake.opened_keys == [])

        # 3) client.open_by_key 自体が例外を投げる(認証/API失敗を模す)場合も fail-soft
        class _ExplodingClient:
            def open_by_key(self, key):
                raise RuntimeError("simulated API failure")

        ok3 = PSS.write_to_sheets(_pss_sample_record(), client=_ExplodingClient())
        check("pss-failsoft: API exception during sync -> False (no exception propagates)",
              ok3 is False)
    finally:
        config.PUBLIC_EXPORT_LATEST_PATH = orig_latest
        config.LOG_PATH = _saved_log


def test_public_sheets_sync_run_once_step_gate():
    """GSHEETS_SYNC_ENABLED(既定False)時、run_once._run_gsheets_sync_step() が
    public_sheets_sync.write_to_sheets() を一切呼ばずにスキップすること、
    Trueの時は呼ぶことを検証する(実ネットワークアクセスなし=write_to_sheetsをスタブ化)。"""
    orig_flag = config.GSHEETS_SYNC_ENABLED
    orig_write = PSS.write_to_sheets
    calls = []
    PSS.write_to_sheets = lambda *a, **kw: (calls.append((a, kw)) or True)
    try:
        config.GSHEETS_SYNC_ENABLED = False
        RO._run_gsheets_sync_step()
        check("pss-gate: GSHEETS_SYNC_ENABLED=False -> write_to_sheets NOT called",
              len(calls) == 0)

        config.GSHEETS_SYNC_ENABLED = True
        RO._run_gsheets_sync_step()
        check("pss-gate: GSHEETS_SYNC_ENABLED=True -> write_to_sheets called once",
              len(calls) == 1)
    finally:
        config.GSHEETS_SYNC_ENABLED = orig_flag
        PSS.write_to_sheets = orig_write


def test_eventstudy_pure():
    # to_jst 9h shift
    d_epoch = ES.to_jst(0)                       # 1970-01-01 09:00 JST
    check("es: epoch+9h", d_epoch.hour == 9 and d_epoch.year == 1970)
    d_str = ES.to_jst("2026-07-08T10:05:00")
    check("es: iso naive", d_str.hour == 10)
    # forward_return P(t0)=cutoff, no look-ahead
    bars = [{"close": 100}, {"close": 110}, {"close": 121}]
    check("es: forward_return", abs(ES.forward_return(bars, 0, 1) - 0.1) < 1e-9)
    check("es: forward_return OOR None", ES.forward_return(bars, 2, 1) is None)
    check("es: realized_vol positive", ES.realized_vol(bars, 0, 2) > 0)
    # ccf band + known lag: y lags x by 1 (y[t]=x[t-1]) => peak at k=+1
    x = [0, 1, 0, -1, 0, 1, 0, -1, 0, 1, 0, -1]
    y = [0] + x[:-1]
    c = ES.ccf(x, y, 3)
    check("es: ccf band ~1.96/sqrt(n)", abs(c["band"] - 1.96 / (len(x) ** 0.5)) < 1e-2)
    check("es: ccf peak at known lag+1", c["peak_lag"] == 1 and c["peak_r"] > 0.9)
    # autocorr flag on a trending series
    check("es: ccf autocorr flag on trend",
          ES.ccf([1, 2, 3, 4, 5, 6, 7, 8], [1, 2, 3, 4, 5, 6, 7, 8], 3)["autocorr_flag"] is True)
    # align 9h: epoch bar vs naive-JST feature must match same wall clock
    al = ES.align_series([("2026-07-08T09:05:00", 5.0)],
                         [(ES._epoch("2026-07-08 09:05"), 2.0)], freq="5m")
    check("es: align 9h-safe", len(al) == 1 and al[0][1] == 5.0 and al[0][2] == 2.0)
    # bootstrap CI + permutation
    lo_hi = ES.bootstrap_ci([0.1, 0.2, 0.15, 0.05, -0.1, 0.2, 0.0, 0.3], iters=500)
    check("es: bootstrap CI ordered", lo_hi[1] <= lo_hi[0] <= lo_hi[2])
    pt = ES.permutation_test([1, 2, 3, 4], [5, 6, 7, 8], iters=500)
    check("es: permutation p+effect", pt["p"] is not None and pt["effect_size"] is not None)
    bc = ES.bin_conditional([(0.1, 1), (0.2, -1), (0.9, 1), (0.95, 1)], [0.5])
    check("es: bin_conditional hit_rate", bc[-1]["hit_rate"] == 1.0)


def test_backtest_pnl():
    # matured OOS rows with known forward returns
    rows = [{"is_oos": "True", "vol_regime_score": "0.9", "range_day_score": "0.1",
             "forward_return_1d": "0.02", "date": "2026-07-09"},
            {"is_oos": "True", "vol_regime_score": "0.1", "range_day_score": "0.1",
             "forward_return_1d": "-0.03", "date": "2026-07-10"},
            {"is_oos": "False", "vol_regime_score": "0.5",
             "forward_return_1d": "0.5", "date": "2026-07-08"}]  # IS seed excluded
    base = BT.simulate_rule("baseline", rows)
    check("bt: is_oos_only excludes seed", base["n"] == 2)
    cand = BT.simulate_rule("vol_sized", rows)
    check("bt: vol_sized shrinks high-vol", cand["sizes"][0] < 1.0)
    kpi = BT.pnl_kpi(base)
    check("bt: kpi computes", kpi["n"] == 2 and kpi["cum_pnl"] is not None)
    cmp = BT.baseline_vs_candidate(base, cand, start_date="2026-07-08")
    check("bt: small-sample REJECT", "REJECT" in cmp["verdict"])
    check("bt: delta computed", cmp["delta_cum_pnl"] is not None)


def test_forward_oos_freeze_settle():
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    path = _os.path.join(d, "fwd.csv")
    # 密閉化: _log を本番 run.log でなく一時ファイルへ向ける(テストの price=100 等が本番ログを汚さない)
    _saved_log = config.LOG_PATH
    config.LOG_PATH = _os.path.join(d, "run.log")
    days = [f"2026-06-{x:02d}" for x in range(1, 8)]
    raw = _dense_raw(days) + _dense_raw(["2026-07-09"], posts_per_day=15)
    an = [_mk("2026-07-09T10:00:00", "bearish", "損切り")]
    price_daily = {"bars": [
        {"ts": ES._epoch_day("2026-07-09"), "close": 100.0, "volume": 1000},
        {"ts": ES._epoch_day("2026-07-10"), "close": 105.0, "volume": 1200},
        {"ts": ES._epoch_day("2026-07-14"), "close": 110.0, "volume": 1500},
    ]}
    sig = SG.compute_signals(an, raw_rows=raw, price_daily=price_daily, day="2026-07-09")
    sig["price"] = {"last": 100.0, "prev_close": 99.0, "change_pct": 1.0, "in_low_zone": False}
    row = FOOS.freeze_daily_row(sig, raw, price_daily, cutoff="close_consolidated", path=path)
    check("foos: is_oos true after harness start", row["is_oos"] == "True")
    # idempotent: second freeze same day doesn't duplicate
    FOOS.freeze_daily_row(sig, raw, price_daily, cutoff="close_consolidated", path=path)
    nrows = len(open(path, encoding="utf-8").read().strip().split("\n")) - 1
    check("foos: freeze idempotent (1 row)", nrows == 1)
    # settle T+1
    filled = FOOS.settle_matured_rows(price_daily, path=path)
    check("foos: settle fills matured", filled == 1)
    import csv as _csv
    rr = list(_csv.DictReader(open(path, encoding="utf-8")))[0]
    check("foos: forward_return_1d filled", rr["forward_return_1d"] != "")
    check("foos: forward_return_1d value", abs(float(rr["forward_return_1d"]) - 0.05) < 1e-6)
    config.LOG_PATH = _saved_log


# ============================================================================
# 日次記述統計台帳(descriptive_daily / write_descriptive_ledger・純記述のみ)
# ============================================================================
def test_descriptive_daily():
    raw = [
        {"ts": "2026-07-08T09:10:00", "text": "損切りして退場します", "source": "yahoo"},
        {"ts": "2026-07-08T09:20:00", "text": "決算に期待", "source": "yahoo"},
        {"ts": "2026-07-08T10:00:00", "text": "爆益🚀", "source": "yahoo"},
        {"ts": "2026-07-08T10:30:00", "text": "暴落だ逃げろ", "source": "5ch"},
        {"ts": "2026-07-08T11:00:00", "text": "様子見", "source": "5ch"},
        {"ts": "2026-07-08T12:00:00", "text": "$KXHCF adding", "source": "stocktwits"},
        {"ts": "2026-07-08T13:00:00", "text": "壊れ���", "source": "5ch"},  # 化け=除外
        {"ts": "2026-07-07T10:00:00", "text": "前日のコメント", "source": "yahoo"},
    ]
    analyzed = [
        {"ts": "2026-07-08T09:10:00", "meaningful": True, "sentiment": "bearish"},
        {"ts": "2026-07-08T09:20:00", "meaningful": True, "sentiment": "bullish"},
        {"ts": "2026-07-08T10:00:00", "meaningful": True, "sentiment": "bullish"},
        {"ts": "2026-07-08T10:30:00", "meaningful": False, "sentiment": "bullish"},  # 除外
    ]
    snaps = [{"date": "2026-07-08", "feel_vs_llm": {"agree_rate": 0.75}}]
    rows = RS.descriptive_daily(raw, analyzed, snaps)
    by = {r["date"]: r for r in rows}
    check("desc: two days sorted", sorted(by.keys()) == ["2026-07-07", "2026-07-08"])
    r8 = by["2026-07-08"]
    check("desc: raw_count excludes mojibake (6)", r8["raw_count"] == 6)
    check("desc: src yahoo=3", r8["src_yahoo"] == 3)
    check("desc: src 5ch=2 (mojibake dropped)", r8["src_5ch"] == 2)
    check("desc: src stocktwits=1", r8["src_stocktwits"] == 1)
    check("desc: analyzed_meaningful=3", r8["analyzed_meaningful"] == 3)
    check("desc: bull=2 bear=1", r8["bull_count"] == 2 and r8["bear_count"] == 1)
    check("desc: bull_ratio ~0.667", abs(r8["bull_ratio"] - round(2 / 3, 3)) < 1e-9)
    check("desc: cap_index>0", r8["cap_index"] > 0)
    check("desc: eup_index>0", r8["eup_index"] > 0)
    check("desc: aori_index>0", r8["aori_index"] > 0)
    check("desc: feel agree from snapshot", r8["feel_vs_llm_agree"] == 0.75)
    # 投稿6件<SIG_MIN_DAY_COVERAGE(10) → dense=False(既存 _is_today_dense 再利用)
    check("desc: not dense on small day", r8["dense"] is False)
    check("desc: posts_median numeric", isinstance(r8["posts_median"], float))
    # 前日はsnapshot無し → feel None
    check("desc: no snapshot -> feel None", by["2026-07-07"]["feel_vs_llm_agree"] is None)
    # 空入力でも落ちない
    check("desc: empty safe", RS.descriptive_daily([], [], []) == [])
    check("desc: none safe", RS.descriptive_daily([], None, None) == [])


def test_descriptive_ledger():
    import tempfile, os as _os, csv as _csv
    d = tempfile.mkdtemp()
    path = _os.path.join(d, "descriptive_285A.csv")

    def _row(date, raw_count, **kw):
        base = {"date": date, "raw_count": raw_count, "dense": False,
                "src_yahoo": raw_count, "src_5ch": 0, "src_stocktwits": 0,
                "src_reddit": 0, "src_other": 0, "cap_index": 0.0, "eup_index": 0.0,
                "aori_index": 0.0, "analyzed_meaningful": 0, "bull_count": 0,
                "bear_count": 0, "bull_ratio": None, "posts_median": 1.5,
                "feel_vs_llm_agree": None}
        base.update(kw)
        return base

    rows = [_row("2026-07-07", 5),
            _row("2026-07-08", 6, bull_count=2, bear_count=1, bull_ratio=0.667)]
    n1 = RS.write_descriptive_ledger(rows, path=path)
    check("ledger: first write adds 2", n1 == 2)
    n2 = RS.write_descriptive_ledger(rows, path=path)
    check("ledger: idempotent second write adds 0", n2 == 0)
    rows2 = rows + [_row("2026-07-09", 9)]
    n3 = RS.write_descriptive_ledger(rows2, path=path)
    check("ledger: only new date appended", n3 == 1)
    recs = list(_csv.DictReader(open(path, encoding="utf-8", newline="")))
    check("ledger: total 3 rows", len(recs) == 3)
    check("ledger: date-sorted",
          [r["date"] for r in recs] == ["2026-07-07", "2026-07-08", "2026-07-09"])
    check("ledger: header = DESCRIPTIVE_FIELDS", set(recs[0].keys()) == set(RS.DESCRIPTIVE_FIELDS))
    check("ledger: bool serialized False", recs[1]["dense"] == "False")
    check("ledger: None -> empty string", recs[0]["bull_ratio"] == "")
    check("ledger: value preserved", recs[1]["bull_count"] == "2")


def main():
    # 密閉化(2026-08-17・恒久策): 個別テスト関数ごとに config.LOG_PATH を退避する対症療法
    # (test_forward_oos_freeze_settle等)では、新しいテスト関数が追加されるたびに同じ穴を
    # 踏み直すいたちごっこになる(実際に本日、public_insight._run_selftests()・
    # test_public_sheets_sync_fail_soft_missing_or_broken_latest・
    # test_lmstudio_hybrid(ケース5)の3箇所で同型の本番run.log汚染事故が発生し、
    # おにやが2回にわたって「simulated ...」テスト文言を本番障害と誤認する事態を招いた)。
    # ここでmain()の実行区間全体を一括で一時ファイルへ退避することで、今後どのテスト
    # 関数が_log()を(直接・間接問わず)呼んでも本番run.logへは絶対に書き込まれない。
    import tempfile as _tempfile
    import os as _os
    _saved_log_path = config.LOG_PATH
    config.LOG_PATH = _os.path.join(_tempfile.mkdtemp(), "run.log")
    try:
        return _main_body()
    finally:
        config.LOG_PATH = _saved_log_path


def _main_body():
    for fn in [test_jsonl_window,
               test_parse, test_dedupe, test_date, test_sentiment_agg,
               test_spikes, test_topic_cluster_counts, test_today_rows,
               test_cluster_texts, test_json_extract, test_build_cluster_summaries,
               test_mojibake_and_charset, test_mojibake_cache,
               test_5ch_search, test_5ch_thread,
               test_5ch_thread_no_truncation_beyond_old_80_cap, test_reddit_parse,
               test_stocktwits_parse, test_yahoo_api_parse, test_feel_vs_llm,
               test_feel_to_sentiment_both_maps_neutral,
               test_interleave_by_source, test_iter_jsonl_torn_write_resilient,
               test_pending_raw_newest_first, test_dedupe_analyzed_by_id,
               test_source_breakdown, test_tokenizer_fallback,
               test_classify_lexicon, test_lmstudio_hybrid, test_analyze_time_budget_sec_param,
               test_price_parse, test_price_adr_pts_parse, test_sig_lexicon, test_sig_zscore,
               test_sig_other_symbols,
               test_sig_resolved_sentiment, test_sig_ratios_votes_hourly_feel_priority,
               test_sig_votes, test_sig_named,
               test_sig_named_user_fallback,
               test_sig_hourly, test_sig_lowzone, test_sig_gauges,
               test_sig_cap_shock_and_price_freeze_guard,
               test_sig_compute_and_cards, test_sig_raw_vs_analyzed_split,
               test_eng_dense_and_calib, test_eng_robust_z_pctrank,
               test_eng_bvp_and_direction, test_eng_confidence_and_regime,
               test_eng_export_record_schema,
               test_eng_state_dense_honest,
               test_eng_export_state_and_range_dense_honest,
               test_eng_data_health_wiring, test_export_atomic_and_append,
               test_public_export_build_record, test_public_export_load_regime_readonly,
               test_public_export_trend_from_snapshots,
               test_public_export_extended_hours_summary,
               test_public_export_price_sentiment_series,
               test_public_export_intraday_today_series,
               test_public_export_adr_pts_price_fallback,
               test_public_export_live_price_staleness_and_trading_hours,
               test_public_export_market_session_label,
               test_public_export_next_commentary_failure_streak,
               test_public_export_next_lock_busy_streak,
               test_public_export_board_score_daily_series,
               test_public_export_signal_cards_daily_series,
               test_news_fetch_pure_functions,
               test_news_fetch_collect_news_io,
               test_public_export_signal_state_changes,
               test_public_export_previous_deltas,
               test_public_export_sentiment_last_24h_10min,
               test_public_dashboard_today_time_buckets,
               test_public_dashboard_today_time_buckets_60s,
               test_public_export_kabu_tick_today_summary,
               test_public_export_detect_series_outliers,
               test_public_export_board_totals_60s_series,
               test_public_export_board_totals_60s_series_itayose_shape_detection,
               test_public_export_intraday_today_high_low,
               test_public_export_intraday_today_sentiment_10min,
               test_public_export_sentiment_today_from_last_24h,
               test_public_export_prev_close_from_price_sentiment_series,
               test_public_export_parse_public_json_csv,
               test_public_export_parse_visit_counter_response,
               test_public_export_validate_no_leak,
               test_public_export_write_atomic_and_leak_abort,
               test_public_export_commentary_daily_gate,
               test_run_once_public_export_refresh_step_gate,
               test_run_once_public_export_refresh_commentary_wiring,
               test_run_once_public_export_refresh_lmstudio_bypasses_gate,
               test_run_once_export_lock, test_run_once_catchup_runs_export_step,
               test_run_once_parse_raw_rows_resilient,
               test_run_once_public_export_refresh_runs_before_gsheets_sync,
               test_run_once_catchup_mode, test_run_once_analyze_lock,
               test_run_once_update_commentary_failure_streak_returns_streak,
               test_run_once_update_lock_busy_streak,
               test_public_sheets_sync_build_rows,
               test_public_sheets_sync_write_mocked,
               test_public_sheets_sync_fail_soft_missing_or_broken_latest,
               test_public_sheets_sync_run_once_step_gate,
               test_eventstudy_pure, test_backtest_pnl,
               test_forward_oos_freeze_settle,
               test_descriptive_daily, test_descriptive_ledger]:
        fn()
    pillar_fails = _run_pillar_selftests()
    print("-" * 40)
    if FAILS or pillar_fails:
        print(f"FAILED core={len(FAILS)} pillars={pillar_fails}: {FAILS}")
        return 1
    print("ALL GREEN (core + 4柱 + backtest + vol_eval + intraday_linkage + retail_chase + moomoo + collect_intl)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
