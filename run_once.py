# -*- coding: utf-8 -*-
"""
run_once.py - collect -> analyze -> snapshot を順に1回実行するエントリポイント。

各段の失敗を分離してログ。片方が落ちても後段を可能な範囲で続行する。
発注は一切しない。別台帳のみ更新。
"""
import os
import sys
import json
import time
import datetime as dt

import config


def _maybe_mirror_external():
    """生データ(DATA_DIR)を外付けHD(D:)へ日次ミラー(大容量データ方針=tick側と同じ)。
    run_once は5分毎ゆえ「1日1回だけ」に壁時計ゲートで間引く(CLAUDE.md推奨型)。
    失敗分離=バックアップが失敗しても収集本体は絶対に止めない。robocopy /MIR(差分のみ)。
    毎日の最初の run_once で発火し、その時点の C: 全体を D: へ鏡写しする。"""
    if not getattr(config, "BBS_EXTERNAL_BACKUP", False):
        return
    try:
        import subprocess
        mark = os.path.join(config.DATA_DIR, ".last_external_mirror")
        today = dt.date.today().isoformat()
        last = ""
        if os.path.exists(mark):
            try:
                last = open(mark, encoding="utf-8").read().strip()
            except OSError:
                last = ""
        if last == today:
            return   # 本日は実施済み(1日1回に間引く)
        dst = getattr(config, "BBS_EXTERNAL_BACKUP_DIR",
                      r"D:\AI用フォルダ\おにや式投資法\data")
        r = subprocess.run(
            ["robocopy", config.DATA_DIR, dst, "/MIR", "/R:1", "/W:1",
             "/NP", "/NDL", "/NFL", "/NJH", "/NJS"],
            capture_output=True)
        if r.returncode < 8:      # robocopy 0-7=正常(8以上=エラー)
            with open(mark, "w", encoding="utf-8") as f:
                f.write(today)
            _log(f"external_backup ok: C->D mirror (rc={r.returncode})")
        else:
            _log(f"ERROR external_backup robocopy rc={r.returncode}")
    except Exception as e:
        _log(f"ERROR external_backup failed: {e!r}")


def _log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] run_once: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_ANALYZE_LOCK_STALE_SEC = 3600   # 60分超のlockはクラッシュ残骸とみなし強制解除(analyzeは最大40分想定)


def _acquire_analyze_lock(timeout_sec=15):
    """フル実行(60分毎)とcatchup(高頻度・2026-08-17追加)が同時にLLM分析(analyze)を
    行い、LM Studioへ二重にリクエストを送らないためのプロセス間ロック。
    price_fetch._acquire_price_lock()と全く同じ設計(排他生成O_CREAT|O_EXCL・
    stale-lock強制解除・timeout時はNoneを返しfail-soft)。timeout_secを短め(既定15秒)
    にしているのは、analyze自体は最大LMSTUDIO_ANALYZE_TIME_BUDGET_SEC(既定40分)
    かかりうるため、長時間ブロックして待つのではなく「今回のこのサイクルは諦めて
    次のサイクルに譲る」判断を素早く下すため。"""
    import os
    import time as _time
    lock_path = os.path.join(config.DATA_DIR, ".analyze.lock")
    deadline = _time.time() + timeout_sec
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
            return lock_path
        except FileExistsError:
            try:
                age = _time.time() - os.path.getmtime(lock_path)
                if age > _ANALYZE_LOCK_STALE_SEC:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if _time.time() >= deadline:
                return None
            _time.sleep(0.3)


def _release_analyze_lock(lock_path):
    if not lock_path:
        return
    import os
    try:
        os.remove(lock_path)
    except OSError:
        pass


_COLLECT_LOCK_STALE_SEC = 300   # 5分超のlockはクラッシュ残骸とみなし強制解除
                                 # (通常のcollectは数秒・200ページ大量取得の
                                 # 最悪ケースでも実測約3分のため十分な余裕)


def _acquire_collect_lock(timeout_sec=15):
    """★2026-08-21追加(おにや20:59投稿・改善提案③)。collect_yahoo/collect_5ch/
    collect_intlの3収集元が同一のseen_ids.json/raw_comments.jsonlを排他制御
    無しで共有していたため、collect-only(数分毎)/catchup(15-20分毎)/フル
    (毎時)の3系統が同時にcollect()を実行すると、load→dedupe→append→save
    の間にread/write競合が起き、seen_ids.jsonが空集合として読まれる
    (=既読情報が消えたのと同じ状態になる)ことがあった。実測ではこれが原因で
    Yahoo収集がYAHOO_MAX_PAGES(200ページ)まで無駄に遡り、raw_comments.jsonl
    に214,615行もの重複が蓄積していた(過去1ヶ月で少なくとも10回発生)。
    _acquire_analyze_lock()と全く同じ設計(排他生成O_CREAT|O_EXCL・stale-lock
    強制解除・timeout時はNoneを返しfail-soft=「今回のcollectは諦めて次サイクル
    に譲る」)の別ロックファイル。collect自体は通常数秒で終わるため、
    ここでロックが取れない=ちょうど他プロセスがcollect中という稀なケースのみ
    発生し、収集の取りこぼしは次サイクル(数分後)で自然に解消される。"""
    import os
    import time as _time
    lock_path = os.path.join(config.DATA_DIR, ".collect.lock")
    deadline = _time.time() + timeout_sec
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
            return lock_path
        except FileExistsError:
            try:
                age = _time.time() - os.path.getmtime(lock_path)
                if age > _COLLECT_LOCK_STALE_SEC:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if _time.time() >= deadline:
                return None
            _time.sleep(0.3)


