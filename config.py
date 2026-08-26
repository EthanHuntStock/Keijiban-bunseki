# -*- coding: utf-8 -*-
"""
config.py - 掲示板センチメント監視モジュールの設定（ポータブル）

規律:
  - 発注は絶対にしない。独立モニタリングツール／将来トレPJのシグナル候補。
  - 既存トレPJの台帳/コードは触らない。完全に別フォルダ・別台帳。
  - パスは全て __file__ から相対導出。固有パス(C:\\Users\\...)を書かない。
  - 生データは消さない(append only)。
"""
import os

# ---- ポータブルなパス導出 ----------------------------------------------------
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
# ---- 生データ(掲示板の全投稿・分析済み等=大容量・再取得不能)の保管先 ----
#   ★2026-07-18 ユーザー決定=大容量データ方針(tick側と同じ)に統一。
#   Dropboxから外し、ローカル C:\AI用フォルダ に置く(+外付けHD D:\ へバックアップ)。
#   理由=(1)日次で増える再取得不能データ (2)他PCで見ない=同期不要 (3)世代管理不要。
#   env `BBS_DATA_ROOT` で上書き可(既定は艦隊共通の C:\AI用フォルダ・JQ_TICK_DIRと同型)。
#   ※ research/_ledger(集計台帳)は小容量ゆえ Dropbox 維持(台帳=Dropbox方針)。
DATA_DIR = os.environ.get("BBS_DATA_ROOT") or r"C:\AI用フォルダ\おにや式投資法\data"

RAW_COMMENTS_PATH = os.path.join(DATA_DIR, "raw_comments.jsonl")
SEEN_IDS_PATH     = os.path.join(DATA_DIR, "seen_ids.json")
ANALYZED_PATH     = os.path.join(DATA_DIR, "analyzed.jsonl")
SNAPSHOTS_PATH    = os.path.join(DATA_DIR, "snapshots.jsonl")
CLUSTERS_PATH     = os.path.join(DATA_DIR, "clusters.jsonl")
LOG_PATH          = os.path.join(DATA_DIR, "run.log")
PRICE_DAILY_PATH    = os.path.join(DATA_DIR, "price_daily.json")
PRICE_INTRADAY_PATH = os.path.join(DATA_DIR, "price_intraday.json")
PRICE_1M_PATH       = os.path.join(DATA_DIR, "price_1m.json")

# ---- PTS(私設取引システム=夜間取引)・米国ADR円換算(price_fetch.fetch_adr_pts_and_save) ----
#   ★2026-08-19追加(ユーザー依頼: 「AI分析はPTS・米国ADRの時間帯もそれらの値を分析する
#   ように。翌日の傾向につながる可能性がある」)。実測で確認済み: Yahoo Finance API
#   (query1.finance.yahoo.com、285A.Tのmeta.hasPrePostMarketData=false)ではPTS/ADRを
#   一切提供していないため、ユーザー提示の外部サイト nikkei225jp.com のADR/PTS専用
#   JSONフィード(TSE現在値・PTS株価・ADR円換算・ADR USD の4本値を5分間隔程度で保持)を
#   別ソースとして新規に取得する。Yahoo Finance API(285A.T)とは完全に独立したドメイン・
#   別ファイルのため、失敗してもYahoo側の日足/日中足取得には一切影響しない(fail-soft)。
ADR_PTS_URL      = "https://nikkei225jp.com/_data/_nfsDATA/adr/{symbol}.json"
ADR_PTS_REFERER  = "https://nikkei225jp.com/adr/adr.php?a={symbol}"
ADR_PTS_PATH     = os.path.join(DATA_DIR, "adr_pts.json")

# ---- 点時刻票(point-in-time votes)スナップショット(votes_snapshot.py) --------
#   raw_comments の votes_yes/no は初回取得時点の累積値のみ=先読み疑い対策として
#   毎収集で「パース済み全件(既読含む)」の票を差分記録する台帳。記録のみ(受動)=既定ON。
#   '0' で完全停止(収集本体は不変)。
VOTES_SNAPSHOT_PATH = os.path.join(DATA_DIR, "votes_snapshot.jsonl")
VOTES_STATE_PATH    = os.path.join(DATA_DIR, "votes_state.json")
BBS_VOTES_PIT = os.environ.get("BBS_VOTES_PIT", "1") == "1"


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ---- 対象銘柄 ----------------------------------------------------------------
SYMBOL = "285A"
SYMBOL_NAMES = ["285A", "キオクシア", "Kioxia", "KIOXIA"]

# ---- 共通HTTP ---------------------------------------------------------------
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
HTTP_TIMEOUT = 20
POLITE_DELAY_SEC = 2.0   # ソース内で複数リクエストする時の間隔(ToS配慮)

# ---- ソース1: Yahoo掲示板 ---------------------------------------------------
YAHOO_CODE = SYMBOL                              # bff APIの code パラメータ("285A")
YAHOO_FORUM_URL = f"https://finance.yahoo.co.jp/quote/{SYMBOL}.T/forum"  # canonical
YAHOO_BBS_URL = f"https://finance.yahoo.co.jp/quote/{SYMBOL}.T/bbs"      # HTMLフォールバック
# 内部JSON API(深いページング取得)。要 x-jwt-token + forumで得たセッションcookie。
YAHOO_BFF_URL = "https://finance.yahoo.co.jp/bff-quote-stocks/v1/ajax/bbs/comment"
YAHOO_PAGE_SIZE = 100     # 1ページ取得件数(実測OK)
# 遡りページ上限=暴走防止のバックストップ。正常な停止経路は「前回既読(seen_id)到達」。
# 実測: 暴落日は~860投稿/h=1日1万〜1.5万件。3h間隔でも2600件/回、初回フルバックフィルは
# 1万件級。10(=1000件)では頭打ちして取りこぼす(実測350件連続欠落)。200=最大2万件/runへ。
# page上限に達した場合は「まだ溜まっている」兆候として警告ログを出す。
YAHOO_MAX_PAGES = 200     # 20,000件/run 安全上限(通常はseen到達で早期停止)
YAHOO_PAGE_DELAY_SEC = 0.7  # ページ間sleep(過剰アクセス回避)
FETCH_PAGES = 1          # (旧HTML方式の名残・フォールバックで最新~72件)

# ---- ソース2: 5ch(株/雑談スレ) --------------------------------------------
FIND_5CH_URL = "https://find.5ch.net/search?q="   # + urlencoded keyword
BBS_5CH_KEYWORDS = ["キオクシア", "285A"]          # スレ検索キーワード
BBS_5CH_TITLE_FILTER = ["キオクシア", "285A", "KIOXIA", "Kioxia", "kioxia"]
BBS_5CH_MAX_THREADS = 4             # 1実行で取りに行くスレ数(負荷配慮)
BBS_5CH_MAX_POSTS_PER_THREAD = 1000  # スレあたり取得レス上限(=5ch自体の1スレ上限)。
                                      # 旧80は活況スレ×長時間停止の組合せで末尾80件の外側が
                                      # 構造的に永久欠落するリスクがあった(2026-08-21おにや発見)。
                                      # read.cgiは元々スレ全体のHTMLを一括取得しておりページング
                                      # 不要・重複はseen_ids側で排除されるため上限を実質無効化
                                      # しても再取得コストは増えない。

# ---- ソース3: Reddit(公開JSON・認証不要/IP制限あり) ----------------------
REDDIT_SEARCH_URL = "https://www.reddit.com/search.json?q=Kioxia&sort=new&limit=25"
REDDIT_UA = "windows:bbs-sentiment-monitor:0.2 (personal research, read-only)"

