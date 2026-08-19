# -*- coding: utf-8 -*-
"""
collect_yahoo.py - Yahoo!ファイナンス掲示板の取得・パース・重複排除・append

取得は内部JSON API(bff-quote-stocks)の深いページング方式を第一手段とし、
失敗時は従来のHTML最新~72件パーサへフォールバック(失敗分離)。
生データは data/raw_comments.jsonl に append only。既読は data/seen_ids.json。

内部API手順:
  1) /quote/<sym>.T/forum をGET(Session) -> cookie(A/XA/B/XB) と JWT を得る。
  2) bff .../bbs/comment?code=<sym>&size=100&mid=<cursor> を x-jwt-token 付きで叩く。
     (cookieが無いと 400 "signature verification failed"。cookie必須。)
  3) カーソル= そのページ最小 part。既読到達/件数<size/ページ上限で停止。
"""
import os
import re
import html as htmllib
import json
import time
import datetime as dt

import config

# bs4 は HTMLフォールバックparse_bbs_html でのみ必要(ネットワーク非依存=selftest対象)。
from bs4 import BeautifulSoup

# JWT / mid 抽出用の正規表現(ネット非依存の純関数で使用)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_MID_RE = re.compile(r"/forum/(\d+)")

# 直近 collect() の収集メタ(run_once が data_health 導出に読む=新規API非取得で結線)。
# page_cap_hit=YAHOO_MAX_PAGES到達(未取得の新しい投稿が残存)。parsed=0=一次収集失敗(=stale)。
LAST_COLLECT_META = {}


def _record_meta(*, parsed, new, pages, page_cap_hit, fetched_at=None):
    """collect() の結果メタをモジュール変数に退避(副作用のみ・返り値契約は不変)。"""
    global LAST_COLLECT_META
    LAST_COLLECT_META = {
        "source": "yahoo",
        "parsed": int(parsed),
        "new": int(new),
        "pages": int(pages),
        "page_cap_hit": bool(page_cap_hit),
        "fetched_at": fetched_at or dt.datetime.now().isoformat(timespec="seconds"),
    }


# ---- 純関数: 文字コード/文字化け判定(全ソース共通・ネット非依存=selftest対象) --
def normalize_charset(enc, default="utf-8"):
    """宣言charset名を Python codec 名へ正規化。Shift_JIS系は上位互換の cp932 に。"""
    if not enc:
        return default
    e = str(enc).strip().lower().replace("_", "-")
    if e in ("shift-jis", "shift-jis", "sjis", "x-sjis", "ms932", "windows-31j",
             "cp932", "shiftjis"):
        return "cp932"
    if e in ("euc-jp", "eucjp", "x-euc-jp"):
        return "euc_jp"
    if e in ("iso-2022-jp",):
        return "iso2022_jp"
    if e in ("utf-8", "utf8"):
        return "utf-8"
    return e or default


def is_mojibake(text, fffd_min=1, susp_ratio=0.30, susp_min=4):
    """
    文字化けテキストか判定(純関数)。True=化け(LLM/クラスタに渡さない)。
      規則1: U+FFFD(置換文字)を fffd_min 個以上含む -> 化け(正常テキストには出ない)。
      規則2: Latin-1補助/Latin拡張(誤デコード副産物)が多くCJKより優勢で
             全体比率も susp_ratio 以上 -> 化け。
    純ASCII英語(StockTwits/Reddit)や絵文字混じりの正常日本語は誤検知しない。
    """
    if not text:
        return False
    n = len(text)
    if text.count("�") >= fffd_min:
        return True
    susp = 0
    cjk = 0
    for c in text:
        o = ord(c)
        if 0x0080 <= o <= 0x00FF or 0x0100 <= o <= 0x024F:
            susp += 1
        elif (0x3040 <= o <= 0x30FF) or (0x4E00 <= o <= 0x9FFF) or (0xFF00 <= o <= 0xFFEF):
            cjk += 1
    if susp >= susp_min and susp > cjk and (susp / n) >= susp_ratio:
        return True
    return False


# ---- 純関数: パース ----------------------------------------------------------
def _cls_has(el, needle):
    """要素の class 属性に needle を部分一致で含むか(ハッシュ付きクラス名対策)。"""
    c = el.get("class")
    return bool(c) and any(needle in x for x in c)