def _release_collect_lock(lock_path):
    if not lock_path:
        return
    import os
    try:
        os.remove(lock_path)
    except OSError:
        pass


_EXPORT_LOCK_STALE_SEC = 900   # 15分超のlockはクラッシュ残骸とみなし強制解除(export/sheets/AI考察は数分想定)


def _acquire_export_lock(timeout_sec=15):
    """★2026-08-19(おにや08:57投稿④): 公開エクスポート再生成+Sheets同期+AI考察生成
    (LM Studio呼び出しを含む)という一連の処理全体を包むプロセス間ロック。catchupを
    10分毎化した結果、毎時フル実行の同ブロックと時間的に重なる可能性が実質的に
    高まったため新設(_acquire_analyze_lockと全く同じ設計・別ロックファイル)。
    analyze-lockとは別ドメイン(このstepはanalyze完了後に呼ばれるため通常は
    同一プロセス内で時間的に重ならないが、フル実行とcatchupという別プロセス間では
    重なりうる)。timeout_sec短め(既定15秒)=長時間ブロックせず今回のサイクルを
    諦めて次回に譲る設計もanalyze-lockと同じ。"""
    import os
    import time as _time
    lock_path = os.path.join(config.DATA_DIR, ".export.lock")
    deadline = _time.time() + timeout_sec
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
            return lock_path
        except FileExistsError:
            try:
                age = _time.time() - os.path.getmtime(lock_path)
                if age > _EXPORT_LOCK_STALE_SEC:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if _time.time() >= deadline:
                return None
            _time.sleep(0.3)


def _release_export_lock(lock_path):
    if not lock_path:
        return
    import os
    try:
        os.remove(lock_path)
    except OSError:
        pass