# application-only OAuth (client_credentials): ユーザーログイン不要の公開データ読取用。
# creds は必ず env 経由(ファイル/log/print に secret を絶対出さない)。未設定なら None ->
# collect_reddit() は従来の公開JSONへフォールバック(後方互換)。
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_OAUTH_TOKEN_URL  = "https://www.reddit.com/api/v1/access_token"
# oauth.reddit.com 側の検索(公開 search.json と同一 Listing 構造を返す)。
REDDIT_OAUTH_SEARCH_URL = "https://oauth.reddit.com/search?q=Kioxia&sort=new&limit=100"

# ---- ソース4: StockTwits(公開JSON・認証不要) ------------------------------
STOCKTWITS_SYMBOLS = ["KXHCF", "KXIAY"]  # Kioxia OTC / ADR
STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"

# ---- Claudeモデル ------------------------------------------------------------
# 大量の一次分類(意味判定+センチメント)はコスト重視でHaiku。
HAIKU_MODEL = "claude-haiku-4-5"
# クラスタ代表ラベル付け等の少数回はOpus。
OPUS_MODEL  = "claude-opus-4-8"

# 分類(意味判定+センチメント)のrun毎上限。生データは全件取るが、判定はサンプル
# (interleave_by_sourceで偏り防止)。強弱比には十分な代表標本。
# ★2026-07-23 修正: 上限400はLLM(Haiku)課金対策の値。だが既定は辞書モード(BBS_USE_LLM=0
#   =無料)で、Yahoo流入は1日1.3万〜4万件あり容量400×13run=5,200/日では溢れてバックログ化
#   していた(07-15以降Yahoo分析が事実上停止・後日195k滞留を一括バックフィルで解消)。
#   → 課金のあるLLMモードのみ400に絞り、無料の辞書モードは3000(×13run=最大3.9万/日=流入を吸収)。
# ★2026-08-11 追加: ローカルLLM(ollama/lemonade)は課金が発生しないため、400キャップの
#   対象外とし、辞書モードと同じ3000を使う。課金が発生するのはclaude経路のみ。
#   BBS_LLM_BACKEND未設定時はBBS_USE_LLMから後方互換で導出(下のBBS_LLM_BACKEND定義と同じ式)。
# ★2026-08-17 追加: lmstudioを本番切替した直後、上記ロジックのまま(3000件枠)だと
#   実測検証済みのn=400を大幅に超える規模(曖昧2,000件超相当)で未検証のまま走ってしまう
#   重大な見落としが判明した(CROSS_PROJECT_LOG参照。おにやが`BBS_MAX_COMMENTS=400`の
#   環境変数で応急処置済み)。lmstudioは"課金なし"という点ではollama/lemonadeと同じだが、
#   コンテキスト長制約からくる件数上限がある点で性質が異なるため、独自の安全な上限を持つ。
#   環境変数 BBS_LMSTUDIO_MAX_COMMENTS で上書き可能(実データでの追加検証後に引き上げる想定)。
# ★2026-08-17 追加(2回目): 本番投入直後、投稿急増日(Yahoo実測1,300件超/時)に対し固定400が
#   構造的に小さすぎ、Yahoo分析が約4.5時間分バックログ化する事態が発生した(おにや17:17投稿・
#   CROSS_PROJECT_LOG参照)。固定件数でなく、**LLM処理に割り当てる時間予算から逆算する動的値**
#   へ変更(チャンク分割を固定件数からトークン予算ベースへ変更したのと同じ考え方)。
#   実測(n=400・272/273件成功・1,167.8秒)から sec/item=4.29秒・曖昧比率=68.25%(272/400程度)
#   と判明したが、曖昧比率は日によって56.7%〜80%とブレるため、**安全側(最もLLM負荷が高い側)
#   の80%を前提**に算出する。時間予算(既定40分)は1時間サイクルの残り20分を、収集/価格取得/
#   snapshot/研究層/Sheets同期等の他ステップ用に確保する意図。
#   環境変数で個別に上書き可能(BBS_LMSTUDIO_TIME_BUDGET_SEC / BBS_LMSTUDIO_SEC_PER_ITEM /
#   BBS_LMSTUDIO_AMBIGUOUS_RATIO)。BBS_LMSTUDIO_MAX_COMMENTS を明示指定した場合はそちらが
#   最優先(動的算出をスキップ)。
# ★2026-08-19 15:xx 修正(おにや11:11投稿・14:49投稿で2回目再発を実測・ユーザー指摘
#   「対症療法でなく構造設計を見直すべき」を受けた根本対応): 既定を40分から15分へ
#   大幅短縮する。実測(2026-08-19)で、フル実行(catchupタグなし)が.analyze.lockを
#   40分規模で占有し続け、その間の10分毎catchupトリガーが4〜5回連続で
#   `WARN analyze skipped(lock busy)`と空振りし、公開更新が41〜47分完全停止する
#   事象が同日2回発生した(10:20-11:07・14:00-14:41)。これは「バックログ解消の
#   主力を毎時フル実行の長時間analyzeに委ねる」という当初設計そのものが、
#   10分間隔のcatchup(newest_first・2026-08-19新設)と役割衝突する構造的欠陥
#   だったため、**役割を明確に再分担**する: フル実行は「短く抑えたanalyze(15分)+
#   研究層(regime/export/descriptive等・こちらは元々LLM非依存で長時間化しない)」を
#   1時間に1回・catchupは「短時間analyze(4分・newest_first)+公開更新」を10分毎、
#   という頻度非対称だが両者とも"短い"分担にする。バックログ解消は両者の複数run
#   跨ぎの積み重ねで進む設計(単発runでの一括解消を狙わない)。
#   worst-caseのロック占有時間が40分→15分になることで、catchupが空振りする
#   最大回数も4〜5回→最大1〜2回に抑えられ、公開更新の停止も同程度に短縮される。
LMSTUDIO_ANALYZE_TIME_BUDGET_SEC = int(os.environ.get("BBS_LMSTUDIO_TIME_BUDGET_SEC", "900"))  # 15分
# ★2026-08-19追加(おにや11:11投稿(a)対応): catchup(10分間隔)専用のanalyze予算。
#   毎時フル実行と同じ40分予算のままだと、高ボラ日にanalyze単体で10分間隔の枠を
#   何周も超過し、タスクスケジューラのIgnoreNewで後続トリガーが連鎖スキップされる
#   問題が実測された(2026-08-19朝・47分間公開更新停止)。catchupはこの短い予算を
#   analyze.analyze(time_budget_sec=...)へ明示的に渡すことで1サイクルの上限を保ち、
#   残り時間を確実にexport/AI考察/Sheets同期(公開更新の鮮度=catchupの主目的)に回す。
#   既定4分=10分の枠のうち収集/価格/スナップショット(~1分)を除いた残り約9分の
#   半分弱を確保し、export/insight/sheets(実測で合計2〜4分程度)に十分な余白を残す。
CATCHUP_ANALYZE_TIME_BUDGET_SEC = int(os.environ.get("BBS_CATCHUP_ANALYZE_TIME_BUDGET_SEC", "240"))  # 4分
LMSTUDIO_SEC_PER_AMBIGUOUS_ITEM = float(os.environ.get("BBS_LMSTUDIO_SEC_PER_ITEM", "4.3"))
LMSTUDIO_AMBIGUOUS_RATIO_ESTIMATE = float(os.environ.get("BBS_LMSTUDIO_AMBIGUOUS_RATIO", "0.80"))
# 2026-08-17: `if _env_x is not None` から `if _env_x:` へ変更(おにや17:38投稿の指摘対応)。
#   [Environment]::SetEnvironmentVariable(name, $null, "User") で「削除」したつもりでも
#   レジストリ上は REG_SZ の空文字列として残るケースが実際にあった(おにやが実測発見)。
#   空文字列は is not None では真になり int("") で ValueError → run全体がクラッシュしうる。
#   truthy判定なら空文字列は「未設定」として自動判定側にフォールバックし安全。
# ★2026-08-17 22:xx 方針転換(ユーザー承認済み・CROSS_PROJECT_LOG参照): 上記の時間予算からの
#   逆算値は「1件あたりの平均所要時間」という"仮定"に依存し外れると総所要時間が予算を超過する
#   問題があった(21:00 run実測)ため、analyze.py側の analyze_batch_with_lmstudio_hybrid() に
#   壁時計デッドライン(LMSTUDIO_ANALYZE_TIME_BUDGET_SECを直接参照)を実装済み。実際の打ち切りは
#   そちらが担うため、ここでの件数上限はもはや「安全弁」ではなく「他の全ステップ(collect/
#   snapshot/研究層等)より先に候補を絞り込み過ぎて、大きな残債を1回のrunで十分消化できない」
#   というボトルネックになっていた(実測: 697件/runでは残債5,950件の解消に9回runが必要=
#   半日以上かかる計算。流入は実測390〜440件/時のみで697自体は十分な量だったにもかかわらず、
#   この事前上限が残債消化を遅らせていた)。よって既定値を大幅に引き上げ、実質的には
#   デッドラインだけが打ち切りを制御するようにする(下限100は維持=異常env設定でのゼロ化防止)。
_env_lmstudio_max = os.environ.get("BBS_LMSTUDIO_MAX_COMMENTS")
if _env_lmstudio_max:
    LMSTUDIO_MAX_COMMENTS_PER_RUN = int(_env_lmstudio_max)
