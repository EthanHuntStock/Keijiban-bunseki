# -*- coding: utf-8 -*-
"""
mojibake_cache.py - is_mojibake() 判定結果の id 単位永続キャッシュ。

背景(cProfile実測): signals.py::clean_rows() は compute_signals() が呼ばれる
たびに全行(現在約59万件)へ is_mojibake(text) を再計算していた(実測 約6.5秒)。
is_mojibake は text のみに依存する純関数で、同じ id のテキストは書込み後不変
(raw_comments.jsonl/analyzed.jsonl は追記専用)なので、id をキーに一度だけ判定
すれば以降は引くだけでよい。

安全方針(厳守):
  - raw_comments.jsonl/analyzed.jsonl 等の既存の追記専用ログは一切読み書きしない。
    このモジュールが触るのは config.DATA_DIR 配下の独立した副次ファイル
    (mojibake_cache.json)のみ。
  - 読込は fail-soft: ファイル無し/壊れていても例外を出さず空dictにフォールバック。
  - 書込みは tmp ファイル -> os.replace の atomic 保存(dashboard.py の
    _save_ui_settings と同じパターン)。新規追加が無ければ書込み自体をスキップ。
  - 同時実行(dashboard.py の手動閲覧と snapshot.py の定期バッチが並走しうる)を
    想定し、キャッシュ層のどんな異常(壊れたJSON・書込み失敗)も本体の
    is_mojibake 計算そのものは止めない(その場で計算するだけに縮退)。
  - id 単位で不変ゆえ TTL/失効は無し。ただし際限ない肥大化を避けるため、
    キャッシュ件数が「今回呼ばれた rows の id 数」の1.5倍を超えたら、現存しない
    id を間引く簡易プルーニングを行う(優先度低・ベストエフォート)。
"""
import os
import json

import config
from collect_yahoo import is_mojibake


CACHE_FILENAME = "mojibake_cache.json"


def _cache_path():
    """毎回 config.DATA_DIR から導出(テストでの差し替え/実行時変更にも追従)。"""
    return os.path.join(config.DATA_DIR, CACHE_FILENAME)


def load_cache():
    """キャッシュを読み込む。無ければ/壊れていれば空dict(fail-soft・例外を出さない)。"""
    try:
        p = _cache_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _save_cache(cache):
    """atomic保存(tmp -> os.replace)。fail-soft(失敗しても例外を外に出さない)。"""
    try:
        config.ensure_data_dir()
        p = _cache_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def _prune_if_bloated(cache, live_keys):
    """キャッシュ件数が今回の live_keys 数の1.5倍を超えたら、現存しない id を
    一括削除する簡易間引き(優先度低・ベストエフォート)。live_keys が空なら何もしない。"""
    try:
        if live_keys and len(cache) > 1.5 * len(live_keys):
            for k in list(cache.keys()):
                if k not in live_keys:
                    del cache[k]
    except Exception:
        pass
    return cache


def get_or_compute(rows, cache=None):
    """rows(各行 dict、'id' と 'text' を持つ想定)の is_mojibake 判定結果を
    id 単位でキャッシュ経由で返す。

    戻り値: {str(id): bool} (今回の rows に含まれる id のみ・id無し行は含まない)。
    id が無い行はキャッシュに乗せず、呼び出し側がその場で is_mojibake() を
    計算する(fail-soft・呼び出し側=signals.clean_rows で対応)。

    cache=None なら関数内でロードする。新規計算(キャッシュミス)が1件でも
    発生した場合のみ、関数の最後で1回だけ atomic 保存する。ヒットのみなら
    保存処理自体をスキップし、無駄な I/O を避ける。
    """
    owns_cache = cache is None
    if owns_cache:
        cache = load_cache()

    result = {}
    dirty = False
    live_keys = set()
    for r in rows or []:
        rid = r.get("id")
        if rid is None:
            continue
        key = str(rid)
        live_keys.add(key)
        if key in cache:
            result[key] = cache[key]
        else:
            v = is_mojibake(r.get("text", ""))
            cache[key] = v
            result[key] = v
            dirty = True

    if owns_cache:
        if dirty:
            _prune_if_bloated(cache, live_keys)
            _save_cache(cache)

    return result