def main(collect_only=False, catchup=False):
    """collect_only=True: 収集+価格+スナップショットのみ(LLM分析/研究台帳を省く高速サイクル)。
    場中の鮮度用=無料(LLMなし)。ダッシュボードは生コメントから信号をライブ再計算するため、
    量/灼熱/阿鼻叫喚/速度/サージ 等が最新化される。強弱比(AI)は毎時の全サイクルで更新。

    catchup=True: ★2026-08-17追加(ユーザー承認済み・CROSS_PROJECT_LOG参照)・2026-08-19更新
    (おにや08:57投稿)。collect_onlyとの違いはLLM分析(step2)を**含む**こと=収集+価格+
    分析(LLM)+スナップショットを行い、研究層(step4-13)だけは省く軽量サイクル(研究層は
    毎時フル実行が担当・凍結の二重発火を避けるため)。★2026-08-19: 公開エクスポート
    再生成・Sheets同期・AI考察生成(step13.5/14)は**catchupでも実行するよう変更**
    (従来は毎時フル実行のみだったため公開ダッシュボードの更新間隔が実質60分だった。
    catchupの実行間隔を15分→10分へ短縮しこのstepも含めることで公開更新を10分化)。
    毎時のフル実行(60分間隔・研究層込み)だけでは697→20000上限化後でも壁時計デッドライン
    (LMSTUDIO_ANALYZE_TIME_BUDGET_SEC)一杯までしか1回のrunで処理できず、高ボラ日に積み
    上がった数千件規模の残債(バックログ)の解消には多くの時間がかかる問題が実測で判明した
    ため、より高頻度にこの軽量サイクルを追加起動し、残債解消を加速する
    (run_catchup.bat・タスクスケジューラ`BBS_Sentiment_Catchup`)。フル実行のanalyzeと
    同時に走ってLM Studioへ二重リクエストを送らないよう _acquire_analyze_lock() で
    相互排他する(price_fetch.pyの排他ロックと同じ設計)。公開エクスポート/Sheets同期/
    AI考察生成側も同様に _acquire_export_lock() で相互排他する(こちらもLM Studioを
    呼ぶため・詳細は該当箇所のコメント参照)。バックログが少ない日は_pending_rawが
    返す件数がすぐ尽きるためanalyze自体が数秒で終わり、GPU/LLMリソースを無駄に
    占有しない(明示的な閾値ゲートは不要=デッドライン機構が自然にスケールする)。"""
    run_start = time.time()
    config.ensure_data_dir()
    _mode_tag = " (collect-only)" if collect_only else (" (catchup)" if catchup else "")
    _log(f"=== start{_mode_tag} ===")
    new_count = 0
    analyze_summary = {}

    # 1) collect (ソースごとに失敗分離。1ソース落ちても他は生きる)。
    #    ★2026-08-21追加(おにや20:59投稿・改善提案③): 3ソースが共有する
    #    seen_ids.json/raw_comments.jsonlへのread-modify-write競合を防ぐため、
    #    collect-only/catchup/フル実行という独立スケジュールの3系統間で
    #    排他ロックする(_acquire_collect_lockのdocstring参照)。ロックが
    #    取れない場合はこのサイクルのcollect全体を丸ごとスキップし(fail-soft・
    #    次サイクルへ譲る)、部分的なロックだけの取得(1ソースだけロック内・
    #    他がロック外)という中途半端な状態を作らない。
    collect_lock = _acquire_collect_lock()
    if collect_lock is None:
        _log("WARN collect_lock busy (another run_once collecting concurrently) "
             "- skipping collect this cycle (fail-soft, next cycle will retry)")
    else:
        try:
            for name, mod in [("yahoo", "collect_yahoo"),
                              ("5ch", "collect_5ch"),
                              ("intl", "collect_intl")]:
                try:
                    m = __import__(mod)
                    new_rows, total = m.collect()
                    new_count += len(new_rows)
                    _log(f"collect[{name}] ok: parsed={total} new={len(new_rows)}")
                except Exception as e:
                    _log(f"ERROR collect[{name}] failed: {e!r}")
        finally:
            _release_collect_lock(collect_lock)

    # 1.5) price(価格取得。失敗しても続行=fail-soft)
    try:
        import price_fetch
        nd, ni = price_fetch.fetch_and_save()
        _log(f"price ok: daily={nd} intraday={ni}")
    except Exception as e:
        _log(f"ERROR price failed: {e!r}")

    # 1.6) PTS(夜間取引)・米国ADR円換算(★2026-08-19追加・ユーザー依頼「翌日傾向の
    #     手がかりとしてAI考察へ反映」)。Yahoo(285A.T)とは完全に別ドメイン・別ファイル
    #     の取得のため、1.5と独立したstepとして失敗分離する(ここが落ちても価格本体・
    #     以降のstepには一切影響しない)。
    try:
        import price_fetch
        n_adr = price_fetch.fetch_adr_pts_and_save()
        _log(f"adr_pts ok: rows={n_adr}")
    except Exception as e:
        _log(f"ERROR adr_pts failed: {e!r}")

    # 2) analyze (APIキーが無い/失敗しても後段は続ける)。collect-only では LLM を回さない。
    #    フル実行(60分毎)とcatchup(高頻度)が同時にLLM分析を行い、LM Studioへ二重に
    #    リクエストを送らないよう相互排他ロックで直列化する(price_fetch.pyの
    #    _acquire_price_lock/_acquire_analyze_lockと同じ設計・2026-08-17)。
    if collect_only:
        _log("analyze skipped (collect-only 高速サイクル=LLM非実行)")
    else:
        analyze_lock = _acquire_analyze_lock()
        # ★2026-08-21追加(おにや提案・連携ログ01:38投稿): busy/取得成功のどちらでも
        # ストリークを更新する(busyならインクリメント、取得できればリセット)。これにより
        # 「何回連続で取れていないか」がrun.log上のERROR行として可視化され、2026-08-19に
        # 実際に起きた「手動の実データ再検証でしか気づけない」状態を解消する。
        _update_lock_busy_streak("analyze", config.ANALYZE_LOCK_BUSY_STATE_PATH,
                                 was_busy=(analyze_lock is None))
        if analyze_lock is None:
            _log("WARN analyze skipped (lock busy: another run's analyze is in progress)")
        else:
            try:
                import analyze
                # ★2026-08-19(おにや11:11投稿(a)対応): catchup(10分間隔)は毎時フル実行と
                # 同じ40分予算ではなく、短い専用予算(既定4分)を明示的に渡す。詳細は
                # config.CATCHUP_ANALYZE_TIME_BUDGET_SEC / analyze.analyze()のdocstring参照。
                # ★2026-08-19(ユーザー承認済み・「10分更新に乗る情報/間に合わない情報の
                # 仕分け」): catchupは newest_first=True で新しい投稿を優先分析し、
                # 短い予算でも"直近の空気感"を公開更新に反映する。毎時フル実行は
                # newest_first=False(古い順)のままバックログを漏れなく解消する。
                _budget = config.CATCHUP_ANALYZE_TIME_BUDGET_SEC if catchup else None
                analyze_summary = analyze.analyze(time_budget_sec=_budget, newest_first=catchup)
                _log(f"analyze ok: {json.dumps(analyze_summary, ensure_ascii=False)}")
            except Exception as e:
                _log(f"ERROR analyze failed: {e!r}")
            finally:
                _release_analyze_lock(analyze_lock)

    # 3) snapshot
    try:
        import snapshot
        snap = snapshot.write_snapshot(new_count=new_count)
        _log(f"snapshot ok: day_cum={snap['day_cumulative']} "
             f"meaningful={snap['day_meaningful']} spikes={len(snap['spikes'])}")
    except Exception as e:
        _log(f"ERROR snapshot failed: {e!r}")

    # collect-onlyは以降の研究層台帳(export/forward_oos/descriptive/cluster)も公開
    #   エクスポート/Sheets同期も省く(LLM分析自体をしていないため反映すべき新規AI判定
    #   結果が無い)。これらは毎時のフル実行が担当(凍結の二重発火・intraday行のログ
    #   肥大を避ける)。
    if collect_only:
        _maybe_mirror_external()   # 生データを外付けHDへ日次ミラー(1日1回・失敗分離)
        _log(f"=== done{_mode_tag} ===")
        return 0

    # 4-13) 研究層(descriptive/cluster_trend/euphoria系の別台帳群・LLM呼び出しなし)。
    #    ★2026-08-17: このrunが既にRESEARCH_LAYER_TIME_LIMIT_SEC(既定45分)を超えて
    #    経過していたら、当runに限り丸ごと見送る安全弁(おにや17:17投稿参照)。研究層は
    #    別台帳への追記のみで次run以降に取り戻せるため、スキップしても収集/分析本体
    #    (step1-3)や公開用latest.json再生成・Sheets同期(step13.5/14・下記)には影響
    #    しない。実体は _run_research_layer() へ分離(selftestでの単体検証を容易に
    #    するため。_run_public_export_refresh_step等と同じ分離パターン)。
    #    ★2026-08-19: catchup(10分毎)は研究層を毎回スキップする(そもそも毎時フル実行
    #    が担当する設計・catchupは残債解消+公開更新の高頻度軽量サイクルに徹する)。
    if catchup:
        _log("research_layer skipped (catchup: always deferred to hourly full run)")
    else:
        _elapsed = time.time() - run_start
        if _elapsed < config.RESEARCH_LAYER_TIME_LIMIT_SEC:
            _run_research_layer()
        else:
            _log(f"research_layer skipped (elapsed={_elapsed:.0f}s >= "
                 f"RESEARCH_LAYER_TIME_LIMIT_SEC={config.RESEARCH_LAYER_TIME_LIMIT_SEC}s)")

    # 13.5-14) 公開エクスポート再生成 + Sheets同期。
    #    ★2026-08-19(おにや08:57投稿③): 従来は毎時のフル実行でしか呼ばれていなかった
    #    (=公開ダッシュボード/Sheetsの更新間隔が実質60分)。catchup(10分毎)からも
    #    呼ぶことで、新規タスクを増やさずに公開更新サイクルを10分化する。
    #    ★同投稿④: フル実行とcatchupが10分間隔で重なってこのブロックを同時実行
    #    しないよう、analyzeとは別のプロセス間ロック(_acquire_export_lock)で
    #    直列化する(この中でLM Studioを呼ぶpublic_insight生成も含めて丸ごと排他)。
    export_lock = _acquire_export_lock()
    # ★2026-08-21追加(おにや提案・連携ログ01:38投稿): analyze_lockと同型のbusyストリーク
    # 追跡をexport_lock側にも横展開。
    _update_lock_busy_streak("export", config.EXPORT_LOCK_BUSY_STATE_PATH,
                             was_busy=(export_lock is None))
    if export_lock is None:
        _log("WARN public_export_refresh/gsheets_sync skipped (export lock busy: "
             "another run's export/sheets/insight step is in progress)")
    else:
        try:
            # 13.5) public_export.py latest.json 自動再生成(欠落修正・2026-08-16)。
            #     step14(gsheets sync)はlatest.jsonを読むだけで、latest.json自体を
            #     「このrunの最新データで」再生成するstepがこれまで存在しなかった
            #     (=Sheetsに同じ古い値を書き続ける欠落)。PUBLIC_EXPORT_AUTO_REFRESH
            #     既定True(GSHEETS_SYNC_ENABLEDと同型の安全弁)。AI考察は
            #     PUBLIC_INSIGHT_BACKEND=="lmstudio"(既定・無料)の間は毎回生成、
            #     "claude"(課金経路)に切り替えている間だけ1日1回ゲートを適用する
            #     (詳細は_run_public_export_refresh_step()内・2026-08-19方針転換)。
            #     失敗分離=step7-13と同型。
            _run_public_export_refresh_step()

            # 14) Google Sheets同期(Phase 2・public_sheets_sync.py)。latest.json
            #     (公開集計値のみ)をjson_blobタブへ丸ごとJSON文字列として反映
            #     (★2026-08-20: 旧latest/trend/commentaryタブは廃止・削除済み。
            #     クラウドダッシュボードがこのタブを直接読む一本化構成)。
            #     GSHEETS_SYNC_ENABLED既定Falseの間はスキップ(Phase 1の
            #     --with-commentaryと同型=明示有効化まで安全側)。失敗分離=
            #     step7-13と同型(ここが落ちても本体の収集・記録を壊さない)。
            _run_gsheets_sync_step()
        finally:
            _release_export_lock(export_lock)

    _maybe_mirror_external()   # 生データを外付けHDへ日次ミラー(1日1回・失敗分離)
    _log(f"=== done{_mode_tag} ===")
    return 0


