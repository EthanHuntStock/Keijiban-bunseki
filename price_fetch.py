# -*- coding: utf-8 -*-
"""
price_fetch.py - Yahoo Finance chart API から 285A の価格を取得して保存。

- 日足: interval=1d&range=6mo(実測~122本)
- 日中: interval=5m&range=5d(実測~388本。closeがnullの昼休みスロットは除外。
  末尾バーはvolume=0の現在値合成バーの場合あり=そのまま保持し必要なら呼び手が判断)
- Chrome UA 必須(デフォルトUAは403/429リスク)。`chart.error` をチェック。
- 失敗は warn して続行(fail-soft)。dashboard は保存済みJSONを読むだけ(読み取り専用維持)。

純関数(parse_chart_json)はネット非依存=selftest対象。
"""
import json
import re
import datetime as dt

import config


# ---- 純関数: パース ----------------------------------------------------------
def parse_chart_json(data):
    """
    Yahoo chart API の応答dict -> {meta:{...}, bars:[{ts,open,high,low,close,volume,adjclose}]}
    close が None のスロット(昼休み等)は除外。エラー応答/構造欠損は {'error': msg}。
    純関数(ネット非依存)。
    """
    if not isinstance(data, dict):
        return {"error": "not a dict"}
    chart = data.get("chart") or {}
    err = chart.get("error")
    if err:
        return {"error": str(err)}
    results = chart.get("result") or []
    if not results or not isinstance(results[0], dict):
        return {"error": "no result"}
    res = results[0]
    ts = res.get("timestamp") or []
    ind = res.get("indicators") or {}
    quotes = (ind.get("quote") or [{}])[0] or {}
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose") or []
    meta = res.get("meta") or {}

    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    vols = quotes.get("volume") or []

    bars = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue  # 昼休み等のnullスロット除外
        bars.append({
            "ts": int(t),
            "open": opens[i] if i < len(opens) else None,
            "high": highs[i] if i < len(highs) else None,
            "low": lows[i] if i < len(lows) else None,
            "close": c,
            "volume": (vols[i] if i < len(vols) else None) or 0,
            "adjclose": adj[i] if i < len(adj) else None,
        })
    out_meta = {
        "symbol": meta.get("symbol"),
        "currency": meta.get("currency"),
        "regularMarketPrice": meta.get("regularMarketPrice"),
        "previousClose": meta.get("previousClose"),
        "chartPreviousClose": meta.get("chartPreviousClose"),
        "gmtoffset": meta.get("gmtoffset"),
        "timezone": meta.get("timezone"),
        "dataGranularity": meta.get("dataGranularity"),
        "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
    }
    return {"meta": out_meta, "bars": bars}


def latest_price_change(parsed_intraday):
    """
    parse済み日中データ -> (現値, 前日終値, 変化率%) 。無ければ (None,None,None)。純関数。
    """
    if not parsed_intraday or parsed_intraday.get("error"):
        return None, None, None
    meta = parsed_intraday.get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None:
        bars = parsed_intraday.get("bars") or []
        price = bars[-1]["close"] if bars else None
    if price is None or not prev:
        return price, prev, None
    return price, prev, round((price - prev) / prev * 100.0, 2)


# ============================================================================
# PTS(夜間取引)・米国ADR円換算(★2026-08-19追加・ユーザー依頼)
# ============================================================================
# nikkei225jp.com の ADR/PTS フィードは `var ADRm = [[ts_ms,tse,pts,adr_yen,adr_usd],...];`
# というJS配列リテラル(要素が空欄=その時点でその項目の取引が無かったことを意味し、
# JSONのnullとは書かれず単に空文字になる=json.loadsでは読めない)。1行1レコードずつ
# 正規表現で抜き出す。列の意味は実測(2026-08-19 22時台)で確認済み:
#   [0]=UNIX epoch ms(UTC) [1]=TSE現在値(取引時間外は空欄) [2]=PTS株価
#   [3]=ADR円換算(285Aと同じ円建てで直接比較できる値=ADR株価×為替×ADR比率換算済み)
#   [4]=ADR株価(USD建て)
_ADR_PTS_ROW_RE = re.compile(r"\[(\d+),([^,\]]*),([^,\]]*),([^,\]]*),([^,\]]*)\]")