def parse_post_date(s, now=None):
    """'2026/7/8 16:19' 形式を ISO 文字列へ。失敗時は原文を返す。"""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return dt.datetime(y, mo, d, h, mi).isoformat()
        except ValueError:
            return s
    # 年なし '7/8 16:19' 形式のフォールバック(現在年を補う)
    m = re.match(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        now = now or dt.datetime.now()
        mo, d, h, mi = map(int, m.groups())
        try:
            return dt.datetime(now.year, mo, d, h, mi).isoformat()
        except ValueError:
            return s
    return s


def parse_bbs_html(html):
    """
    掲示板HTML -> コメントdictのリスト。ネットワーク非依存(selftest対象)。
    dict: {id, ts, text, votes_yes, votes_no, user}
    要素が無ければ空リストを返す(例外を投げない=失敗分離)。
    """
    out = []
    if not html:
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return out

    articles = soup.find_all("article")
    for a in articles:
        try:
            # コメント番号リンク(class に commentNo を含む)から id を抽出
            cno = a.find(lambda t: t.name == "a" and _cls_has(t, "commentNo"))
            cid = None
            if cno:
                m = re.search(r"/forum/(\d+)", cno.get("href", ""))
                if m:
                    cid = m.group(1)
            if not cid:
                # フォールバック: No. で始まるforumリンク
                for link in a.find_all("a", href=True):
                    if "/forum/" in link["href"] and link.get_text(strip=True).startswith("No"):
                        m = re.search(r"/forum/(\d+)", link["href"])
                        if m:
                            cid = m.group(1)
                            break
            if not cid:
                continue  # id が取れないものはスキップ

            # 投稿日時
            tt = a.find("time")
            ts = parse_post_date(tt.get_text(strip=True)) if tt else None

            # 本文(body div の <p> を連結。返信引用リンクは除外される)
            body = a.find(lambda x: x.name == "div" and _cls_has(x, "__body"))
            if body:
                ps = body.find_all("p")
                text = "\n".join(p.get_text(" ", strip=True) for p in ps).strip()
            else:
                text = ""

            # 賛成(はい)/反対(いいえ)票
            yes = no = 0
            for b in a.find_all("button"):
                al = b.get("aria-label", "")
                cnt = b.find(lambda x: x.name == "span" and _cls_has(x, "__count"))
                if not cnt:
                    continue
                v = cnt.get_text(strip=True)
                try:
                    v = int(re.sub(r"[^\d]", "", v) or "0")
                except ValueError:
                    v = 0
                if al.startswith("はい"):
                    yes = v
                elif al.startswith("いいえ"):
                    no = v

            # ユーザー名
            un = a.find(lambda x: x.name in ("a", "div") and _cls_has(x, "userName"))
            user = un.get_text(strip=True) if un else None

            out.append({
                "id": cid,
                "ts": ts,
                "text": text,
                "votes_yes": yes,
                "votes_no": no,
                "user": user,
            })
        except Exception:
            # 1件の失敗で全体を止めない
            continue
    return out


# ---- 純関数: 内部JSON API(forum) 用 ---------------------------------------
def extract_jwt(html):
    """forum HTMLから JWT を抽出。無ければ None。純関数。"""
    if not html:
        return None
    m = _JWT_RE.search(html)
    return m.group(0) if m else None


def extract_max_mid(html):
    """forum HTMLの /forum/<digits> の最大値を返す。無ければ None。純関数。"""
    if not html:
        return None
    mids = [int(x) for x in _MID_RE.findall(html)]
    return max(mids) if mids else None


def clean_forum_body(body):
    """
    bff item の body(HTML) をプレーンテキスト化。<br>→改行、その他タグ除去、
    HTMLエンティティ(&hellip; 等)をunescape。純関数。
    """
    if not body:
        return ""
    s = re.sub(r"(?i)<br\s*/?>", "\n", body)      # <br> -> 改行
    s = re.sub(r"<[^>]+>", "", s)                   # 残りのタグ除去
    s = htmllib.unescape(s)                          # エンティティ復元
    # 過剰な空白/改行を整理
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_forum_items(items):
    """
    bff API の response.items(list of dict) -> コメントdictのリスト。純関数。
    dict: {id, ts, text, votes_yes, votes_no, feel, is_reply, user, source}
    不正な要素はスキップ(失敗分離)。
    """
    out = []
    for it in (items or []):
        try:
            part = it.get("part")
            if part is None:
                continue
            text = clean_forum_body(it.get("body", ""))
            if not text:
                continue
            out.append({
                "id": str(part),
                "ts": parse_post_date(it.get("postDate")),
                "text": text,
                "votes_yes": int(it.get("good") or 0),
                "votes_no": int(it.get("bad") or 0),
                "feel": it.get("feelLabel"),
                "is_reply": bool(it.get("parent")),
                "user": it.get("dispname"),
                "author": it.get("userId") or None,   # ハッシュ済みID(ネームド集中用)
                "source": "yahoo",
            })
        except Exception:
            continue
    return out


def min_part(items):
    """items の最小 part(次カーソル用)。無ければ None。純関数。"""
    parts = [it.get("part") for it in (items or []) if isinstance(it.get("part"), int)]
    return min(parts) if parts else None


# ---- 純関数: 重複排除 --------------------------------------------------------
def dedupe_new(rows, seen_ids):
    """seen_ids(set/list)に無い id の行だけ返す。入力内の重複も除去。"""
    seen = set(seen_ids or [])
    new = []
    local = set()
    for r in rows:
        rid = r.get("id")
        if not rid or rid in seen or rid in local:
            continue
        local.add(rid)
        new.append(r)
    return new


# ---- I/O ヘルパ --------------------------------------------------------------
def load_seen_ids(path=None):
    path = path or config.SEEN_IDS_PATH
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("ids", []) if isinstance(data, dict) else data)
    except Exception:
        return set()