def _run_research_layer():
    """step4-13(研究層=descriptive/cluster_trend/euphoria系の別台帳群)の実体をmain()から
    分離した関数(selftestでの単体検証を容易にするため。_run_public_export_refresh_step()
    と同じ分離パターン)。2026-08-17: main()側で経過時間がRESEARCH_LAYER_TIME_LIMIT_SECを
    超えた場合はこの関数自体を呼ばずスキップする安全弁を追加(おにや17:17投稿=Yahoo分析
    バックログ対応)。ここに含まれる各step間で共有される外部変数は無い(呼び出しごとに
    raw_rows/an_rows等を都度読み直す設計=既存挙動を変えないための意図的な非効率)。"""
    # 4) 研究層エクスポート + forward-OOS 凍結(既存挙動は不変=追記のみ・失敗分離)
    #    量/語彙は raw 全件、強弱比は analyzed。cutoff は壁時計から自動判定。
    try:
        import signals as sigmod
        import price_fetch
        import export_signal
        import forward_oos
        raw_rows = _parse_raw_rows_resilient(_iter_raw())
        pd_daily = price_fetch.load_price(config.PRICE_DAILY_PATH)
        pd_intra = price_fetch.load_price(config.PRICE_INTRADAY_PATH)
        # analyzed 全件(dedupe)
        an_rows = _load_analyzed()
        sig = sigmod.compute_signals(an_rows, raw_rows=raw_rows,
                                     price_daily=pd_daily, price_intraday=pd_intra)
        cutoff = _cutoff_kind()
        # data_health: 収集段の結果メタ(既存のみ・新規API非取得)から導出して渡す。
        #   page_cap_hit=YAHOO_MAX_PAGES到達(未取得の新しい投稿が残存)=faithful結線。
        #   stale=当runの一次収集(yahoo)が新鮮な取得に失敗(parsed==0/メタ無し)の保守シグナル。
        data_health = _collect_data_health()
        rec = export_signal.write_from_signals(sig, raw_rows, cutoff=cutoff,
                                               data_health=data_health)
        _log(f"export ok: cutoff={cutoff} regime={rec['vol_regime']} "
             f"calib={rec['calibration_status']}(n={rec['calib_days']})")
        # 引け後(close_consolidated)のみ日次OOS行を凍結。加えて毎runで成熟行を settle。
        if cutoff == "close_consolidated":
            forward_oos.freeze_daily_row(sig, raw_rows, pd_daily, cutoff=cutoff)
        settled = forward_oos.settle_matured_rows(pd_daily)
        _log(f"forward_oos ok: settled={settled}")
    except Exception as e:
        _log(f"ERROR export/forward_oos failed: {e!r}")

    # 5) 日次記述統計台帳(純記述のみ・発注なし・別台帳・append-only。失敗分離=他段に影響しない)
    #    フォワード/損益/点推定/昇格判定なし。dense蓄積前でも当日から傾向を可視化。
    try:
        import os as _os
        from research import run_study as _run_study
        raw_rows = _parse_raw_rows_resilient(_iter_raw())
        an_rows = _load_analyzed()
        snaps = []
        if _os.path.exists(config.SNAPSHOTS_PATH):
            # ★2026-08-19修正(おにや22:13投稿・重大障害調査の横展開): torn write対策
            # (バイナリモード+行ごと個別decode)。1行分のバイト破損で以降の行が全滅しない。
            with open(config.SNAPSHOTS_PATH, "rb") as f:
                for raw_line in f:
                    try:
                        line = raw_line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        continue
                    if not line:
                        continue
                    try:
                        snaps.append(json.loads(line))
                    except Exception:
                        continue
        drows = _run_study.descriptive_daily(raw_rows, an_rows, snaps)
        added = _run_study.write_descriptive_ledger(drows)
        _log(f"descriptive ok: days={len(drows)} appended={added}")
    except Exception as e:
        _log(f"ERROR descriptive failed: {e!r}")

    # 6) 柱4: 話題クラスタ日次台帳(別台帳・append-only・冪等=同日再実行で重複しない。失敗分離)
    try:
        import cluster_trend
        import price_fetch as _pf
        an_rows = _load_analyzed()
        pd_daily = _pf.load_price(config.PRICE_DAILY_PATH)
        crec, cadded = cluster_trend.save_daily(an_rows, pd_daily)
        _log(f"cluster_trend ok: date={crec.get('date')} appended={cadded}")
    except Exception as e:
        _log(f"ERROR cluster_trend failed: {e!r}")

    # 7) 掲示板×株価 記述連動の日次追跡(別台帳・append-only・冪等・シグナル非feed。
    #    記述で昇格でない=コーディネーターGO(2026-07-11・研究レーン既定OFF)。失敗分離)
    if config.BBS_LINKAGE_DAILY:
        try:
            from research import intraday_linkage
            lres = intraday_linkage.append_daily()
            _log(f"linkage ok: skipped={lres['skipped']} "
                 f"n={lres['row'].get('n_intervals')} status={lres['row'].get('status')}")
        except Exception as e:
            _log(f"ERROR intraday_linkage failed: {e!r}")

    # 8) 小口chase/capitulation記述台帳(凍結定義v1 spec="rc1"・別台帳・append-only・冪等・
    #    シグナル非feed。記述で昇格でない。失敗分離=step7と同型)
    if config.BBS_CHASE_DAILY:
        try:
            from research import retail_chase
            cres = retail_chase.append_daily()
            _log(f"retail_chase ok: daily+{cres['daily_appended']} "
                 f"buckets+{cres['buckets_appended']} days={cres['n_days']}")
        except Exception as e:
            _log(f"ERROR retail_chase failed: {e!r}")

    # 9) capit_v2(level成分)のA1前向き検証を毎日積む(A1 `research/A1_capit_level_2026-07-16.md`)。
    #    別台帳(retail_chase_v2_285A.jsonl)・append-only・冪等・シグナル非feed・記述で昇格でない。
    #    現行 capit(step8/rc2)の定義と台帳には一切触れない(A1 §6)。
    #    is_oos境界 date>2026-07-16 は capit_v2_level 側が持つ(A1 §5)。
    #    失敗分離=step7/8と同型(ここが落ちても本体の収集・記録を壊さない)。
    if getattr(config, "BBS_CAPIT_V2_DAILY", False):
        try:
            from research import capit_v2_level
            wrote, built = capit_v2_level.append_daily()
            _log(f"capit_v2_level ok: rows+{wrote} built={built}")
        except Exception as e:
            _log(f"ERROR capit_v2_level failed: {e!r}")

    # 10) vote_ratio(点時刻票・確信度)→+2バケット出来高 の PITクリーン前向き検証(A1 vote_ratio確信度)。
    #     別台帳(votes_pit_285A.jsonl)・append-only・冪等・シグナル非feed・記述で昇格でない。
    #     PIT専用アグリゲータ=raw_comments累積票を使わず votes_snapshot.jsonl を as-of再構成。
    #     真OOS(date>2026-07-11)が15取引日に満たない間は評価不能(点推定を判定に使わない)。
    #     凍結台帳 forward_sentiment_285A と spec_hash には触れない。失敗分離=step7-9と同型。
    if getattr(config, "BBS_VOTES_PIT_EVAL", False):
        try:
            from research import votes_pit_eval
            vres = votes_pit_eval.append_daily()
            vrow = vres["row"]
            _log(f"votes_pit ok: {'skip' if vres['skipped'] else 'append'} "
                 f"oos={vrow.get('n_oos_days')}/15 pairs+2={vrow.get('n_pairs_lag2')} "
                 f"des_ic2={vrow.get('deseason_ic_lag2')} status={vrow.get('status')}")
        except Exception as e:
            _log(f"ERROR votes_pit_eval failed: {e!r}")

    # 11) 半導体ピア(SanDisk/Micron/SK Hynix)→285A クロス市場オーバーナイト・リード(A1半導体ピア)。
    #     別台帳(sector_peer_lead_285A.jsonl)・append-only・冪等・シグナル非feed・記述で昇格でない。
    #     ①ピア価格C-Cリターン→翌285A取引日の寄りギャップ(as-of先読み排除)②MU/SNDK sentiment(前向き・vol側)。
    #     ネット取得(Yahoo/StockTwits)を伴うので失敗分離必須(取得失敗でも本体を止めない)。
    #     真OOS(date>2026-07-19)15取引日未満は評価不能。凍結台帳 forward_sentiment_285A 非接触。
    if getattr(config, "SECTOR_PEER_LEAD_DAILY", False):
        try:
            from research import sector_peer_lead
            pres = sector_peer_lead.append_daily()
            prow = pres["row"]
            _log(f"sector_peer_lead ok: {'skip' if pres['skipped'] else 'append'} "
                 f"range={prow.get('date_range')} oos={prow.get('n_oos_days_primary')}/15 "
                 f"seed_ic_SNDK_gap={prow.get('seed_ic_SNDK_gap')} status={prow.get('status')}")
        except Exception as e:
            _log(f"ERROR sector_peer_lead failed: {e!r}")

    # 12) 掲示板"注目"の相対(trailing平均比・増減対称)→翌日285A vol/出来高(A1 掲示板注目相対)。
    #     forward_sentiment_285A.csv を read専用で読み log_rel_W/decay_W を計算・別台帳へ日次append。
    #     level(posts_z_dense)への上乗せ増分で採否・方向不使用・凍結台帳非接触・失敗分離・非feed・冪等。
    #     真OOS(date>2026-07-19)15取引日未満は評価不能。
    if getattr(config, "ATTENTION_RELATIVE_DAILY", False):
        try:
            from research import attention_relative
            ares = attention_relative.append_daily()
            arow = ares["row"]
            _log(f"attention_relative ok: {'skip' if ares['skipped'] else 'append'} "
                 f"range={arow.get('date_range')} oos={arow.get('n_oos_days_primary')}/15 "
                 f"seed_ic_logrel_vol={arow.get('seed_ic_logrel_vol')} status={arow.get('status')}")
        except Exception as e:
            _log(f"ERROR attention_relative failed: {e!r}")

    # 13) 場後(15:00-23:59:59 JST)弱気投稿→翌営業日リターン 記述追跡(丸山ほか2008)。
    #     別台帳(afterhours_bearish_285A.jsonl)・append-only(1日1行更新)・冪等・シグナル非feed・
    #     記述で昇格でない。日次append(このrunまでの場後投稿数を反映) + 成熟行の
    #     next_day_return後埋め(settle)の両方を毎run実行。price_dailyはこのstep専用に読み直す
    #     (他stepの変数は変更しない)。凍結台帳 forward_sentiment_285A と spec_hash には
    #     一切触れない。失敗分離=step7-12と同型。
    if getattr(config, "BBS_AFTERHOURS_BEARISH_DAILY", False):
        try:
            from research import afterhours_bearish
            import price_fetch as _pf_ab
            pd_daily_ab = _pf_ab.load_price(config.PRICE_DAILY_PATH)
            ab_res = afterhours_bearish.append_daily(price_daily=pd_daily_ab)
            ab_settled = afterhours_bearish.settle_matured_rows(price_daily=pd_daily_ab)
            ab_row = ab_res["row"]
            _log(f"afterhours_bearish ok: {'skip' if ab_res['skipped'] else 'append'} "
                 f"date={ab_row.get('date')} bear_ratio={ab_row.get('afterhours_bear_ratio')} "
                 f"settled={ab_settled}")
        except Exception as e:
            _log(f"ERROR afterhours_bearish failed: {e!r}")


