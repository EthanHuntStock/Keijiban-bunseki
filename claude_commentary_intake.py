# -*- coding: utf-8 -*-
"""Claude(AIセクターワールドモニター)の考察を公開ダッシュボード(latest.json の ai_commentary)へ取り込む。

★2026-10-15(コーデ指定・10-13ユーザー決定『AI考察パネルの表示=2段構成+急変時の臨時考察』): LM廃止後の考察は
  モニタが 寄り前 08:50／寄り直後 09:10／前場引け前 11:30／大引け後 15:45(+急変時の臨時)に書く。その出力
  ai_sector_monitor\\_ledger\\ai_insight_claude_latest.json を、**実測と突合して問題が無い時だけ**公開へ反映する。

突合(終値/現在値・当日高値・当日安値・前営業日終値・前営業日比)は、おにやが記録した ticks_285A_<日付>.csv の
生成時刻(generated_at)時点の実測と**円単位で**照合する(10-15 15:42 の考察が『終値12,740円・安値12,730円』と書き
実測 終値12,880円・安値12,675円と食い違った実例を受けた。連続取引の最終値を終値と読み、大引けの板寄せと
日中安値を取りこぼしていた)。不一致なら公開せず、理由を data\\claude_commentary_intake_log.jsonl に残す。

設計メモ: 失敗しても例外を出さない(fail-soft)・自己テスト中は BBS_INTAKE_OFF=1 で何もしない・
latest.json の ai_commentary は text/generated_at の2キーだけを書く(既存の carry と同じ形)。
"""
import csv
import datetime as dt
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR_JSON = r"C:\Users\ryuta\OneDrive\AI用フォルダ\ai_sector_monitor\_ledger\ai_insight_claude_latest.json"
TICKS_DIR = r"C:\Users\ryuta\kabu_local_record\ticks_board\285A_キオクシア\記録データ"
try:  # データ置き場は config.DATA_DIR(=C:\AI用フォルダ\おにや式投資法\data・BBS_DATA_ROOTで上書き可)。ソース木の data/ ではない
    import config as _config
    LATEST_JSON = _config.PUBLIC_EXPORT_LATEST_PATH
    INTAKE_LOG = os.path.join(_config.DATA_DIR, "claude_commentary_intake_log.jsonl")
except Exception:  # pragma: no cover
    LATEST_JSON = os.path.join(HERE, "data", "public_export", "latest.json")
    INTAKE_LOG = os.path.join(HERE, "data", "claude_commentary_intake_log.jsonl")

HL_TOL = 10.0  # 高値・安値の許容差(円)
_NUM = r"([\d,]+(?:\.\d+)?)"
_PCT = r"([+\-−－]?\d+(?:\.\d+)?)\s*[%％]"


def _num(s):
    return float(str(s).replace(",", ""))


def _pct(s):
    return float(str(s).replace("−", "-").replace("－", "-"))


def parse_claims(text):
    """考察本文から、実測と突合できる数値の主張を取り出す(無い項目は入れない)。"""
    t = str(text or "")
    c = {}
    m = re.search(r"(?:本日の|当日の)?終値は\s*" + _NUM + r"\s*円", t)
    if not m:
        m = re.search(r"現在値(?:は)?\s*" + _NUM + r"\s*円", t)
    if m:
        # ★2026-10-29: 同じ文の中で『昨日』『前営業日』が終値より前にある時は『前営業日の終値』の主張(yday_close)。
        #   寄り前・寄り直後の考察は前日の終値を述べる。以前はこれを『今の終値』と読んで現在値と突合し不合格にしていた。
        _s0 = max(t.rfind("。", 0, m.start()), t.rfind("\n", 0, m.start())) + 1
        if re.search(r"昨日|前営業日", t[_s0:m.start()]):
            c["yday_close"] = _num(m.group(1))
        else:
            c["close"] = _num(m.group(1))
    if "yday_close" not in c:
        for s in re.split(r"(?<=。)|\n", t):
            if "285A" not in s or "信用" in s or "本日" in s or "当日" in s:
                continue
            m2 = re.search(r"(?:昨日|前営業日)[^。]{0,40}?終値(?:は)?\s*" + _NUM + r"\s*円", s)
            if m2:
                c["yday_close"] = _num(m2.group(1))
                break
    m = re.search(r"当日高値は\s*" + _NUM + r"\s*円", t)
    if m:
        c["high"] = _num(m.group(1))
    m = re.search(r"当日安値は\s*" + _NUM + r"\s*円", t)
    if m:
        c["low"] = _num(m.group(1))
    m = re.search(r"前営業日終値(?:は)?[（(]\s*" + _NUM + r"\s*円", t)
    if m:
        c["prev_close"] = _num(m.group(1))
    # ★2026-10-28 修正: 前営業日比は『285Aの終値』を述べた文からだけ取る。以前は本文全体の最初の「前営業日比 ○%」を
    #   取っていたため、信用倍率の文『31.88倍（前営業日比-1.5%）』を285Aの騰落率と読み、10-20 15:33以降8日間すべて
    #   不合格になった(公開のAI考察が10-20のまま)。信用倍率など別の指標の文は除き、『…終値12,605円と比べて+13.76%』
    #   『前営業日終値比 +x%』『前営業日終値(○円)に対して +x%』の形を読む。
    for s in re.split(r"(?<=。)|\n", t):
        if "信用" in s:
            continue
        if "終値" not in s and "現在値" not in s:
            continue
        m = (re.search(r"前営業日(?:終値)?比\s*" + _PCT, s) or re.search(r"に対して\s*" + _PCT, s)
             or re.search(r"[\d,]+\s*円と比べて\s*" + _PCT, s))
        if m:
            c["pct"] = _pct(m.group(1))
            break
    return c


