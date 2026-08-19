# -*- coding: utf-8 -*-
"""
jsonl_window.py - 大容量 append-only jsonl(raw_comments/analyzed/snapshots等)を
「全件読み込まずに直近だけ」読むための共通ヘルパー。

背景: dashboard.py の毎レンダリング読込と snapshot.py::write_snapshot() の
7分おき読込の両方が、同じ「末尾から逆読みして早期打ち切る」ロジックを必要とするため
共通モジュール化する(コピー2箇所の版ズレを避ける)。

規律(絶対):
  - 読み取り専用。書き込み/削除/切り詰めは一切行わない(呼び出し元の台帳ファイルは不変)。
  - ファイルは基本的に時系列順追記(append-only)を前提にするが、
    万一の逆転行/壊れた行があっても例外を出さず fail-soft に振る舞う。
    read_jsonl_recent() は異常時、安全側で read_jsonl_full()(全件読込)へフォールバックする。
  - 純粋な read ヘルパーのみ(Streamlit 等フレームワークに非依存)。
"""
import os
import json
import datetime as dt


def _iter_lines_reverse(path, chunk_size=1 << 20):
    """ファイル末尾から行を逆順(最新行から)に yield するジェネレータ。

    バイト単位で chunk_size(既定1MiB)ずつ末尾から遡って読み、b'\\n' で分割する。
    分割はASCII改行のみに依存するため UTF-8 のマルチバイト文字を壊さない
    (chunkを跨ぐ半端な行は次のchunkと結合してから decode するので安全)。
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        remaining = f.tell()
        tail = b""
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            f.seek(remaining)
            chunk = f.read(read_size)
            data = chunk + tail
            parts = data.split(b"\n")
            tail = parts[0]          # 次chunkへ跨ぐ可能性がある先頭断片
            for raw_line in reversed(parts[1:]):
                if raw_line:
                    try:
                        yield raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
        if tail:
            try:
                yield tail.decode("utf-8")
            except UnicodeDecodeError:
                pass


def read_jsonl_full(path):
    """全件読み込み(フォールバック/全履歴が必要な機能専用)。壊れた行はスキップ。

    ★2026-08-19修正(おにや22:13投稿・重大障害調査): 従来はテキストモード
    (`encoding="utf-8"`)で`for line in f`と行ごと反復していたため、torn write
    (catchup等の並行書き込みが1行の途中で分断されマルチバイトUTF-8文字が壊れる
    既知の競合パターン)に遭遇するとPythonのファイルイテレータ自体が
    UnicodeDecodeErrorを送出し、それ以降の行は(外側のtry/exceptに丸ごと
    捕捉されるため)一切読めずrowsが不完全なまま返っていた(=呼び手には
    「エラーにはならないが値が静かに欠落する」形で現れる潜在バグ)。
    _iter_lines_reverse()と同じ設計(バイナリモード+行ごとに個別decode)へ
    揃えることで、1行だけ不正バイト列でもその行だけをスキップして残りは
    正常に読めるようにする。"""
    rows = []
    if not path or not os.path.exists(path):
        return rows
    try:
        with open(path, "rb") as f:
            for raw_line in f:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue  # torn write等でバイト列が壊れた1行だけスキップし継続
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def read_jsonl_tail(path, n):
    """末尾n行を古い→新しい順で返す(ファイル末尾からの逆読み・全件は読まない)。

    fail-soft: 逆読み中に何か起きたら安全側で全件読込→末尾n件にフォールバックする。
    """
    if not path or not os.path.exists(path) or not n or n <= 0:
        return []
    out = []
    try:
        for line in _iter_lines_reverse(path):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= n:
                break
        out.reverse()
        return out
    except Exception:
        return read_jsonl_full(path)[-n:]


def read_jsonl_recent(path, days=60, now=None, ts_field="ts", stop_after_old=500):
    """ファイル末尾から逆読みし、行の ts_field が (now - days) より前になった
    時点で打ち切る(全件を読まない・行数ではなく日付境界で打ち切り)。古い→新しい順で返す。

    ファイルは基本的に時系列順追記(append-only)だが、実データを実測したところ
    完全な単調増加ではなかった: StockTwits/5ch収集は同じ少数の過去投稿を毎runで
    再度書き込むため(id去重は読込側でのみ行う設計)、ファイル中に短い「逆行ブロック」
    (実測: raw_comments.jsonlで最大71行連続)が散発する。これを境界より古い行
    1行だけで打ち切ると直近データを不当に切り捨ててしまうため、境界より古い行が
    stop_after_old(既定500・実測最大の約7倍の安全マージン)行"連続"した時点で
    初めて打ち切る(単発の逆行行はスキップして読み進める=結果には含めない)。

    fail-soft: 例外(壊れたファイル・型不正のts等)が起きた異常時は、
    安全側で read_jsonl_full()(全件読込)にフォールバックする。
    """
    if not path or not os.path.exists(path):
        return []
    now = now or dt.datetime.now()
    cutoff = (now - dt.timedelta(days=days)).isoformat()
    out = []
    old_run = 0
    try:
        for line in _iter_lines_reverse(path):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get(ts_field)
            if isinstance(ts, str) and ts and ts < cutoff:
                old_run += 1
                if old_run >= stop_after_old:
                    break
                continue  # 単発(short-run)の逆行行=結果に含めず読み進める
            old_run = 0
            out.append(r)
        out.reverse()
        return out
    except Exception:
        # fail-soft: 逆読みで何か起きたら安全側=全件読込
        return read_jsonl_full(path)