def _update_commentary_failure_streak(succeeded):
    """★2026-08-20追加(ユーザー提案「AI考察生成の失敗が静かに握りつぶされないように」)。
    AI考察生成の連続失敗回数をconfig.AI_COMMENTARY_FAILURE_STATE_PATHへ永続化し、
    更新後の値を返す。ロジック本体(次の値をどう決めるか)はpublic_export側の純関数
    (next_commentary_failure_streak)に委譲し、ここでは状態ファイルの読み書きだけを
    行う。fail-soft(読み書き失敗時はストリークをリセット扱いにして警告を出さない
    安全側=誤検知よりも見逃しの方がまだましという判断)。"""
    import public_export
    path = config.AI_COMMENTARY_FAILURE_STATE_PATH
    prev = 0
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                prev = (json.load(f) or {}).get("streak", 0)
    except Exception:
        prev = 0
    streak = public_export.next_commentary_failure_streak(prev, succeeded)
    try:
        config.ensure_data_dir()
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"streak": streak,
                      "updated_at": dt.datetime.now().isoformat(timespec="seconds")}, f)
        os.replace(tmp, path)
    except Exception:
        pass
    # ★2026-08-21修正(実障害調査で発覚): 元々この関数にreturn文が無く、呼び出し元
    # (_run_public_export_refresh_step)がstreak変数へNoneを受け取り、続く
    # `streak >= config.AI_COMMENTARY_FAILURE_WARN_THRESHOLD`比較でTypeErrorが
    # 発生していた(2026-08-20の新設時からlmstudio backendでは毎run発生し続けていた
    # =with_commentary=Trueが常に成立するため)。この例外自体は_run_public_export_refresh_step
    # 側のtry/exceptで握りつぶされ、latest.json自体は例外より前に書き込み済みのため
    # 実害(株価/AI考察の更新停止)は無かったが、run.logへ無関係なERROR行が毎回混入し
    # 続けており、かつストリーク閾値超過アラート自体も一度も機能していなかった
    # (streak>=thresholdの比較そのものが毎回例外で落ちるため)。
    return streak