def save_seen_ids(ids, path=None):
    path = path or config.SEEN_IDS_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(ids)}, f, ensure_ascii=False)
    os.replace(tmp, path)


def append_jsonl(path, records):
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _log(msg):
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] collect: {msg}"
    print(line)
    try:
        config.ensure_data_dir()
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---- ネットワーク取得 --------------------------------------------------------
def fetch_html(url=None, session=None):
    """指定URL(既定=HTMLフォールバックのbbs)を取得。失敗時 None。"""
    import requests
    url = url or config.YAHOO_BBS_URL
    getter = session or requests
    try:
        r = getter.get(url, headers=config.HTTP_HEADERS, timeout=config.HTTP_TIMEOUT)
        r.encoding = "utf-8"
        if r.status_code != 200:
            _log(f"WARN http {r.status_code} for {url}")
            return None
        return r.text
    except Exception as e:
        _log(f"WARN fetch failed: {e!r}")
        return None


def _open_forum_session():
    """forumをGETしてSession(cookie付)とJWT・max_midを返す。失敗時(None,...)。"""
    import requests
    s = requests.Session()
    s.headers.update(config.HTTP_HEADERS)
    html = fetch_html(config.YAHOO_FORUM_URL, session=s)
    if not html:
        return None, None, None
    return s, extract_jwt(html), extract_max_mid(html)


def _fetch_bff_page(session, jwt, cursor):
    """bff API を1ページ取得。戻り値: (status_code, items or None)。"""
    headers = {
        "Accept": "application/json",
        "Accept-Language": "ja-JP",
        "Referer": config.YAHOO_FORUM_URL,
        "x-jwt-token": jwt,
    }
    url = (f"{config.YAHOO_BFF_URL}?code={config.YAHOO_CODE}"
           f"&size={config.YAHOO_PAGE_SIZE}&mid={cursor}")
    try:
        r = session.get(url, headers=headers, timeout=config.HTTP_TIMEOUT)
        if r.status_code != 200:
            return r.status_code, None
        data = r.json()
        if not data.get("isSuccess", True):
            return 400, None
        return 200, (data.get("response", {}) or {}).get("items", []) or []
    except Exception as e:
        _log(f"WARN bff fetch failed: {e!r}")
        return None, None


def _numeric_seen_max(seen):
    """既読IDのうち純数字(=yahoo part)の最大。無ければ -1。"""
    mx = -1
    for i in seen:
        if isinstance(i, str) and i.isdigit():
            mx = max(mx, int(i))
    return mx