else:
    LMSTUDIO_MAX_COMMENTS_PER_RUN = max(100, int(os.environ.get("BBS_LMSTUDIO_MAX_COMMENTS_CEILING", "20000")))
# 環境変数 BBS_MAX_COMMENTS で上書き可能(例: 検証=30, 全件=0)。全バックエンド共通の
# 最終上書き手段として、以下の自動判定ロジックより優先される。
_env_max = os.environ.get("BBS_MAX_COMMENTS")
if _env_max:
    MAX_COMMENTS_PER_RUN = int(_env_max) or None   # 空文字は上と同型の理由で未設定扱い
else:
    _backend_for_cap = os.environ.get("BBS_LLM_BACKEND") or (
        "claude" if os.environ.get("BBS_USE_LLM", "0") == "1" else "lexicon"
    )
    if _backend_for_cap == "claude":
        MAX_COMMENTS_PER_RUN = 400
    elif _backend_for_cap == "lmstudio":
        MAX_COMMENTS_PER_RUN = LMSTUDIO_MAX_COMMENTS_PER_RUN
    else:
        MAX_COMMENTS_PER_RUN = 3000

# ★2026-08-17 追加: run_once.pyのstep4〜13(研究層=descriptive/cluster_trend/euphoria系の
#   別台帳群、いずれも失敗分離済みだがLLM呼び出しは含まない)を、当runの経過時間が既にこの秒数
#   を超えていたら見送る安全弁。おにや17:17投稿の増幅要因(runが61分かかり次の毎時起動が
#   IgnoreNewでまるごとスキップされ機会損失が加速)への対策。研究層は別台帳への追記のみで
#   当runで書けなくても次run以降に取り戻せるため、スキップしても収集/分析本体(step1-3)や
#   公開用latest.json再生成・Sheets同期(step13.5/14・軽量なので常に実行)には影響しない。
#   ★2026-08-17 21:xx 実測により初期値45分(2700)を30分(1800)へ下方修正。19:00開始runの
#   実測で判明=analyze自体がLMSTUDIO_ANALYZE_TIME_BUDGET_SEC(40分)予算をほぼ使い切って
#   39分で完了 → ゲート判定時点で elapsed≈40分<45分ゆえ研究層が起動 → 研究層自体にも
#   時間上限が無く実測30分かかり、run全体は19:00→20:10の70分に達し、結局20:00枠は
#   IgnoreNewでスキップされた(このゲート単体では防げなかった)。分析予算(40分)に対し
#   ゲートの余裕が5分しかなく、研究層自身の所要時間(実測30分)を吸収できていなかったのが
#   原因。ゲートを30分に下げることで、analyzeが予算を使い切る重い日は研究層を確実に
#   見送って総所要を~40分程度に抑え、次の毎時起動を守る(軽い日はanalyzeが速く終わり
#   elapsed<30分のまま研究層も通常通り走る=データ量に応じて自然にスケールする設計は
#   維持)。
RESEARCH_LAYER_TIME_LIMIT_SEC = int(os.environ.get("BBS_RESEARCH_LAYER_TIME_LIMIT_SEC", "1800"))  # 30分

# Haikuの1バッチに載せるコメント数(JSON安定のため中庸に)
ANALYZE_BATCH_SIZE = 10
HAIKU_MAX_TOKENS = 3000
OPUS_MAX_TOKENS = 400

# ---- LLM 使用ゲート(非LLM代替が既定・API を一切呼ばない運用) ----------------
# 既定 '0' = Anthropic API を一切呼ばない。強弱分類は辞書(analyze.classify_lexicon)、
# クラスタ見出しは TF-IDF(analyze.label_from_tfidf)、引け後考察はテンプレ
# (insight.render_template_insight)で代替する。'1' で従来 LLM 経路に戻せる(後方互換)。
BBS_USE_LLM = os.environ.get("BBS_USE_LLM", "0") == "1"

# ---- ローカルLLM連携(Ollama/Lemonade Server) --------------------------------
# ★2026-08-11 追加。既存の辞書モード(BBS_USE_LLM=0)・Claude経路(BBS_USE_LLM=1)は
# 一切変更しない(後方互換)。新しいバックエンドは別の環境変数 BBS_LLM_BACKEND で選ぶ。
#   "lexicon"(既定・API非依存) | "claude"(従来のBBS_USE_LLM=1経路) | "ollama" | "lemonade"
#   | "lmstudio"(★2026-08-16追加・辞書プレフィルタ+LM Studioハイブリッド。BBS_LLM_BACKEND=
#     "lmstudio" を明示指定した場合のみ有効・既定は変わらず "lexicon")
# BBS_LLM_BACKEND 未設定時は、従来どおり BBS_USE_LLM から後方互換で導出する
# (BBS_USE_LLM=1 なら "claude"、それ以外は "lexicon")。
_BBS_LLM_BACKEND_CHOICES = ("lexicon", "claude", "ollama", "lemonade", "lmstudio")
BBS_LLM_BACKEND = os.environ.get("BBS_LLM_BACKEND") or ("claude" if BBS_USE_LLM else "lexicon")
if BBS_LLM_BACKEND not in _BBS_LLM_BACKEND_CHOICES:
    # fail-soft: 不正値は辞書モードへ(analyze.py 側で例外を出さないため)
    BBS_LLM_BACKEND = "lexicon"