def _update_lock_busy_streak(lock_name, state_path, was_busy):
    """★2026-08-21追加(おにや提案・連携ログ01:38投稿への対応)。
    analyze_lock/export_lockの取得busyの連続回数をstate_pathへ永続化し、更新後の値を
    返す。ロジック本体はpublic_export.next_lock_busy_streak()に委譲、ここでは
    _update_commentary_failure_streak()と全く同じ設計(状態ファイルの読み書きのみ・
    fail-soft=読み書き失敗時はストリークをリセット扱いにし警告を出さない=誤検知より
    見逃しの方がまだましという判断)。lock_nameはログ用のラベル("analyze"/"export")。"""
    import public_export
    prev = 0
    try:
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                prev = (json.load(f) or {}).get("streak", 0)
    except Exception:
        prev = 0
    streak = public_export.next_lock_busy_streak(prev, was_busy)
    try:
        config.ensure_data_dir()
        tmp = f"{state_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"streak": streak, "lock": lock_name,
                      "updated_at": dt.datetime.now().isoformat(timespec="seconds")}, f)
        os.replace(tmp, state_path)
    except Exception:
        pass
    if streak >= config.LOCK_BUSY_WARN_THRESHOLD:
        _log(f"ERROR {lock_name}_lock busy {streak} times in a row "
             f"(threshold={config.LOCK_BUSY_WARN_THRESHOLD}) — "
             f"long-running lock holder suspected, investigate manually")
    return streak