def collect_via_api():
    """
    内部JSON APIで深くページング取得。戻り値: (all_rows, pages, hit_page_cap) または
    (None, 0, False)=失敗。all_rows は新旧混在(呼び出し側で dedupe)。既読最大partを
    下回ったら停止。hit_page_cap=True は YAHOO_MAX_PAGES 到達(未取得が残存)を意味する。
    """
    session, jwt, max_mid = _open_forum_session()
    if session is None or not jwt or max_mid is None:
        _log("WARN forum/JWT取得失敗 -> HTMLフォールバックへ")
        return None, 0, False

    seen = load_seen_ids()
    seen_max = _numeric_seen_max(seen)
    cursor = max_mid + 1     # 最新も含める
    all_rows, pages = [], 0
    refreshed = False
    size = config.YAHOO_PAGE_SIZE
    hit_page_cap = True   # ループを break で抜けたら False(=正常停止)

    for _ in range(config.YAHOO_MAX_PAGES):
        status, items = _fetch_bff_page(session, jwt, cursor)
        if status == 400 and not refreshed:
            # JWT期限切れ等 -> forum再取得で新JWT/cookieを得て1回だけ継続
            _log("bff 400 -> JWT再取得を試行")
            session, jwt, _ = _open_forum_session()
            refreshed = True
            if session is None or not jwt:
                hit_page_cap = False
                break
            continue
        if status != 200 or items is None:
            _log(f"bff stop: status={status}")
            hit_page_cap = False
            break
        pages += 1
        all_rows.extend(parse_forum_items(items))
        mn = min_part(items)
        _log(f"bff page {pages}: items={len(items)} min_part={mn} cursor={cursor}")
        # 停止条件
        if len(items) < size:
            hit_page_cap = False
            break
        if mn is None or mn <= seen_max:
            hit_page_cap = False
            break            # 既読領域に到達(正常経路)
        cursor = mn          # 次カーソル= 最小part(part<mid が返る=重複なし)
        time.sleep(config.YAHOO_PAGE_DELAY_SEC)

    if hit_page_cap:
        # page上限で止まった=まだ未取得が残っている兆候(次runで続きを取得)
        _log(f"WARN YAHOO_MAX_PAGES={config.YAHOO_MAX_PAGES} に到達=未取得が残存"
             f"(min_part={cursor} > seen_max={seen_max})。次runで継続取得。")

    return all_rows, pages, hit_page_cap


def collect(url=None):
    """
    内部JSON APIで取得->失敗時HTMLフォールバック->重複排除->新規のみ raw へ
    append -> seen 更新。戻り値: (new_rows, total_parsed)
    """
    config.ensure_data_dir()

    rows, pages, page_cap = collect_via_api()
    if rows is None:
        # フォールバック: HTML最新~72件(HTMLはページング無し=page_cap概念なし)
        page_cap = False
        html = fetch_html(url)
        if html is None:
            _log("no data (API+HTML両失敗); new=0")
            _record_meta(parsed=0, new=0, pages=0, page_cap_hit=False)
            return [], 0
        rows = parse_bbs_html(html)
        for r in rows:
            r.setdefault("source", "yahoo")
        _log(f"HTML fallback parsed {len(rows)} comments")
    else:
        _log(f"API parsed {len(rows)} comments over {pages} pages")

    if not rows:
        _log("WARN 0 comments parsed")
        _record_meta(parsed=0, new=0, pages=pages, page_cap_hit=page_cap)
        return [], 0

    fetched_at = dt.datetime.now().isoformat(timespec="seconds")

    # 点時刻票スナップショット: 今回パース済み全件(既読含む)の票を差分記録。
    # dedupe前にフックする=既読コメントの票変化も追える(先読み疑い解消の要)。
    # 収集本体を絶対に止めない=例外は握りつぶしてログのみ(失敗分離)。
    if getattr(config, "BBS_VOTES_PIT", False):
        try:
            import votes_snapshot
            nsnap = votes_snapshot.snapshot_batch(rows, fetch_ts=fetched_at)
            _log(f"votes_snapshot: appended={nsnap} (checked={len(rows)})")
        except Exception as e:
            _log(f"WARN votes_snapshot failed (collect本体は続行): {e!r}")

    seen = load_seen_ids()
    new = dedupe_new(rows, seen)
    for r in new:
        r["fetched_at"] = fetched_at
        r.setdefault("source", "yahoo")
    if new:
        append_jsonl(config.RAW_COMMENTS_PATH, new)
        seen.update(r["id"] for r in new)
        save_seen_ids(seen)
    _log(f"new={len(new)} (parsed={len(rows)} total seen={len(seen)})")
    _record_meta(parsed=len(rows), new=len(new), pages=pages,
                 page_cap_hit=page_cap, fetched_at=fetched_at)
    return new, len(rows)


if __name__ == "__main__":
    new, total = collect()
    print(f"collect done: parsed={total} new={len(new)}")
