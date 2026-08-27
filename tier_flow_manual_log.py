# -*- coding: utf-8 -*-
"""
tier_flow_manual_log.py - kabu画面スクリーンショットから読み取った
階層別(超大口/大口/中口/小口)資金分布を手動追記するための簡易ログ。

背景(2026-08-27): moomoo経由の階層別データ自動取得はOpenDログインが
CAPTCHAでブロックされ保留中(CROSS_PROJECT_LOG 2026-08-24 23:01参照)。
一方、kabuステーション画面の「注文・約定分析」パネルは、おにやがユーザーの
スクリーンショットを読む都度、実質的に無償で階層別データを提供している。
このデータを都度読み捨てにせず、CSVへ蓄積することで、moomoo復旧を待たずに
lead-lag分析(MLのboard_totals_trend_leadlag.pyと同型の分析)の材料を
今から積み増せるようにする。

制約:
- 完全自動ではない(スクリーンショットが来た時だけ・頻度は不定期)。
- 値は目視転記(OCR等は行わない)。誤記のリスクはユーザー/おにやの
  確認に依存する。
- record_all.py/board_read.py等、本番の自動記録パイプラインには一切
  触れない(別ファイルへの追記のみ)。

使い方:
  from tier_flow_manual_log import append_observation
  append_observation(
      date_iso="2026-08-27", time_hhmm="09:10",
      price=52970, mega_buy=34.77, mega_sell=298.73,
      large_buy=47.38, large_sell=8.34,
      mid_buy=43.05, mid_sell=36.02,
      small_buy=104.87, small_sell=129.86,
      source="screenshot 09:10",
  )
"""
import os
import csv
import config


LOG_PATH = os.path.join(config.DATA_DIR, "tier_flow_manual_log.csv")

FIELDNAMES = [
    "date", "time", "price",
    "mega_buy", "mega_sell", "large_buy", "large_sell",
    "mid_buy", "mid_sell", "small_buy", "small_sell",
    "source",
]


def append_observation(date_iso, time_hhmm, price,
                        mega_buy, mega_sell, large_buy, large_sell,
                        mid_buy, mid_sell, small_buy, small_sell,
                        source=""):
    """1件分の階層別観測値をCSVへ追記する(read-append-only・他ファイル無変更)。
    ファイルが無ければヘッダー付きで新規作成。戻り値: 書き込んだ行dict。
    """
    config.ensure_data_dir()
    row = {
        "date": date_iso, "time": time_hhmm, "price": price,
        "mega_buy": mega_buy, "mega_sell": mega_sell,
        "large_buy": large_buy, "large_sell": large_sell,
        "mid_buy": mid_buy, "mid_sell": mid_sell,
        "small_buy": small_buy, "small_sell": small_sell,
        "source": source,
    }
    file_exists = os.path.isfile(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            w.writeheader()
        w.writerow(row)
    return row


def read_all_observations():
    """蓄積済みの全観測行を読む(read-only)。ファイル無ければ[]。"""
    if not os.path.isfile(LOG_PATH):
        return []
    with open(LOG_PATH, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _run_selftests():
    import tempfile
    fails = []

    def ck(name, cond):
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    global LOG_PATH
    orig_path = LOG_PATH
    tmpdir = tempfile.mkdtemp(prefix="tier_flow_selftest_")
    LOG_PATH = os.path.join(tmpdir, "tier_flow_manual_log.csv")
    try:
        r1 = append_observation("2026-08-27", "09:10", 52970,
                                34.77, 298.73, 47.38, 8.34,
                                43.05, 36.02, 104.87, 129.86,
                                source="screenshot 09:10")
        ck("append: 1件目書込みの戻り値にdateを含む", r1["date"] == "2026-08-27")
        ck("append: ファイルが作成される", os.path.isfile(LOG_PATH))

        rows = read_all_observations()
        ck("read: 1件読み取れる", len(rows) == 1)
        ck("read: mega_sellが文字列として保持される(CSV仕様)", rows[0]["mega_sell"] == "298.73")

        append_observation("2026-08-27", "10:13", 50690,
                           10.0, 20.0, 5.0, 5.0, 3.0, 3.0, 2.0, 2.0, source="screenshot 10:13")
        rows2 = read_all_observations()
        ck("append: 2件目追記後は2行", len(rows2) == 2)
        ck("append: 2件目のtimeが正しい", rows2[1]["time"] == "10:13")
    finally:
        LOG_PATH = orig_path
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("PASS" if not fails else f"FAIL: {len(fails)}")
    for name in fails:
        print("  - " + name)
    return len(fails)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(1 if _run_selftests() else 0)
    print(__doc__)