def _run_public_export_refresh_step():
    """step13.5の実体をmain()から分離した小関数(selftestでの単体検証を容易にするため。
    _run_gsheets_sync_step()と同じ分離パターン)。

    PUBLIC_EXPORT_AUTO_REFRESH(既定True・config.py)がFalseの間はpublic_exportを
    importすらせずスキップする。有効時は public_export._build_from_live_data() を呼び、
    latest.json/history.jsonlをこのrunの最新データで再生成する
    (write_public_export()内部はfail-closed=漏洩検出時は例外送出。ここでの例外捕捉は
    それも含めた保険=他stepに影響させない)。

    AI考察(ai_commentary)の生成可否は config.PUBLIC_INSIGHT_BACKEND で分岐する
    (★2026-08-19方針転換・おにや08:57投稿②)。
      - "lmstudio"(既定・ローカルLLM=無料): 1日1回ゲートを適用せず、このstepが
        呼ばれるたび毎回 with_commentary=True で生成する(catchup10分毎からも
        呼ばれるため、公開ダッシュボードのAI考察が高頻度で更新される)。
      - "claude"(Opus API=課金): 従来通りPUBLIC_EXPORT_COMMENTARY_DAILY(既定True)が
        有効な間だけ、public_export.should_generate_commentary_today()の冪等ゲート
        (JST当日分がlatest.json/history.jsonlのai_commentary.generated_atに
        見当たらなければTrue)で判定し、今日まだ生成していなければこのrunに限り
        with_commentary=True で呼ぶ(課金経路のみ1日1回に抑える安全弁は維持)。"""
    if not getattr(config, "PUBLIC_EXPORT_AUTO_REFRESH", True):
        _log("public_export_refresh skipped (PUBLIC_EXPORT_AUTO_REFRESH=False)")
        return
    try:
        import public_export
        if getattr(config, "PUBLIC_INSIGHT_BACKEND", "lmstudio") == "lmstudio":
            with_commentary = True   # 無料のローカルLLM=毎回生成・ゲート無し
        else:
            with_commentary = False
            if getattr(config, "PUBLIC_EXPORT_COMMENTARY_DAILY", True):
                with_commentary = public_export.should_generate_commentary_today()
        rec = public_export._build_from_live_data(with_commentary=with_commentary)
        has_commentary = "ai_commentary" in rec
        _log(f"public_export_refresh ok: generated_at={rec.get('generated_at')} "
             f"with_commentary_requested={with_commentary} "
             f"has_commentary={has_commentary}")
        # ★2026-08-20追加(ユーザー提案「AI考察生成の失敗が静かに握りつぶされない
        # ように」): with_commentary=True で要求したのに ai_commentary が付かなかった
        # 場合だけを「失敗」としてカウントする(with_commentary=False=そもそも今回は
        # 生成を試みていない、は失敗にもリセットにもしない=既存のゲート挙動に影響
        # させない)。
        if with_commentary:
            streak = _update_commentary_failure_streak(succeeded=has_commentary)
            if streak >= getattr(config, "AI_COMMENTARY_FAILURE_WARN_THRESHOLD", 3):
                _log(f"ERROR ai_commentary failed {streak} times in a row "
                    f"(threshold={config.AI_COMMENTARY_FAILURE_WARN_THRESHOLD}). "
                    f"backend={getattr(config, 'PUBLIC_INSIGHT_BACKEND', '?')} "
                    f"の状態(LM StudioサーバーやAPI疎通)を確認してください。")
    except Exception as e:
        _log(f"ERROR public_export_refresh failed: {e!r}")


def _run_gsheets_sync_step():
    """step14の実体をmain()から分離した小関数(selftestでの単体検証を容易にするため)。
    GSHEETS_SYNC_ENABLED(既定False・config.py)がFalseの間はpublic_sheets_syncを
    importすらせずスキップする。有効時はwrite_to_sheets()自体がfail-soft(例外を
    投げずFalseを返す)なので、ここでの例外捕捉は import 失敗等への保険。"""
    if not getattr(config, "GSHEETS_SYNC_ENABLED", False):
        _log("gsheets_sync skipped (GSHEETS_SYNC_ENABLED=False)")
        return
    try:
        import public_sheets_sync
        ok = public_sheets_sync.write_to_sheets()
        _log(f"gsheets_sync {'ok' if ok else 'failed (fail-soft, see public_sheets_sync log)'}")
    except Exception as e:
        _log(f"ERROR gsheets_sync failed: {e!r}")