def _read_ticks(path, upto=None):
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            try:
                ts = dt.datetime.strptime(r["time"][:19], "%Y-%m-%d %H:%M:%S")
                p = float(r["price"])
            except Exception:
                continue
            if upto is not None and ts > upto:
                continue
            rows.append((ts, p))
    return rows


def actuals_as_of(generated_at, ticks_dir=TICKS_DIR):
    """generated_at 時点の 現在値(終値)・当日高値・当日安値・前営業日終値 を記録tickから求める。取れなければ None。"""
    g = dt.datetime.fromisoformat(str(generated_at)[:19])
    day = g.strftime("%Y-%m-%d")
    p = os.path.join(ticks_dir, f"ticks_285A_{day}.csv")
    rows = _read_ticks(p, upto=g) if os.path.exists(p) else []
    if not rows and g.time() < dt.time(9, 0):
        # ★2026-10-29: 寄り前(09:00前)の考察は当日tickがまだ無く、前営業日の値(終値・高安)を述べる。
        #   これまで『実測tickが取れない』で必ず不合格になり、寄り前スロットは公開できなかった(08:46版が09:08に不合格)。
        #   当日tickが無い寄り前だけ、直近の前営業日のtick全体を実測とする(当日のtickがある時刻の考察は従来どおり)。
        prevs0 = sorted(q for q in glob.glob(os.path.join(ticks_dir, "ticks_285A_*.csv"))
                        if os.path.basename(q) < os.path.basename(p))
        for q in reversed(prevs0):
            rows = _read_ticks(q)
            if rows:
                p = q
                break
    if not rows:
        return None
    prices = [x[1] for x in rows]
    out = {"close": prices[-1], "high": max(prices), "low": min(prices), "n": len(rows), "last_ts": rows[-1][0].isoformat()}
    if os.path.basename(p) < f"ticks_285A_{day}.csv":
        out["yday_close"] = prices[-1]  # 寄り前の代替(前営業日のtick全体)。この場合の『前営業日の終値』は実測の終値そのもの
    prevs = sorted(q for q in glob.glob(os.path.join(ticks_dir, "ticks_285A_*.csv"))
                   if os.path.basename(q) < os.path.basename(p))
    for q in reversed(prevs):
        pr = _read_ticks(q)
        if pr:
            out["prev_close"] = pr[-1][1]
            break
    if "yday_close" not in out and "prev_close" in out:
        out["yday_close"] = out["prev_close"]  # 立会中の『前営業日の終値』=前営業日のtickの最終値
    return out


def validate(text, generated_at, ticks_dir=TICKS_DIR):
    """(ok, reasons, claims, actuals)。主張が1つも取れない/実測が取れない場合は ok=False(公開しない側に倒す)。"""
    claims = parse_claims(text)
    act = actuals_as_of(generated_at, ticks_dir)
    reasons = []
    if act is None:
        return False, ["実測tickが取れない"], claims, None
    if not claims:
        return False, ["本文から数値の主張を取れない(突合不能)"], claims, act
    # 終値・前営業日終値は円単位で一致を要求。高値・安値は記録tickが約定を取りこぼす(公式値との差=10-15の高値は
    # 公式13,825円に対し記録13,815円)ため±HL_TOL円まで許容する(安値の取り違え12,730 vs 12,675=55円差は検出できる)。
    tol = {"close": 0.5, "prev_close": 0.5, "yday_close": 0.5, "high": HL_TOL, "low": HL_TOL}
    for k in ("close", "high", "low", "prev_close", "yday_close"):
        if k in claims and k in act and abs(claims[k] - act[k]) > tol[k]:
            reasons.append(f"{k}: 考察={claims[k]:.0f} 実測={act[k]:.0f}")
    if "pct" in claims and "prev_close" in act and act["prev_close"]:
        exp = (act["close"] / act["prev_close"] - 1.0) * 100.0
        # ★2026-10-29: 場中(09:00〜15:30)の『HH:MM時点』の騰落率は、本文の時刻と取込側の実測(generated_atまでの最終tick)が
        #   数秒〜数十秒ずれる(09:27臨時 +6.87 vs +7.15・11:24前場引け前 +3.73 vs +3.77)。場中だけ±0.3ptまで許容する。
        #   0.3pt≒45円で、前営業日終値の取り違え(1pt超)や桁違いは引き続き検出できる。寄り前・引け後・円単位の終値は従来どおり厳格。
        _g = dt.datetime.fromisoformat(str(generated_at)[:19]).time()
        _ptol = 0.3 if dt.time(9, 0) <= _g < dt.time(15, 30) else 0.03
        if abs(claims["pct"] - exp) > _ptol:
            reasons.append(f"pct: 考察={claims['pct']:+.2f}% 実測={exp:+.2f}%")
    return (not reasons), reasons, claims, act