# ローカルLLMサーバーのエンドポイント(OpenAI互換API・家PC1で常駐稼働中を前提)。
# base_url/modelともに環境変数で上書き可(モデル名は ollama list / lemonade list で要確認)。
LOCAL_LLM_ENDPOINTS = {
    # ★2026-08-11: 家PC1(A9MAX)実機で `ollama list` を実測し、実際に
    #   pull済みのモデル名に合わせた(qwen2.5:7b-instructは未pull=存在しなかった)。
    "ollama": {
        "base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:14b"),
    },
    # ★2026-08-11: 家PC1実機で `lemonade list` を実測。qwen2.5-7b-instruct(小文字)は
    #   一覧に存在せず、"Qwen2.5-7B-Instruct-Hybrid"(AMD NPU+iGPU Hybrid実行・8.65GB)が
    #   正しいモデル名だった。`lemonade pull Qwen2.5-7B-Instruct-Hybrid` で取得要。
    "lemonade": {
        "base_url": os.environ.get("LEMONADE_BASE_URL", "http://localhost:13305/v1"),
        "model": os.environ.get("LEMONADE_MODEL", "Qwen2.5-7B-Instruct-Hybrid"),
    },
    # ★2026-08-16 追加: LM Studio(OpenAI互換API)。辞書プレフィルタ+簡素化プロンプトの
    #   ハイブリッド経路(analyze.analyze_batch_with_lmstudio_hybrid)専用エンドポイント。
    #   実測: gemma-4-12b-it で meaningful/sentimentのみの簡素化プロンプトなら
    #   10件バッチ25.6〜29.8秒(フル項目版88〜94秒の約1/3)。
    "lmstudio": {
        "base_url": os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        "model": os.environ.get("LMSTUDIO_MODEL", "gemma-4-12b-it"),
    },
}
# ローカルLLM呼び出しのタイムアウト秒(7B級・CPU/内蔵GPU実行を想定しやや長め)。
LOCAL_LLM_TIMEOUT_SEC = int(os.environ.get("BBS_LOCAL_LLM_TIMEOUT", "120"))
# ★2026-08-17 追加: lmstudioハイブリッド(analyze_batch_with_lmstudio_hybrid)専用タイムアウト。
#   曖昧アイテム全件を1回のバッチ呼び出しに集約する設計のため、既定の120秒(他バックエンド用)
#   では実運用規模(n=100・曖昧58件)で実測229.1秒かかり確実にタイムアウトすると判明した
#   (2026-08-17 実測・CROSS_PROJECT_LOG参照)。n=400級(曖昧最大320件程度)でも実測スループット
#   (3.95秒/件)からの外挿で最大21分程度に収まる見込みのため、安全マージンを見て30分とする。
LMSTUDIO_TIMEOUT_SEC = int(os.environ.get("BBS_LMSTUDIO_TIMEOUT", "1800"))
# ★2026-08-17 追加: analyze_batch_with_lmstudio_hybrid の内部チャンク分割。
#   曖昧アイテム全件を1回のバッチ呼び出しに集約する設計だと、n=400級(曖昧268件・
#   プロンプト長14,893文字≒約9,650トークン)でLM Studio側のコンテキスト長上限
#   (実測環境では8192トークン)を超過しHTTP 400になる(エラー本文:
#   "n_keep: 9650 >= n_ctx: 8192")。
#   ★当初は固定150件を既定にしたが、おにやの実データ再検証(12:35投稿)で
#   150件でも投稿文の実際の長さ次第で7,702〜9,650トークン相当まで振れ、
#   依然コンテキスト長を超過するケースが実測された(固定件数では安全マージンを
#   保証できない)。そのため**プロンプトの推定トークン数ベースで動的に分割**する
#   方式へ変更した。件数上限(下記)は「1チャンクの規模に対する保険」として残す。
#   トークン推定は日本語想定の粗い近似(文字数÷1.5)。
LMSTUDIO_HYBRID_TOKEN_BUDGET = int(os.environ.get("BBS_LMSTUDIO_TOKEN_BUDGET", "6000"))
# 1アイテムあたりの応答JSON(簡素化スキーマ)の推定出力トークン数。入力だけでなく
# 出力もコンテキストを消費するため、チャンクサイズ見積もりに加算する。
LMSTUDIO_HYBRID_OUTPUT_TOKENS_PER_ITEM = int(os.environ.get("BBS_LMSTUDIO_OUTPUT_TOKENS_PER_ITEM", "40"))
# 1チャンクあたりの件数上限(トークン推定が外れた場合の保険・実測で50/80件は
# 成功したことを確認済みのため、余裕を見て100を上限とする)。
LMSTUDIO_HYBRID_CHUNK_SIZE = int(os.environ.get("BBS_LMSTUDIO_CHUNK_SIZE", "100"))

# 非LLM強弱分類の辞書。過剰一致を避けるため「明確に強弱を示す語」に絞る(曖昧語は入れない)。
# ヒット数の多寡で bull/bear を決め、同数/ゼロは投稿者feel(Yahoo)→neutral にフォールバック。
LEXICON_BULLISH = [
    "買い", "買った", "買い増し", "買い増", "買増", "押し目買い", "拾った", "仕込ん",
    "上げ", "爆上げ", "上昇", "上抜け", "急騰", "ストップ高", "S高", "反発", "反騰",
    "ホールド", "握力", "握って", "握り", "含み益", "利益確定", "利確でき", "含み益拡大",
    "期待", "強い", "強気", "伸びる", "伸びしろ", "最高", "神", "爆益", "大勝利",
    "底打ち", "底入れ", "戻り", "戻し", "上方修正", "好決算", "モテる", "テンバガー",
]
LEXICON_BEARISH = [
    "売り", "売った", "投げ", "投げ売り", "利確して撤退", "撤退", "損切り", "損切",
    "下げ", "暴落", "急落", "ストップ安", "S安", "下落", "下抜け", "続落", "崩れ",
    "含み損", "オワタ", "終わった", "終わってる", "オワコン", "退場", "退場した",
    "弱い", "弱気", "地獄", "セリクラ", "狼狽", "狼狽売り", "ナンピン地獄", "塩漬け",
    "追証", "焼かれ", "焼き払", "阿鼻叫喚", "最悪", "絶望", "大損", "泣", "下方修正",
]
# 直前否定(語の直後に来ると符号を反転させる簡易対応。例「上げない」→弱気側)。
LEXICON_NEGATORS = ("ない", "無い", "なかっ", "ず", "ぬ", "ません", "まへん",
                    "なくなっ", "そうにない", "そうもない", "きれない")

# ---- クラスタリング ----------------------------------------------------------
# グルーピングのトークナイズ切替: "word"=fugashi分かち書き+語TF-IDF(意味的まとまり良),
# "char"=文字n-gram(char_wb 2-3gram)。fugashi importに失敗したら自動でchar版へ。
# 環境変数 BBS_GROUPING_TOKENIZER でも上書き可。
GROUPING_TOKENIZER = os.environ.get("BBS_GROUPING_TOKENIZER", "word")

# Agglomerative は cosine距離の事前計算行列 + complete linkage を使う
# (average linkageは高volume同一話題データで連鎖併合し全体が1塊に潰れるため。
#  completeは全対が閾値内の時だけ塊を作るので頑健=実測で塊化を回避)。
# char版のcosine距離閾値(文字n-gram)。
CLUSTER_DISTANCE_THRESHOLD = 0.90
# word版(fugashi語TF-IDF 1-2gram)のcosine距離閾値。語共有で距離が下がるため小さめ。
CLUSTER_DISTANCE_THRESHOLD_WORD = 0.80
CLUSTER_MIN_COMMENTS = 3            # これ未満なら単一クラスタ扱い
CLUSTER_LABEL_SAMPLES = 4          # 代表ラベル生成でOpusに渡す代表コメント数
CLUSTER_MAX_LABELS = 12            # ラベル付けするクラスタ数の上限(コスト抑制)

# ---- スパイク検知 ------------------------------------------------------------
# 前スナップショット比でトピック件数がこの倍率以上かつ最低件数以上なら急増。
SPIKE_RATIO = 2.0
SPIKE_MIN_COUNT = 3