def _iter_raw():
    """★2026-08-19修正(おにや22:13投稿・重大障害調査): 従来はテキストモード
    (`encoding="utf-8"`)で`for line in f`と行ごと反復していたため、torn write
    (catchup等の並行書き込みが1行の途中で分断されマルチバイトUTF-8文字が壊れる
    既知の競合パターン。_parse_raw_rows_resilient()のdocstring参照)に遭遇すると
    Pythonのファイルイテレータ自体がUnicodeDecodeErrorを送出し、以降の全行が
    読めなくなっていた(実際にexport/forward_oos・descriptive等の研究層stepが
    2026-08-19 17:42〜22:10の間このエラーで繰り返し失敗し続けていたことを実測で
    確認)。_parse_raw_rows_resilient()自体は「1行がJSONとして壊れている」ケースには
    対応済みだったが、この「バイト列そのものが壊れている」ケースには対応して
    いなかった(このファイルの前段=ここでクラッシュしていたため、後段の
    resilient処理まで到達できていなかった)。
    対策: バイナリモードで読み、行(bytes)ごとに個別にdecodeする。1行だけ不正
    バイト列でも、その行だけをスキップして次行から反復を継続できる。"""
    import os
    p = config.RAW_COMMENTS_PATH
    if not os.path.exists(p):
        return []
    lines = []
    with open(p, "rb") as f:
        for raw_line in f:
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue  # torn write等でバイト列が壊れた1行だけスキップし継続
            if line.strip():
                lines.append(line)
    return lines


def _parse_raw_rows_resilient(lines):
    """★2026-08-19追加(おにや11:11投稿(c)対応): raw_comments.jsonlの各行をJSON parse
    する際、1行でも壊れていたら例外を伝播させて研究層のstep全体(export/forward_oos・
    descriptive)を丸ごと失敗させていた問題への対策。実データで
    `JSONDecodeError('Extra data: line 1 column 2 (char 1)')`が本日複数回発生し原因調査
    したところ、raw_comments.jsonlはcollect_yahoo等が10分毎(catchup)/60分毎(フル実行)に
    追記し続けている**共有の追記専用ファイル**であり、フル実行の研究層がこのファイルを
    読み込む瞬間に別プロセス(catchup等)の追記(1行分のwrite)がちょうど部分的にしか
    flushされていないタイミングと重なると、その1行だけが torn write(不完全な書き込み)
    になり得ることが分かった(おにやの当初の帰属「catchupが原因でエラーが起きる」は
    不正確=正しくはフル実行の"読み"とcatchup等の"書き"が稀に競合するタイミング問題)。
    この関数は壊れた行を1行ずつスキップして残りは正常に処理する(全滅させない)。
    スキップされた行は次runで再度追記済みの状態として読める(append-only=消えない)ため、
    データ損失にはならない(その回のexport/descriptive計算に1行分だけ反映が遅れるのみ)。"""
    rows = []
    n_bad = 0
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            n_bad += 1
    if n_bad:
        _log(f"WARN _parse_raw_rows_resilient: skipped {n_bad} malformed raw line(s) "
             f"(likely a torn concurrent write; not a data loss - will be readable next run)")
    return rows


def _load_analyzed():
    """analyzed.jsonl を id 重複排除して読む(snapshotと同じ扱い)。"""
    import os
    p = config.ANALYZED_PATH
    if not os.path.exists(p):
        return []
    by_id, order = {}, []
    # ★2026-08-19修正(おにや22:13投稿・重大障害調査の横展開): torn write対策
    # (バイナリモード+行ごと個別decode)。analyzed.jsonlもanalyze()が毎run追記する
    # 共有ファイルのため、他の共有jsonl読込と同じ対策を適用する。
    with open(p, "rb") as f:
        for raw_line in f:
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rid = r.get("id")
            if rid is None:
                continue
            if rid not in by_id:
                order.append(rid)
            by_id[rid] = r
    return [by_id[i] for i in order]


def _collect_data_health():
    """
    収集段(collect_yahoo.LAST_COLLECT_META)から export用 data_health を導出。新規API非取得。
      - page_cap_hit: yahoo内部APIが YAHOO_MAX_PAGES に到達=未取得の新しい投稿が残存(faithful)。
      - stale: 当runで一次収集(yahoo)が新鮮な取得に失敗(parsed==0 or メタ無し)の保守的シグナル。
    ここで結線するのは上記2つのみ。より細粒度の鮮度(最新投稿ts vs 壁時計JST)は【未接続】=
    build_export_record 側の既定(False)のまま(garbled_dropped/dense_session は同関数が別途計算)。
    """
    try:
        import collect_yahoo
        meta = getattr(collect_yahoo, "LAST_COLLECT_META", None) or {}
    except Exception:
        meta = {}
    if not meta:
        # メタ無し=このrunで一次収集が走っていない/失敗 → 保守的に stale とみなす。
        return {"stale": True, "page_cap_hit": False}
    return {
        "stale": bool(meta.get("parsed", 0) == 0),
        "page_cap_hit": bool(meta.get("page_cap_hit")),
    }


def _cutoff_kind():
    """壁時計(JST)から cutoff 種別を決める。schtaskの意味は変えない(追記のみ)。"""
    h = dt.datetime.now().hour
    if h == 13:
        return "13:00_decision"
    if h >= 15:
        return "close_consolidated"
    return "intraday_rolling"


if __name__ == "__main__":
    sys.exit(main(collect_only=("--collect-only" in sys.argv),
                   catchup=("--catchup" in sys.argv)))
