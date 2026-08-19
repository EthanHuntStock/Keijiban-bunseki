# -*- coding: utf-8 -*-
"""
export_signal.py - 機械可読エクスポートの唯一の書き手。副作用アダプタ(ネット非依存)。
  - history.jsonl に1行 append(退避主義=書換/削除しない)
  - latest.json を atomic 置換(temp -> os.replace = Dropbox WinError5 安全)
消費側(トレPJ)は読み取り専用でこれを vol-regime / range-day のフィルタ&サイザとして使う
(方向オラクルではない)。OFF-by-default(BBS_SIGNAL_ENABLE)。
"""
import os
import json
import datetime as dt

import config
import signal_engine


# ============================================================================
# 軽量スキーマ検証(jsonschema非依存・selftest/DEBUG用)
# ============================================================================
_REQUIRED = ["schema_version", "symbol", "run_ts", "asof_date", "calibration_status",
             "vol_regime", "vol_regime_score", "range_day_score", "direction_candidate",
             "confidence", "n", "features", "thresholds_crossed", "signal_spec_hash",
             "disclaimer"]
_VOL_REGIME_ENUM = {"calm", "normal", "elevated", "extreme", "calibrating"}
_CUTOFF_ENUM = {"intraday_rolling", "13:00_decision", "close_consolidated", None}
_SIDE_ENUM = {"fade_up", "fade_down", "none"}


def validate_record(rec):
    """schema v1.0 の必須/const/enum/範囲を検証。エラー文字列のリストを返す(空=OK)。"""
    errs = []
    if not isinstance(rec, dict):
        return ["record is not a dict"]
    for k in _REQUIRED:
        if k not in rec:
            errs.append(f"missing required: {k}")
    if rec.get("schema_version") != "1.0":
        errs.append("schema_version must be '1.0'")
    if rec.get("symbol") != "285A":
        errs.append("symbol must be '285A'")
    if rec.get("calibration_status") not in ("calibrating", "calibrated"):
        errs.append("calibration_status invalid")
    if rec.get("vol_regime") not in _VOL_REGIME_ENUM:
        errs.append("vol_regime invalid")
    if "cutoff" in rec and rec["cutoff"] not in _CUTOFF_ENUM:
        errs.append("cutoff invalid")
    for k in ("vol_regime_score", "range_day_score"):
        v = rec.get(k)
        if v is not None and not (0.0 <= v <= 1.0):
            errs.append(f"{k} out of [0,1]")
    conf = rec.get("confidence")
    if not (isinstance(conf, (int, float)) and 0.0 <= conf <= 1.0):
        errs.append("confidence out of [0,1]")
    # 較正中は confidence <= cap
    if rec.get("calibration_status") == "calibrating" and isinstance(conf, (int, float)):
        if conf > config.BVP_CONF_CALIB_CAP + 1e-9:
            errs.append(f"confidence must be <= {config.BVP_CONF_CALIB_CAP} while calibrating")
    dc = rec.get("direction_candidate", {})
    if dc.get("status") != "未検証":
        errs.append("direction_candidate.status must be const '未検証'")
    if dc.get("side") not in _SIDE_ENUM:
        errs.append("direction_candidate.side invalid")
    if rec.get("disclaimer") != signal_engine.DISCLAIMER:
        errs.append("disclaimer const mismatch")
    n = rec.get("n", {})
    if not isinstance(n, dict) or "raw_today" not in n or "analyzed_today" not in n:
        errs.append("n missing raw_today/analyzed_today")
    return errs


# ============================================================================
# 書き込み
# ============================================================================
def _atomic_write_json(path, obj):
    """temp へ書いて os.replace(WinError5安全)。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] export_signal: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_from_signals(sig, raw_rows, *, cutoff, run_ts=None, data_health=None):
    """
    build_export_record でレコードを作り、history.jsonl に append + latest.json を atomic置換。
    戻り値: 書いたレコード。selftest/DEBUG では検証も回す。
    """
    config.ensure_signal_export_dir()
    run_ts = run_ts or dt.datetime.now().isoformat(timespec="seconds")
    rec = signal_engine.build_export_record(
        sig, run_ts=run_ts, cutoff=cutoff, raw_rows=raw_rows,
        data_health=data_health)
    errs = validate_record(rec)
    if errs:
        _log(f"WARN schema validation: {errs}")
    _append_jsonl(config.SIGNAL_HISTORY_PATH, rec)      # append-only
    _atomic_write_json(config.SIGNAL_LATEST_PATH, rec)  # atomic
    _log(f"export cutoff={cutoff} regime={rec['vol_regime']} "
         f"vrs={rec['vol_regime_score']} conf={rec['confidence']} "
         f"calib={rec['calibration_status']}(n={rec['calib_days']})")
    return rec


def load_latest():
    """消費側/ダッシュボード用: latest.json を読む。無ければ None。"""
    p = config.SIGNAL_LATEST_PATH
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_history():
    """history.jsonl を list で。無ければ []。"""
    p = config.SIGNAL_HISTORY_PATH
    rows = []
    if not os.path.exists(p):
        return rows
    # ★2026-08-19修正(おにや22:13投稿・重大障害調査の横展開): torn write対策
    # (バイナリモード+行ごと個別decode)。1行分のバイト破損で以降の行が全滅しない。
    try:
        with open(p, "rb") as f:
            for raw_line in f:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return rows