# ---- 価格取得(Yahoo Finance chart API・Chrome UA必須) ----------------------
PRICE_CHART_URL = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}.T"
PRICE_DAILY_PARAMS = "interval=1d&range=6mo"
PRICE_INTRADAY_PARAMS = "interval=5m&range=5d"
PRICE_1M_PARAMS = "interval=1m&range=5d"   # 狭い時間幅用の細かい足(Yahoo 1mは~7dが上限)

# ---- signals(おにや式・逆張りリテールセンチメント研究用) --------------------
# 全シグナルは研究用・未検証(損益ゲート通過まで"シグナル"と呼ばない)。
SIG_ZSCORE_WINDOW = 20        # 日次投稿数/語彙zのtrailing窓(営業日)
SIG_MIN_CALIB_DAYS = 20       # これ未満は「較正中」フラグ
SIG_MIN_DAY_COVERAGE = 10     # この投稿数未満の日はz/較正の基準日に数えない
                              # (StockTwits過去分等の疎な日を混ぜるとzが暴れるため)
SIG_OVERHEAT_TH = 36          # 過熱発火の閾値(0-100・旧70)
SIG_OVERHEAT_WARN = 25        # 過熱警戒の閾値(旧ハードコード50)
SIG_CAPITULATION_WARN = 15    # セリクラ接近(旧50)
SIG_CAPITULATION_FIRE = 30    # セリクラ発火(旧70。安値圏ANDは撤廃=下記c_shock参照)
SIG_CAP_SHOCK_MIN_PCT = 3.0   # 阿鼻叫喚c_shock: 当日下落率がこれ未満(-3%未満)は0
SIG_CAP_SHOCK_FULL_PCT = 15.0 # 阿鼻叫喚c_shock: 当日下落率がこれ以上(-15%)で1.0
SIG_LOW_ZONE_PCT = 2.0        # 20日安値圏 ±%(参考表示用・発火判定からは除外)
SIG_ONIYA_VOTES_MAX = 95      # 単一bullishコメvotes_yes過熱閾値=発火(おにや発言・旧100)
SIG_ONIYA_VOTES_WARN = 65     # 同・警戒(旧ハードコード60)
SIG_EUPHORIA_WARN = 4.0       # イナゴ語彙(euphoria index)警戒(旧ハードコード10)
SIG_EUPHORIA_FIRE = 5.5       # イナゴ語彙 発火(旧ハードコード20)
SIG_NAMED_WARN = 0.06         # ネームド集中(top5share)警戒(旧ハードコード0.35)
SIG_NAMED_FIRE = 0.10         # ネームド集中 発火(旧ハードコード0.5)
SIG_OTHER_WARN = 0.08         # 他銘柄混入率 警戒(旧ハードコード0.15)
SIG_OTHER_FIRE = 0.11         # 他銘柄混入率 発火(旧ハードコード0.3)
SIG_AORI_WARN = 4.0           # 暴落煽り語彙(aori index)警戒(旧ハードコード15)
SIG_AORI_FIRE = 5.0           # 暴落煽り語彙 発火(旧ハードコード30)
SIG_NAMED_MIN_N = 30          # ネームド集中の最低標本数(未満は非表示)
SIG_BEAR_EXTREME = 0.70       # bear率極値
SIG_BULL_EXTREME = 0.70       # bull率極値

# ============================================================================
# 研究層(signal export / forward-OOS / BVP) — 全てOFF-by-default・研究用・未検証
# 仕様: _handoff/研究層実装仕様_v1_2026-07-09.md
# ============================================================================
# 消費側(トレPJ)がexportを実際に使うためのenvゲート。既定'0'=OFF(発注経路に載せない)。
SIGNAL_ENABLE = os.environ.get("BBS_SIGNAL_ENABLE", "0") == "1"

# ---- signal export(機械可読・研究専用) ----
SIGNAL_EXPORT_DIR   = os.path.join(DATA_DIR, "signal_export")
SIGNAL_HISTORY_PATH = os.path.join(SIGNAL_EXPORT_DIR, "history.jsonl")
SIGNAL_LATEST_PATH  = os.path.join(SIGNAL_EXPORT_DIR, "latest.json")
SIGNAL_SCHEMA_VERSION = "1.0"

# ---- research / forward-OOS ----
RESEARCH_DIR     = os.path.join(BASE_DIR, "research", "_ledger")
FORWARD_OOS_PATH = os.path.join(RESEARCH_DIR, "forward_sentiment_285A.csv")
# これ以前(<=)は IS seed(is_oos=false)。真OOSは date > HARNESS_START_DATE のみ。
HARNESS_START_DATE = "2026-07-08"

# ---- backtest 往復コスト(全プロト必須のコスト補正=連携ログ2026-07-09 番犬systemic警鐘) ----
#   285Aは10万円台・実測spread中央値≈20円/株(トレPJ計測)+スリッページ。手数料0(eスマート)。
#   往復(建て+返し)の対名目コスト率。size|pos|でスケール。gross偏りを避ける唯一KPI=net。
BACKTEST_ROUNDTRIP_COST = float(os.environ.get("BBS_BACKTEST_ROUNDTRIP_COST", "0.0006"))

# ---- 掲示板×株価 記述連動の日次追跡(研究レーン・別台帳・シグナル非feed) ----
#   コーディネーターGO(2026-07-11)。記述で昇格でない・発注に一切影響しない=既定ON(=記録のみ)。
#   OFFにするとrun_onceの追跡step7をスキップ(台帳不変)。
BBS_LINKAGE_DAILY = os.environ.get("BBS_LINKAGE_DAILY", "1") == "1"

# ---- 小口chase/capitulation 記述台帳(凍結定義v1・研究レーン・別台帳・シグナル非feed) ----
#   北極星「本物/偽物上昇の判別」のおにや担当(research/retail_chase.py・spec="rc1")。
#   記述専用=昇格でない・発注に一切影響しない。OFFにすると run_once の step8 をスキップ(台帳不変)。
BBS_CHASE_DAILY = os.environ.get("BBS_CHASE_DAILY", "1") == "1"

# ---- capit_v2(level成分)の A1 前向き検証を日次で積むか ----
#   A1 = research/A1_capit_level_2026-07-16.md（式・閾値・KPI・撤退条件を結果を見る前に凍結済）。
#   ONの意味は「別台帳 retail_chase_v2_285A.jsonl へ日次appendする」ことだけ＝
#   現行 capit(rc2=step8)の定義・台帳・シグナルには一切触れない(A1 §6)。記述で昇格でない。
#   既定ON＝A1 §5が要求する前向き10-20営業日を自動で積むため(手動実行に依存させない)。
#   is_oos境界(date>2026-07-16)は capit_v2_level 側が保持。止める時は BBS_CAPIT_V2_DAILY=0。
BBS_CAPIT_V2_DAILY = os.environ.get("BBS_CAPIT_V2_DAILY", "1") == "1"

# ---- vote_ratio(票の確信度)→+2バケット出来高 の PITクリーン前向き検証(A1 vote_ratio確信度) ----
#   A1 = _handoff/A1事前登録_vote_ratio確信度_2026-07-11.md／番犬2026-07-19「条件付きPASS」の5条件を
#   research/votes_pit_eval.py が構造で担保(PIT専用アグリゲータ=累積票経路を流用しない・(vote_ratio,tvol,+2)固定・
#   deseason+count偏相関に判定束縛・family_reality_checkをintraday結線+ex-ante登録・min_oos=15)。
#   ONの意味は「別台帳 votes_pit_285A.jsonl へ日次appendする」ことだけ＝
#   凍結台帳 forward_sentiment_285A.csv と signal_engine の spec_hash には一切触れない。記述で昇格でない。
#   既定ON＝真OOS15取引日を自動で積むため(評価そのものは成熟後・未成熟は評価不能で点推定を出さない)。
#   止める時は BBS_VOTES_PIT_EVAL=0。
BBS_VOTES_PIT_EVAL = os.environ.get("BBS_VOTES_PIT_EVAL", "1") == "1"