def parse_adr_pts_js(text):
    """nikkei225jp.com の ADR/PTSフィード(JS配列リテラルのテキスト)をパースする純関数。
    ネット非依存。戻り値: [{ts(UNIX秒,UTC), tse, pts, adr_yen, adr_usd}, ...]
    (時系列順・空欄はNoneのまま保持=捏造しない)。構造異常/空文字は空リストを返す
    (fail-soft)。
    """
    if not text:
        return []

    def _num(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    rows = []
    for m in _ADR_PTS_ROW_RE.finditer(text):
        ts_ms, a, b, c, d = m.groups()
        try:
            ts = int(ts_ms) // 1000
        except ValueError:
            continue
        rows.append({"ts": ts, "tse": _num(a), "pts": _num(b),
                     "adr_yen": _num(c), "adr_usd": _num(d)})
    return rows


def fetch_adr_pts_and_save(symbol=None):
    """nikkei225jp.com からPTS/ADR円換算フィードを取得し config.ADR_PTS_PATH へ保存。
    fail-soft(失敗時は0を返し例外を投げない・既存ファイルは保持=書き換えない)。
    Yahoo Finance API(285A.T)とは完全に別ドメイン・別ファイルのため、価格ロック
    (_acquire_price_lock)は使わず独立して呼べる(呼び手=run_once.pyは価格取得の
    直後に独自stepとして呼び、失敗しても他stepへ影響しない設計にする)。
    """
    import requests
    symbol = symbol or config.SYMBOL
    url = config.ADR_PTS_URL.format(symbol=symbol)
    try:
        r = requests.get(url, headers={"User-Agent": config.CHROME_UA,
                                       "Referer": config.ADR_PTS_REFERER.format(symbol=symbol)},
                         timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            _log(f"WARN adr_pts http {r.status_code} (kept previous file)")
            return 0
        text = r.text
    except Exception as e:
        _log(f"WARN adr_pts fetch failed: {e!r} (kept previous file)")
        return 0
    rows = parse_adr_pts_js(text)
    if not rows:
        _log("WARN adr_pts: no rows parsed (kept previous file)")
        return 0
    import os
    out = {"symbol": symbol, "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
          "rows": rows}
    tmp = f"{config.ADR_PTS_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, config.ADR_PTS_PATH)
    _log(f"adr_pts: saved {len(rows)} rows")
    return len(rows)


# ---- I/O / ネットワーク ------------------------------------------------------
def _log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] price: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _fetch(params):
    import requests
    url = f"{config.PRICE_CHART_URL}?{params}"
    try:
        r = requests.get(url, headers={"User-Agent": config.CHROME_UA,
                                        "Accept": "application/json"},
                          timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            _log(f"WARN http {r.status_code} for {params}")
            return None
        return r.json()
    except Exception as e:
        _log(f"WARN fetch failed ({params}): {e!r}")
        return None


_PRICE_LOCK_STALE_SEC = 120   # このtmp秒数を超えたlockはクラッシュ残骸とみなし強制解除


def _acquire_price_lock(timeout_sec=25):
    """フル実行(毎時)とcollect-only(5分毎)が同時に価格ファイルへ書き込みを試みて
    PermissionErrorを起こす競合(2026-08-17おにや19:15報告)を防ぐプロセス間ロック。
    ロックファイルの排他生成(O_CREAT|O_EXCL、Windows/POSIX双方でアトミック)で実装。
    取得できるまで短間隔でリトライし、timeout_sec超過時は諦めてNoneを返す(fail-soft=
    価格更新1回を見送るだけで、次のサイクルで取り戻せる。収集/分析本体は止めない)。
    stale-lock対策: 前回プロセスがクラッシュしてロックファイルが残った場合に永久デッド
    ロックしないよう、_PRICE_LOCK_STALE_SEC超の古いロックは強制解除して取り直す。"""
    import os
    import time as _time
    lock_path = os.path.join(config.DATA_DIR, ".price_fetch.lock")
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
                if age > _PRICE_LOCK_STALE_SEC:
                    os.remove(lock_path)   # stale=前回プロセスのクラッシュ残骸とみなし強制解除
                    continue
            except OSError:
                pass   # 他プロセスがちょうど解放した等=次のループで再試行
            if _time.time() >= deadline:
                return None
            _time.sleep(0.2)


def _release_price_lock(lock_path):
    if not lock_path:
        return
    import os
    try:
        os.remove(lock_path)
    except OSError:
        pass


def fetch_and_save():
    """
    日足+日中を取得しパースして data/price_daily.json / price_intraday.json に保存。
    fail-soft: 片方失敗しても他方は保存。戻り値: (daily_bars, intraday_bars) 件数。
    フル実行とcollect-onlyサイクルが同時に走ってもファイル競合しないよう、
    プロセス間ロック(_acquire_price_lock)で書き込み区間を排他制御する。
    """
    config.ensure_data_dir()
    lock_path = _acquire_price_lock()
    if lock_path is None:
        _log("WARN price fetch skipped (lock busy: another run is writing price files)")
        return 0, 0
    try:
        fetched_at = dt.datetime.now().isoformat(timespec="seconds")
        n_d = n_i = 0
        import os

        for name, params, path in [
            ("daily", config.PRICE_DAILY_PARAMS, config.PRICE_DAILY_PATH),
            ("intraday", config.PRICE_INTRADAY_PARAMS, config.PRICE_INTRADAY_PATH),
            ("minute", config.PRICE_1M_PARAMS, config.PRICE_1M_PATH),   # 狭幅用1分足
        ]:
            data = _fetch(params)
            if data is None:
                _log(f"WARN {name}: no data (kept previous file)")
                continue
            parsed = parse_chart_json(data)
            if parsed.get("error"):
                _log(f"WARN {name}: {parsed['error']} (kept previous file)")
                continue
            parsed["fetched_at"] = fetched_at
            # tmpファイル名にPIDを含める(ロックで直列化済みだが、念のための多重防御=
            # 万一ロックをすり抜けても異プロセス間でtmpファイル名が衝突しない)。
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False)
            os.replace(tmp, path)
            n = len(parsed["bars"])
            if name == "daily":
                n_d = n
            elif name == "intraday":
                n_i = n
            _log(f"{name}: saved {n} bars")
        return n_d, n_i
    finally:
        _release_price_lock(lock_path)


def load_price(path):
    """保存済み価格JSONを読む(dashboard/signals用・読み取り専用)。無ければ None。"""
    import os
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


if __name__ == "__main__":
    d, i = fetch_and_save()
    print(f"price_fetch done: daily={d} intraday={i}")