MAX_RECENT_EVENT_DAYS = 3   # ■直近の出来事に載せてよい『日付つきの値動き』の営業日数(直近3営業日＋重要な適時開示だけ)


def lint_recent_events(text):
    """★2026-11-26(ユーザー指示・モニタ側と同じ制約): 考察の『■ 直近の出来事』は直近3営業日の値動き＋重要な適時開示だけに要約し、
    10営業日以上の騰落の羅列はしない。考察はモニタ側で書かれ両サイト共通文面なので、ここでは本文を変えず、違反の疑い
    (節内に日付(MM-DD)つきの騰落率が MAX_RECENT_EVENT_DAYS 日を超えて並ぶ)を数えて取込ログに残すだけ(公開は止めない・警告のみ)。
    戻り値={n_days, ok, dates}。節が無ければ n_days=0・ok=True。純関数。"""
    t = str(text or "")
    m = re.search(r"■\s*直近の出来事[^\n]*\n(.*?)(?=\n\s*■|\Z)", t, re.S)
    if not m:
        return {"n_days": 0, "ok": True, "dates": []}
    dates = []
    for mm in re.finditer(r"(\d{1,2})[-/月](\d{1,2})日?[^。\n]{0,40}?[+\-−－]?\d+(?:\.\d+)?\s*[%％]", m.group(1)):
        d = "%02d-%02d" % (int(mm.group(1)), int(mm.group(2)))
        if d not in dates:
            dates.append(d)
    return {"n_days": len(dates), "ok": len(dates) <= MAX_RECENT_EVENT_DAYS, "dates": dates}


def _log(rec, log_path=INTAKE_LOG):
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        rec = dict(rec)
        rec["at"] = dt.datetime.now().isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def sync(log=None, monitor_json=MONITOR_JSON, latest_json=LATEST_JSON, ticks_dir=TICKS_DIR,
         now=None, log_path=INTAKE_LOG):
    """モニタの最新考察を、検証に通れば latest.json の ai_commentary に反映する。戻り値=結果の短い文字列。
    何があっても例外を出さない。"""
    def say(msg):
        try:
            if log:
                log("claude_intake: " + msg)
        except Exception:
            pass

    if os.environ.get("BBS_INTAKE_OFF", "").strip() == "1":
        return "off"
    try:
        if not os.path.exists(monitor_json):
            return "no_monitor_file"
        c = json.load(open(monitor_json, encoding="utf-8"))
        text = c.get("text")
        gen = c.get("generated_at")
        if not text or not gen:
            return "empty"
        now = now or dt.datetime.now()
        try:
            vu = dt.datetime.fromisoformat(str(c.get("valid_until"))[:19]) if c.get("valid_until") else None
        except Exception:
            vu = None
        if vu is not None and now > vu:
            return "expired"
        gen19 = str(gen)[:19]
        cur = {}
        if os.path.exists(latest_json):
            cur = (json.load(open(latest_json, encoding="utf-8")).get("ai_commentary") or {})
        if str(cur.get("generated_at") or "")[:19] >= gen19:
            return "not_newer"
        ok, reasons, claims, act = validate(text, gen19, ticks_dir)
        slot = c.get("slot")
        if not ok:
            _log({"result": "rejected", "generated_at": gen19, "slot": slot, "reasons": reasons,
                  "claims": claims, "actuals": act}, log_path)
            say(f"REJECT generated_at={gen19} slot={slot} reasons={reasons}")
            return "rejected"
        j = json.load(open(latest_json, encoding="utf-8"))
        j["ai_commentary"] = {"text": text, "generated_at": gen19}
        raw = open(latest_json, encoding="utf-8").read()
        indent = 2 if raw.startswith("{\n") else None
        tmp = latest_json + ".tmp_intake"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(j, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, latest_json)
        _lint = lint_recent_events(text)
        _log({"result": "published", "generated_at": gen19, "slot": slot, "claims": claims, "recent_events_lint": _lint}, log_path)
        if not _lint["ok"]:
            say(f"WARN 直近の出来事に{_lint['n_days']}日ぶんの騰落が並んでいる(直近3営業日+適時開示だけに要約する規則)・公開は止めない dates={_lint['dates']}")
        say(f"PUBLISHED generated_at={gen19} slot={slot} claims={claims}")
        return "published"
    except Exception as e:  # noqa: BLE001
        try:
            _log({"result": "error", "error": repr(e)}, log_path)
            say(f"WARN intake failed (ignored): {e!r}")
        except Exception:
            pass
        return "error"


if __name__ == "__main__":  # 手動実行: python claude_commentary_intake.py
    print(sync(log=print))