# ---- 半導体ピア(SanDisk/Micron/SK Hynix)→285A クロス市場オーバーナイト・リード ----
#   A1 = _handoff\A1事前登録_半導体ピア→285A_2026-07-19.md（仮説/KPI/窓/先読み排除/コスト後ゲート凍結）。
#   research\sector_peer_lead.py＝①ピア価格C-Cリターン→翌285A取引日の寄りギャップ(as-of先読み排除)
#   ②MU/SNDK StockTwitsセンチメント(前向き蓄積・vol側)。別台帳 sector_peer_lead_285A.jsonl。
#   ONの意味は「別台帳へ日次appendする」ことだけ＝凍結台帳 forward_sentiment_285A と spec_hash 非接触。
#   真OOS=date>2026-07-19・15取引日未満は評価不能(IS-seedはscreening・点推定を判定に使わない)。
#   ネット取得(Yahoo/StockTwits)を伴うので失敗分離必須。止める時は SECTOR_PEER_LEAD_DAILY=0。
SECTOR_PEER_LEAD_DAILY = os.environ.get("SECTOR_PEER_LEAD_DAILY", "1") == "1"

# ---- 掲示板"注目"の相対(trailing平均比・増減対称)→翌日285A vol/出来高 ----
#   A1 = _handoff\A1事前登録_掲示板注目相対_2026-07-19.md（AltIndex由来の「直近平均比の増減」枠組みを
#   我々のPIT/family規律に載せる）。research\attention_relative.py＝forward_sentiment_285A.csv を read専用で読み
#   log_rel_W=ln(true_volume/直近W日平均)・decay_W(引き潮) → forward_volume_1d/rv_1d。level(posts_z_dense)への
#   上乗せ増分で採否。別台帳 attention_relative_285A.jsonl。凍結台帳 forward_sentiment_285A・spec_hash 非接触。
#   真OOS=date>2026-07-19・15取引日未満は評価不能(IS-seedはscreening)。止める時は ATTENTION_RELATIVE_DAILY=0。
ATTENTION_RELATIVE_DAILY = os.environ.get("ATTENTION_RELATIVE_DAILY", "1") == "1"

# ---- 場後(15:00-23:59:59 JST)弱気投稿→翌営業日リターン 記述追跡(丸山ほか2008) ----
#   出典: 丸山健・梅原英一・諏訪博彦・太田敏澄「インターネット株式掲示板の投稿内容と株式市場の関係」
#   証券アナリストジャーナル46巻11/12号(2008)。日本のYahoo!株式掲示板50銘柄(2005-2006)分析で
#   「場後(15:00-24:00)の弱気投稿数は翌日リターンに対し1%有意な負の先行指標」と報告。
#   research\afterhours_bearish.py(spec="ab1")＝285Aでこの関係を記述的に追跡するのみ。
#   sentiment分類は retail_chase(rc2)を再利用。別台帳 afterhours_bearish_285A.jsonl。
#   ONの意味は「別台帳へ日次append + settle(next_day_returnの後埋め)」を毎run実行することだけ＝
#   凍結台帳 forward_sentiment_285A.csv と signal_engine の spec_hash には一切触れない。
#   記述専用=昇格でない・発注に一切影響しない。OFFにすると run_once の該当stepをスキップ(台帳不変)。
#   止める時は BBS_AFTERHOURS_BEARISH_DAILY=0。
BBS_AFTERHOURS_BEARISH_DAILY = os.environ.get("BBS_AFTERHOURS_BEARISH_DAILY", "1") == "1"

# ---- 生データ(DATA_DIR)の外付けHD(D:)への日次ミラー ----
#   ★2026-07-18 ユーザー決定=大容量データ方針(tick側と同じ=ローカルC:+外付けD:)。
#   run_once は5分毎ゆえ「1日1回だけ」に壁時計ゲートで間引く(CLAUDE.md推奨型=
#   run_every頻繁チェック・実処理は間引く)。robocopy /MIR(差分のみ)・失敗分離。
#   止める時は BBS_EXTERNAL_BACKUP=0。ミラー先は BBS_EXTERNAL_BACKUP_DIR で上書き可。
BBS_EXTERNAL_BACKUP = os.environ.get("BBS_EXTERNAL_BACKUP", "1") == "1"
BBS_EXTERNAL_BACKUP_DIR = os.environ.get("BBS_EXTERNAL_BACKUP_DIR") \
    or r"D:\AI用フォルダ\おにや式投資法\data"

# ---- dense-session カバレッジゲート(posts_z=69アーティファクト根絶) ----
SIG_MIN_HOUR_BUCKETS = 4    # DENSE判定に必要な取引時間バケット数(09..15の別時)
SIG_DENSE_MIN_CALIB  = 10   # dense_session_count がこれ未満は cross-day z を抑制(None)
TRADING_HOURS = (9, 10, 11, 12, 13, 14, 15)  # 取引時間帯(バケット判定用)

# ---- BVP (BoardVolPressure) ----
BVP_FEATURE_WEIGHTS = None   # None => 等加重(forward-OOS窓ができるまで凍結)
BVP_WINSOR_Z        = 3.0
BVP_REGIME_PCT      = {"normal": 50, "elevated": 80, "extreme": 95}  # BVPのpercentile境界
BVP_CONF_CALIB_CAP  = 0.30   # 較正中はconfidenceをこれ以下に強制

# ---- 計測スペック改定(measurement revision) ----
# パラメータ値は不変でも「計測ロジック」を変えたら bump する。spec_hash に折り込むので
# 旧セグメント(古い凍結行)と新セグメントが signal_spec_hash で必ず区別される(混ぜて集計しない)。
# rev 2 (2026-07-09): 出力の正直さ修正 3件=
#   ① features.state の較正カウントを dense-session 基準へ統一(非dense n を出さない)
#   ② named_concentration が author だけでなく user も投稿者名として集計(user限定行を除外しない)
#   ③ range_day_score を dense-honest な posts_z から算出(dense未了なら None=vol側と整合)
# rev 3 (2026-07-09): 強弱分類器を既定で非LLM(辞書 classify_lexicon)へ切替。
#   sentiment(bull/bear/neutral)の生成ロジックが変わる=bull_ratio/bear_ratio の作り方が
#   変わるため、旧LLMセグメントと混ぜないよう bump(spec_hash が変わり新台帳セグメント化)。
SIG_MEASURE_REV = 3


def ensure_signal_export_dir():
    os.makedirs(SIGNAL_EXPORT_DIR, exist_ok=True)


def ensure_research_dir():
    os.makedirs(RESEARCH_DIR, exist_ok=True)


# ============================================================================
# 一般公開用エクスポート(public_export.py) — 集計値のみ・個別投稿情報は一切含めない
# 将来 Googleサイト「掲示板の分析による投資情報」で公開予定(2026-08-16 合意)。
# 既存の SIGNAL_EXPORT_*(トレPJ向け内部シグナル・別スキーマ)には一切触れない・
# 完全に独立した新規パス。読み取り専用の生データ(RAW_COMMENTS_PATH等)には触れない。
# ============================================================================
PUBLIC_EXPORT_DIR          = os.path.join(DATA_DIR, "public_export")
PUBLIC_EXPORT_LATEST_PATH  = os.path.join(PUBLIC_EXPORT_DIR, "latest.json")
PUBLIC_EXPORT_HISTORY_PATH = os.path.join(PUBLIC_EXPORT_DIR, "history.jsonl")
PUBLIC_EXPORT_SCHEMA_VERSION = "1.0"


def ensure_public_export_dir():
    os.makedirs(PUBLIC_EXPORT_DIR, exist_ok=True)


# ★2026-08-19追加(ユーザー依頼: 公開ダッシュボードをStreamlit Community Cloudへ
# デプロイ)。クラウド環境ではDATA_DIR配下のlatest.jsonへ直接アクセスできないため、
# 環境変数が設定されていればGoogle Sheets(json_blobタブを「ウェブに公開」した
# CSV書き出しURL)からデータを読む(public_export.load_public_latest_from_url()参照)。
# 未設定(既定)ならローカルのlatest.jsonをそのまま読む従来動作を維持する
# (ローカル環境ではこの分岐に一切影響しない=既存の全selftest/実行を壊さない)。
PUBLIC_JSON_SOURCE_URL = os.environ.get("BBS_PUBLIC_JSON_URL")

# ★2026-08-20追加(ユーザー依頼: 公開サイトの閲覧者数を集計して表示)。既存の
# Google Sheets連携基盤を流用し、Google Apps Script Web App(doGet・visit_counter
# タブへ1加算/読取)のURLをここで受ける。未設定なら公開ダッシュボード側で
# バッジ自体を表示しない(fail-soft・新しい第三者サービスへは接続しない設計)。
PUBLIC_VISIT_COUNTER_URL = os.environ.get("BBS_VISIT_COUNTER_URL")

# ---- ライブ価格ブリッジ(live_price_bridge.py) — プロト1のkabuティックCSV(読み取り専用) ----
#   ★2026-08-20追加(ユーザー指示: 公開ダッシュボードの価格をYahoo Finance APIでは
#   なく、株取引API_プロト1がkabuステーションAPIで自己収集済みのティックデータへ
#   切替え、60秒毎に最新値へ更新する)。プロト1のファイル・コードには一切書き込まない
#   (config.py既存方針「既存トレPJの台帳/コードは触らない」を維持=読み取り専用)。
#   パスはこのPC(家PC1)のユーザーフォルダ配下(環境依存)のためenvで上書き可能にする。
KABU_PROTO1_285A_TICKS_DIR = os.environ.get(
    "KABU_PROTO1_285A_TICKS_DIR",
    r"C:\Users\ryuta\OneDrive\AI用フォルダ\株取引API_プロト1\285A_キオクシア\記録データ")

# クラウド公開ダッシュボード側がlive_priceタブ(live_price_bridge.pyが1分毎に書く
# 軽量スナップショット)を読むための「ウェブに公開」CSV URL。未設定ならこの経路を
# 使わず、フォールバック(Yahoo直接取得→Sheets由来のrec['price'])へ順に落ちる。
PUBLIC_LIVE_PRICE_SOURCE_URL = os.environ.get("BBS_LIVE_PRICE_URL")

# ---- 板総計ブリッジ(board_totals_bridge.py) — プロト1の板CSV(読み取り専用) ----
#   ★2026-08-21追加(ユーザー依頼「板の買い・売り総計(成行含む全価格帯)の推移を
#   折れ線グラフで」。おにや10:42投稿で仕様確定・トレPJ10:47投稿で記録側に
#   over_sell_qty/under_buy_qty/market_sell_qty/market_buy_qtyの4列を追加・
#   反映は2026-08-21 11:30(昼休み・record_all.py再起動)以降)。live_price_bridge.py
#   と同じ設計(プロト1のファイル・コードには一切書き込まない・読み取り専用)。
KABU_PROTO1_285A_BOARD_DIR = os.environ.get(
    "KABU_PROTO1_285A_BOARD_DIR",
    r"C:\Users\ryuta\OneDrive\AI用フォルダ\株取引API_プロト1\_board")

# クラウド公開ダッシュボード側がboard_totalsタブ(board_totals_bridge.pyが1分毎に
# 書く60秒足の買い/売り総計系列)を読むための「ウェブに公開」CSV URL。未設定なら
# このチャート自体を表示しない(fail-soft・他フィールドの表示には影響しない)。
PUBLIC_BOARD_TOTALS_SOURCE_URL = os.environ.get("BBS_BOARD_TOTALS_URL")

# ★2026-08-20緊急追加(ユーザー報告「過去24時間のセンチメント推移が表示されない」
# への対応): sentiment_last_24h(過去24時間・10分毎)はjson_blobへ含めると
# Google Sheetsの1セル上限(50,000字)を超過するため、専用タブ(sentiment_24h)へ
# 分離した(public_sheets_sync.TAB_SENTIMENT_24H参照)。クラウド公開ダッシュボード
# 側がそのタブを読むための「ウェブに公開」CSV URL。未設定ならこの経路を使わず、
# rec['sentiment_last_24h'](ローカル直接読み・またはjson_blob同期が古い版のまま
# だった場合はキー自体が無い)へフォールバックする。
PUBLIC_SENTIMENT_24H_SOURCE_URL = os.environ.get("BBS_SENTIMENT_24H_URL")

# ★2026-08-20追加(ユーザー提案「live_price_bridgeの死活監視」)。live_priceタブの
# generated_atがこの分数より古ければ、公開ダッシュボードのヘッダーに軽い注意表示を
# 出す(public_export.live_price_staleness_minutes参照)。1分毎更新の設計に対し、
# 数分程度のズレ(タスクの実行タイミングのブレ・場中の一時的な遅延)は正常範囲として
# 許容し、それを明確に超えたら「ブリッジが止まっているかもしれない」サインとする。
LIVE_PRICE_STALE_MINUTES = int(os.environ.get("BBS_LIVE_PRICE_STALE_MINUTES", "5"))


# ============================================================================
# Google Sheets 同期(public_sheets_sync.py) — public_export.py の latest.json を
# Google Sheetsへ書き込む Phase 2。既定は無効(GSHEETS_SYNC_ENABLED=False)。
# 明示的に有効化するまで run_once.py 側はスキップする(Phase 1 の --with-commentary と
# 同じ安全側設計)。latest.json 自体には一切変更を加えない(読むだけ)。
# ============================================================================
GSHEETS_KEY_PATH = os.environ.get(
    "GSHEETS_KEY_PATH",
    r"C:\AI用フォルダ\おにや式投資法\secrets\gsheets_key.json")
GSHEETS_SPREADSHEET_ID = os.environ.get(
    "GSHEETS_SPREADSHEET_ID",
    "12gxBT9fbAAeYRm_zyt7Tusdi5QvuSOeSzPH4teR0MVQ")

# ★2026-08-16 ユーザー承認により本番有効化(Phase 1/2の検証完了・実データ書込み+
#   読み戻し確認済み)。env `GSHEETS_SYNC_ENABLED=0` で即座に無効化可能(フェイルセーフ)。
GSHEETS_SYNC_ENABLED = os.environ.get("GSHEETS_SYNC_ENABLED", "1") == "1"


# ============================================================================
# public_export.py latest.json 自動再生成(run_once.py step13.5)
# ============================================================================
# 欠落修正(2026-08-16発覚): run_once.py の step14(public_sheets_sync)はlatest.jsonを
# 読むだけで、latest.json自体を「このrunの最新データで」再生成するstepがrun_once.pyに
# 存在しなかった(=Sheetsに同じ古い値を書き続ける欠落)。この定数がTrueの間、step14の
# 直前で public_export._build_from_live_data() を呼び latest.json を再生成する。
# GSHEETS_SYNC_ENABLED と同型の安全弁(既定True・env `PUBLIC_EXPORT_AUTO_REFRESH=0` で
# 即座に無効化可能)。
PUBLIC_EXPORT_AUTO_REFRESH = os.environ.get("PUBLIC_EXPORT_AUTO_REFRESH", "1") == "1"

# ★2026-08-19: 公開AI考察(public_insight.generate_public_insight)のバックエンド。
#   "lmstudio"(既定・おにや08:57投稿=ローカルLLM化・無料のため下記の1日1回ゲートを
#   受けない=毎回生成) | "claude"(従来のOpus API経路・即座に戻せるよう環境変数一つで
#   切替可能に残す。この経路は課金があるため下の1日1回ゲートを引き続き適用する)。
PUBLIC_INSIGHT_BACKEND = os.environ.get("PUBLIC_INSIGHT_BACKEND", "lmstudio")

# ---- AI考察(ai_commentary)の1日1回自動生成ゲート ----
# ★2026-08-19: PUBLIC_INSIGHT_BACKEND=="lmstudio"(既定)の間はこのゲートを適用しない
# (run_once._run_public_export_refresh_step()側で分岐・無料のローカルLLMなので毎回
# 再生成してよい=おにや08:57投稿②)。このゲートは PUBLIC_INSIGHT_BACKEND=="claude" に
# 切り替えた場合の課金保険として残す。
# 毎時間の自動実行(上記 PUBLIC_EXPORT_AUTO_REFRESH による再生成)ではAI考察を生成しない
# (有料API課金のため既定 with_commentary=False)。この定数がTrueの間だけ、JST当日分の
# ai_commentary をまだ生成していなければ、その日最初にこのstepへ到達したrunで1回だけ
# commentary 付きで生成する(public_export.should_generate_commentary_today() の冪等
# ゲートで判定。latest.json / history.jsonl の ai_commentary.generated_at 日付を比較)。
# False なら自動commentary生成は完全に無効(手動 `public_export.py --with-commentary` のみ)。
# 止める時は PUBLIC_EXPORT_COMMENTARY_DAILY=0。
PUBLIC_EXPORT_COMMENTARY_DAILY = os.environ.get("PUBLIC_EXPORT_COMMENTARY_DAILY", "1") == "1"

# ★2026-08-20追加(ユーザー提案「AI考察生成の失敗が静かに握りつぶされないように」)。
# AI考察(ai_commentary)の生成が連続でこの回数以上失敗したら、run.logへ通常のWARNより
# 目立つERRORレベルで記録する(run_once._update_commentary_failure_streak()参照)。
# 連携ログへの自動書き込みはしない(投稿は人/AIセッションが実際に確認してから書く、
# という既存の運用規律を機械生成のログで壊さないため)。日次監視チェックリスト側で
# このrun.log警告の有無を確認する運用とする。
AI_COMMENTARY_FAILURE_STATE_PATH = os.path.join(DATA_DIR, "ai_commentary_failure_state.json")
AI_COMMENTARY_FAILURE_WARN_THRESHOLD = int(
    os.environ.get("BBS_AI_COMMENTARY_FAILURE_THRESHOLD", "3"))

# ★2026-08-27追加(ユーザー依頼「24時間以内のキオクシアに関係しそうなニュースの要約を
# 公開ダッシュボードに」「検索頻度は10分毎・新ニュースが出たら更新とリンクを」)。
# news_fetch.py(Google News RSS・APIキー不要)の設定。NEWS_SEEN_STATE_PATHは前回時点の
# 直近24h窓内リンク一覧+前回の要約を保持する状態ファイル(collect_news()参照)。
NEWS_SEARCH_QUERY = os.environ.get("BBS_NEWS_SEARCH_QUERY", "キオクシア OR 285A")
NEWS_SEEN_STATE_PATH = os.path.join(DATA_DIR, "news_seen_state.json")
NEWS_FETCH_TIMEOUT_SEC = int(os.environ.get("BBS_NEWS_FETCH_TIMEOUT_SEC", "8"))
NEWS_SUMMARY_MAX_TOKENS = int(os.environ.get("BBS_NEWS_SUMMARY_MAX_TOKENS", "400"))

# ★2026-08-21追加(おにや提案・連携ログ01:38投稿=エンジニアの深堀質問③への回答で発覚)。
# analyze_lock/export_lockの取得失敗(busy)は従来WARN単発ログのみで、連続回数・累積占有を
# 追跡する仕組みが無く「手動の実データ再検証でしか気づけない」状態だった(2026-08-19に
# おにやが3回連続で手動発見)。AI考察失敗ストリーク(直上)と全く同型のパターンを
# ロック取得busy側にも横展開する(run_once._update_lock_busy_streak()参照)。
# ロックは2種(analyze/export)あるため状態ファイルも別々に持つ。
ANALYZE_LOCK_BUSY_STATE_PATH = os.path.join(DATA_DIR, "analyze_lock_busy_state.json")
EXPORT_LOCK_BUSY_STATE_PATH = os.path.join(DATA_DIR, "export_lock_busy_state.json")
LOCK_BUSY_WARN_THRESHOLD = int(os.environ.get("BBS_LOCK_BUSY_THRESHOLD", "3"))


# ============================================================================
# moomoo / Futu OpenAPI 読み取り専用アダプタ (moomoo_source.py) — 全てOFF-by-default
# ============================================================================
# 規律(絶対):
#   - 取引/発注APIは一切使わない(OpenQuoteContext の quote 系のみ・OpenSecTradeContext 非import)。
#   - read-only・別台帳(下記 MOOMOO_*_PATH)・env 明示有効化まで動かない。
#   - パスワード/口座情報はファイル/log/print に出さない(OpenD がログイン管理・本アダプタは
#     localhost の OpenD に接続するだけ)。
# 明示有効化ゲート(既定'0'=OFF)。'1' のときだけ collect() が実接続する。
MOOMOO_ENABLE = os.environ.get("BBS_MOOMOO_ENABLE", "0") == "1"


def _moomoo_int_env(name, default):
    """env の数値を安全に読む(不正値でも例外を出さず default = fail-soft)。"""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# OpenD(moomoo のローカルゲートウェイ)の接続先。既定は localhost:11111(OpenD 既定ポート)。
MOOMOO_OPEND_HOST = os.environ.get("MOOMOO_OPEND_HOST", "127.0.0.1")
MOOMOO_OPEND_PORT = _moomoo_int_env("MOOMOO_OPEND_PORT", 11111)

# 小口(リテール主導度)判定の閾値[株数]。この株数"未満"の約定を小口とみなす。
# 根拠: 東証は売買単位(単元) = 100株 が標準(285A キオクシアも 100 株単位)。個人の成行/
# 指値は 1〜4 単元(100〜400 株)が中心なので、既定 500 株(=5 単元未満)で「小口≒リテール」を
# 大掴みに切り出す。機関のブロックは数千株以上に出るので明確に分離できる。env で上書き可。
MOOMOO_SMALL_LOT_SHARES = _moomoo_int_env("MOOMOO_SMALL_LOT_SHARES", 500)

# futu の市場コード。銘柄表記は "市場.コード" 形式(例 US.AAPL / HK.00700)。
# 東証(日本株)は Market.JP='JP' なので 285A は "JP.285A"。
MOOMOO_MARKET_PREFIX = "JP"

# 別台帳(append-only・既存の収集/台帳とは完全分離)。
MOOMOO_TICKS_PATH = os.path.join(DATA_DIR, "moomoo_ticks_285A.jsonl")
MOOMOO_BOOK_PATH  = os.path.join(DATA_DIR, "moomoo_book_285A.jsonl")
MOOMOO_CAPITAL_PATH = os.path.join(DATA_DIR, "moomoo_capital_285A.jsonl")  # 資金分布(大口/小口フロー)
