# -*- coding: utf-8 -*-
"""
tier_size_report.py - kabu tickデータ(285A)から直近(as_ofまで)のtierサイズ構成比を
標準出力へ表示する実運用CLIツール(read-only・発注なし)。

★方向(買い優勢/売り優勢)は一切出さない。tierごとの出来高金額シェア(サイズ分類)のみ。
背景・限界・DEFAULT_THRESHOLDSの校正根拠(2026-08-27・rho≈0.6-0.8)は
tick_tier_classifier.py のモジュールdocstring参照。moomoo経由の階層別データ自動取得は
OpenDログインがCAPTCHAでブロックされ保留中(CROSS_PROJECT_LOG 2026-08-24参照)のため、
このツールが「サイズ分類のみ」の代替として使える(方向は代替しない・出せない)。

使い方:
  python tier_size_report.py                          # 本日・当日ファイル末尾(直近)まで
  python tier_size_report.py --date 2026-08-27 --as-of 11:06
  python tier_size_report.py --compare-manual          # tier_flow_manual_log.csvの当日分と並記
  python tier_size_report.py --date 2026-08-27 --compare-manual

自己テスト(実データ非依存・合成tickのみ): python tier_size_report.py --selftest

将来 run_once.py へ組み込みたくなった場合、build_tier_size_report() /
compare_with_manual() をそのまま呼ぶだけで良いように関数分離してある
(今回のタスクではrun_once.py自体は変更しない・本ファイルは単独スクリプトとして完結)。
"""
import argparse
import datetime as dt
import os

import config
import tick_tier_classifier as ttc
import tier_flow_manual_log


TIER_LABEL = {"super": "超大口", "big": "大口", "mid": "中口", "small": "小口"}


# ============================================================================
# I/O補助(read-only)
# ============================================================================
def _ticks_csv_path(date_iso, ticks_dir=None):
    d = ticks_dir or config.KABU_PROTO1_285A_TICKS_DIR
    return os.path.join(d, f"ticks_285A_{date_iso}.csv")


def load_day_ticks(date_iso, ticks_dir=None):
    """指定日(YYYY-MM-DD)のtick CSVを読み込む。ファイルが無ければ[](fail-soft)。
    read-only(プロト1の記録データを読むだけ・一切書き込まない)。
    """
    path = _ticks_csv_path(date_iso, ticks_dir=ticks_dir)
    if not os.path.exists(path):
        return []
    return ttc.load_ticks_csv(path)


# ============================================================================
# レポート組み立て(純関数寄り。I/Oはload_day_ticksのみに閉じ込める)
# ============================================================================
def _normalize_as_of(date_iso, as_of_hhmm):
    """'HH:MM' または 'HH:MM:SS' をtick CSVのtime列と同じ 'YYYY-MM-DD HH:MM:SS' 形式へ。"""
    if not as_of_hhmm:
        return None
    hhmm = as_of_hhmm.strip()
    if len(hhmm) == 5:   # 'HH:MM'
        hhmm = hhmm + ":59"   # その分の最後まで含める(境界を分単位で切りたいため)
    return f"{date_iso} {hhmm}"


def build_tier_size_report(date_iso, as_of_hhmm=None, thresholds=None, ticks_dir=None):
    """
    指定日・as_of時刻までのtierサイズ構成比レポートを組み立てる。
    ⚠️方向情報は一切含まない(estimate_tier_size_shares()を使用)。

    引数:
      date_iso: 'YYYY-MM-DD'
      as_of_hhmm: 'HH:MM'省略時はその日の全tick(=最新まで)を対象にする
      thresholds: 省略時 tick_tier_classifier.DEFAULT_THRESHOLDS
      ticks_dir: 省略時 config.KABU_PROTO1_285A_TICKS_DIR

    戻り値:
      {date, as_of, n_ticks_total, shares(=estimate_tier_size_sharesの戻り値そのまま)}
    """
    ticks = load_day_ticks(date_iso, ticks_dir=ticks_dir)
    end_time = _normalize_as_of(date_iso, as_of_hhmm)
    windowed = ttc.filter_ticks_window(ticks, end_time=end_time) if end_time else ticks
    shares = ttc.estimate_tier_size_shares(windowed, thresholds=thresholds)
    return {
        "date": date_iso,
        "as_of": as_of_hhmm or "(latest)",
        "n_ticks_total": len(ticks),
        "shares": shares,
    }


def format_report_text(report):
    """人間が読める形式へ整形(標準出力用)。"""
    lines = []
    lines.append(f"=== tier_size_report 285A {report['date']} as_of={report['as_of']} ===")
    sh = report["shares"]
    lines.append(f"分類対象tick数: {sh['n_ticks']}/{report['n_ticks_total']}件"
                 f"(除外=tickvol<=0またはprice欠損)")
    if sh["total_amount_yen"] <= 0:
        lines.append("(対象tickが0件のため構成比は計算できません)")
        return "\n".join(lines)
    lines.append(f"対象金額合計: {sh['total_amount_yen'] / 1e8:.2f}億円")
    for tier in ttc.TIER_ORDER:
        s = sh[tier]
        bar = "#" * int(round(s["share"] * 40))
        lines.append(f"  {TIER_LABEL[tier]}({tier:5s}): {s['share'] * 100:5.1f}%  "
                     f"{bar:<40s} ({s['amount_yen'] / 1e8:.2f}億円)")
    lines.append("※方向(買い優勢/売り優勢)は一切出しません。サイズ構成比のみ"
                 "(信頼できない理由はtick_tier_classifier.pyのdocstring参照)。")
    return "\n".join(lines)


# ============================================================================
# moomoo手動観測(tier_flow_manual_log.csv)との並記(目視確認用)
# ============================================================================
def _spearman_rho(a, b):
    """簡易スピアマン順位相関(純関数・scipy非依存・同順位はタイ平均で処理)。
    len(a)<2 または分散0(全順位同値)なら None を返す。
    """
    if a is None or b is None or len(a) != len(b) or len(a) < 2:
        return None
    ra = _ranks(a)
    rb = _ranks(b)
    n = len(a)
    mean_r = (n + 1) / 2.0
    num = sum((ra[i] - mean_r) * (rb[i] - mean_r) for i in range(n))
    den_a = sum((r - mean_r) ** 2 for r in ra)
    den_b = sum((r - mean_r) ** 2 for r in rb)
    den = (den_a * den_b) ** 0.5
    if den == 0:
        return None
    return num / den


def _ranks(values):
    """タイは平均順位(1-indexed)。純関数。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _manual_shares_from_row(row):
    """tier_flow_manual_log.csvの1行(累積buy+sell・単位=億円)からtierごとの
    金額シェアを計算する純関数。mega=super, large=big, mid=mid, small=smallに対応。
    """
    amounts = {
        "super": float(row["mega_buy"]) + float(row["mega_sell"]),
        "big": float(row["large_buy"]) + float(row["large_sell"]),
        "mid": float(row["mid_buy"]) + float(row["mid_sell"]),
        "small": float(row["small_buy"]) + float(row["small_sell"]),
    }
    total = sum(amounts.values())
    return {t: (amounts[t] / total if total > 0 else 0.0) for t in ttc.TIER_ORDER}


def compare_with_manual(date_iso, thresholds=None, ticks_dir=None, manual_rows=None):
    """
    tier_flow_manual_log.csv の当日分の各観測時刻について、kabu tickベースの推定
    シェアと moomoo実測(手動転記)シェアを並べ、時点ごとのスピアマン順位相関(4tier)と
    tierごとの誤差(diff=kabu-manual)を添えて返す。read-only(tier_flow_manual_log.pyの
    read_all_observations()を使うだけ・既存の凍結台帳には一切触れない)。

    ★時刻の対応づけ: 手動ログのtime(HH:MM=スクリーンショット取得時刻)に対し、kabu tick側は
    「その日の寄付からその時刻までの累積」を用いる(厳密な1tick最近傍マッチではない)。
    tier_flow_manual_log.csvの値自体が寄付からの累積であることは
    tick_tier_classifier.pyの校正で確認済みの前提のため、この対応づけが正しい比較になる。
    分単位のズレ(スクショ取得の数十秒の揺れ)は許容範囲として扱う。

    manual_rows: テスト用に外部から観測行を注入したい場合に指定(省略時は
    tier_flow_manual_log.read_all_observations()から当日分を読む)。
    """
    if manual_rows is None:
        manual_rows = [r for r in tier_flow_manual_log.read_all_observations()
                       if r.get("date") == date_iso]
    rows = []
    for obs in manual_rows:
        as_of = obs["time"]
        report = build_tier_size_report(date_iso, as_of_hhmm=as_of,
                                        thresholds=thresholds, ticks_dir=ticks_dir)
        kabu_shares = {t: report["shares"][t]["share"] for t in ttc.TIER_ORDER}
        manual_shares = _manual_shares_from_row(obs)
        kabu_vec = [kabu_shares[t] for t in ttc.TIER_ORDER]
        manual_vec = [manual_shares[t] for t in ttc.TIER_ORDER]
        rows.append({
            "time": as_of,
            "kabu_shares": kabu_shares,
            "manual_shares": manual_shares,
            "diff": {t: kabu_shares[t] - manual_shares[t] for t in ttc.TIER_ORDER},
            "rho": _spearman_rho(kabu_vec, manual_vec),
            "n_ticks_used": report["shares"]["n_ticks"],
        })
    return rows


def summarize_calibration_error(rows):
    """
    compare_with_manual()の戻り値から、tierごとの平均絶対誤差(MAE・pt=percentage point)を
    計算する純関数。おにやがscreenshot共有のたびに継続モニタリングし、閾値/ロジックの
    改善余地(=どのtierでズレが大きいか)を見つけるための指標。
    rowsが空なら全tier None を返す。
    """
    out = {}
    for t in ttc.TIER_ORDER:
        diffs = [abs(r["diff"][t]) for r in rows]
        out[t] = (sum(diffs) / len(diffs)) if diffs else None
    return out


def format_compare_text(date_iso, rows):
    lines = [f"=== kabu tick推定 vs moomoo実測(手動転記) 285A {date_iso} ==="]
    if not rows:
        lines.append("(tier_flow_manual_log.csvに当日の観測行がありません)")
        return "\n".join(lines)
    lines.append("※時刻はtier_flow_manual_log.csvの記載時刻(分単位)。kabu tick側はその"
                 "時刻までの寄付からの累積を用いる(数十秒のズレは許容範囲)。")
    header = f"{'time':>6s} | " + " | ".join(
        f"{TIER_LABEL[t]}(kabu/moomoo)" for t in ttc.TIER_ORDER) + " | rho"
    lines.append(header)
    rhos = []
    for r in rows:
        cells = []
        for t in ttc.TIER_ORDER:
            cells.append(f"{r['kabu_shares'][t]*100:4.1f}%/{r['manual_shares'][t]*100:4.1f}%")
        rho_str = f"{r['rho']:.2f}" if r["rho"] is not None else "n/a"
        if r["rho"] is not None:
            rhos.append(r["rho"])
        lines.append(f"{r['time']:>6s} | " + " | ".join(cells) + f" | {rho_str}")
    if rhos:
        lines.append(f"平均rho={sum(rhos)/len(rhos):.2f}(n={len(rhos)}点)"
                     " ※2026-08-27校正時点の参考値rho≈0.6-0.8と比較")
    lines.append("※rhoはtierの出来高シェア順位の一致度(方向は一切含まない)。")

    # ---- 誤差詳細(おにやの継続モニタリング用・2026-08-27追加) ----
    lines.append("")
    lines.append("--- 誤差詳細(推定-実測、pt=percentage point) ---")
    lines.append(f"{'time':>6s} {'tier':6s} {'推定':>7s} {'実測':>7s} {'差':>8s}")
    for r in rows:
        for t in ttc.TIER_ORDER:
            k = r["kabu_shares"][t] * 100
            m = r["manual_shares"][t] * 100
            d = r["diff"][t] * 100
            lines.append(f"{r['time']:>6s} {TIER_LABEL[t]:6s} {k:6.1f}% {m:6.1f}% {d:+7.1f}pt")
    mae = summarize_calibration_error(rows)
    lines.append("")
    lines.append(f"--- tierごとの平均絶対誤差(MAE・全{len(rows)}点平均) ---")
    for t in ttc.TIER_ORDER:
        v = mae[t]
        lines.append(f"  {TIER_LABEL[t]}: {v * 100:.1f}pt" if v is not None else f"  {TIER_LABEL[t]}: n/a")
    lines.append("※MAEが大きいtierほど閾値/ロジックの改善余地がある可能性(継続モニタリング指標)。")
    return "\n".join(lines)


# ============================================================================
# CLI
# ============================================================================
def main():
    ap = argparse.ArgumentParser(
        description="kabu tickから285Aのtierサイズ構成比を表示する(方向は出さない・read-only)")
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD(省略時は本日)")
    ap.add_argument("--as-of", default=None,
                    help="HH:MM(省略時はその日の直近=全tick)")
    ap.add_argument("--compare-manual", "--compare-with-manual-log",
                    dest="compare_manual", action="store_true",
                    help="tier_flow_manual_log.csvの当日分と誤差を明示的に表示する"
                         "(★同日に観測行があれば、このフラグを付けなくても自動表示される。"
                         "付けると観測行が無い日でも『観測行なし』の案内を出す)")
    ap.add_argument("--selftest", action="store_true",
                    help="合成tick(実データ非依存)でロジックの自己テストを実行して終了")
    args = ap.parse_args()

    if args.selftest:
        import sys
        sys.exit(1 if _run_selftests() else 0)

    date_iso = args.date or dt.date.today().isoformat()

    report = build_tier_size_report(date_iso, as_of_hhmm=args.as_of)
    print(format_report_text(report))

    # ★2026-08-27追加(おにや21:59要望): tier_flow_manual_log.csvに当日分の観測行が
    # あれば、フラグ無しでも自動的に較正誤差を表示する(継続モニタリング目的)。
    # --compare-manual/--compare-with-manual-log を明示すると、観測行が無い日でも
    # その旨の案内を出す(サイレントに何も出さないと「今日は比較していない」のか
    # 「観測行が無かった」のか区別できないため)。
    rows = compare_with_manual(date_iso)
    if args.compare_manual or rows:
        print()
        print(format_compare_text(date_iso, rows))


# ============================================================================
# 自己テスト(合成tick・実データ/ファイル非依存。tier_flow_manual_log.pyの
# _run_selftests()と同じ「モジュール内--selftestフラグ」パターン)
# ============================================================================
def _run_selftests():
    fails = []

    def ck(name, cond):
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # ---- _ranks / _spearman_rho ----
    ck("_ranks: 単純昇順", _ranks([10, 20, 30]) == [1.0, 2.0, 3.0])
    ck("_ranks: タイは平均順位", _ranks([10, 10, 30]) == [1.5, 1.5, 3.0])
    ck("_spearman_rho: 完全一致 -> 1.0",
       abs(_spearman_rho([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-9)
    ck("_spearman_rho: 完全逆順 -> -1.0",
       abs(_spearman_rho([1, 2, 3, 4], [40, 30, 20, 10]) - (-1.0)) < 1e-9)
    ck("_spearman_rho: 全同値(分散0) -> None",
       _spearman_rho([1, 1, 1], [2, 3, 4]) is None)
    ck("_spearman_rho: 長さ不一致 -> None",
       _spearman_rho([1, 2], [1, 2, 3]) is None)

    # ---- build_tier_size_report(合成ticks_dirを使い実ファイル非依存) ----
    import tempfile
    import csv as _csv
    tmpdir = tempfile.mkdtemp(prefix="tier_size_report_selftest_")
    date_iso = "2026-01-05"
    path = os.path.join(tmpdir, f"ticks_285A_{date_iso}.csv")
    # bid/ask列はtick_tier_classifier.infer_directionの前提通り「反転」させて作る
    # (真ask=bid列, 真bid=ask列)。price>=真askならBUY。
    rows = [
        {"time": f"{date_iso} 09:00:00", "price": "52000", "tickvol": "100",
         "bid": "51900", "ask": "52000"},                                  # 5.2M -> small
        {"time": f"{date_iso} 09:00:01", "price": "51900", "tickvol": "1000",
         "bid": "52100", "ask": "51900"},                                  # 51.9M -> mid(DEFAULT: 50M<=x<200M)
        {"time": f"{date_iso} 09:01:00", "price": "52500", "tickvol": "20000",
         "bid": "52400", "ask": "52300"},                                  # 1050M -> super(09:01台=別分に配置)
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["time", "price", "tickvol", "bid", "ask"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    try:
        report = build_tier_size_report(date_iso, thresholds=ttc.DEFAULT_THRESHOLDS,
                                        ticks_dir=tmpdir)
        ck("build_tier_size_report: n_ticks_total == 3", report["n_ticks_total"] == 3)
        sh = report["shares"]
        ck("build_tier_size_report: n_ticks(分類対象) == 3", sh["n_ticks"] == 3)
        ck("build_tier_size_report: super share == 1050/(5.2+51.9+1050)",
           abs(sh["super"]["share"] - 1050e6 / (5.2e6 + 51.9e6 + 1050e6)) < 1e-6)
        ck("build_tier_size_report: 方向キーが無い(サイズのみの確認)",
           set(sh["super"].keys()) == {"amount_yen", "share"})

        # as_of で絞り込み(09:00:01まで=最初の2件のみ)
        report_w = build_tier_size_report(date_iso, as_of_hhmm="09:00", thresholds=ttc.DEFAULT_THRESHOLDS,
                                          ticks_dir=tmpdir)
        ck("build_tier_size_report: as_of='09:00' は09:00台の2tickのみ対象",
           report_w["shares"]["n_ticks"] == 2)

        # 存在しない日付 -> 空
        report_empty = build_tier_size_report("1999-01-01", ticks_dir=tmpdir)
        ck("build_tier_size_report: 存在しない日付ファイル -> n_ticks_total=0",
           report_empty["n_ticks_total"] == 0)
        ck("build_tier_size_report: 存在しない日付 -> total_amount_yen=0",
           report_empty["shares"]["total_amount_yen"] == 0.0)

        text = format_report_text(report)
        ck("format_report_text: 文字列化でエラーなく主要語を含む",
           "超大口" in text and "小口" in text)

        text_empty = format_report_text(report_empty)
        ck("format_report_text: 0件でも例外を出さず案内文を返す",
           "0件" in text_empty or "計算できません" in text_empty)

        # ---- compare_with_manual(合成manual_rowsで実CSV非依存) ----
        manual_rows = [{
            "date": date_iso, "time": "09:00",
            "mega_buy": "5.0", "mega_sell": "0.0",       # super=5.0
            "large_buy": "0.3", "large_sell": "0.0",     # big=0.3
            "mid_buy": "0.05", "mid_sell": "0.0",        # mid=0.05
            "small_buy": "0.02", "small_sell": "0.0",    # small=0.02 (moomoo側もsuper最大の順位)
        }]
        cmp_rows = compare_with_manual(date_iso, thresholds=ttc.DEFAULT_THRESHOLDS,
                                       ticks_dir=tmpdir, manual_rows=manual_rows)
        ck("compare_with_manual: 1件返る", len(cmp_rows) == 1)
        ck("compare_with_manual: rhoが計算できる(4tierとも順位が一意)",
           cmp_rows[0]["rho"] is not None)
        # ---- diff/MAE(2026-08-27追加・おにや21:59要望=較正誤差の継続モニタリング) ----
        ck("compare_with_manual: diffキーがkabu-manualで一致する",
           all(abs(cmp_rows[0]["diff"][t]
                   - (cmp_rows[0]["kabu_shares"][t] - cmp_rows[0]["manual_shares"][t])) < 1e-9
               for t in ttc.TIER_ORDER))
        mae = summarize_calibration_error(cmp_rows)
        ck("summarize_calibration_error: 1点だけならMAE==|diff|そのもの",
           all(abs(mae[t] - abs(cmp_rows[0]["diff"][t])) < 1e-9 for t in ttc.TIER_ORDER))
        ck("summarize_calibration_error: 観測0件 -> 全tier None",
           all(v is None for v in summarize_calibration_error([]).values()))
        cmp_text = format_compare_text(date_iso, cmp_rows)
        ck("format_compare_text: 文字列化でエラーなくrhoを含む", "rho" in cmp_text)
        ck("format_compare_text: 誤差詳細セクション(MAE)を含む", "MAE" in cmp_text and "pt" in cmp_text)

        # 観測行が無い日 -> 空リスト・案内文
        cmp_rows_none = compare_with_manual(date_iso, thresholds=ttc.DEFAULT_THRESHOLDS,
                                            ticks_dir=tmpdir, manual_rows=[])
        ck("compare_with_manual: 観測行0件 -> 空リスト", cmp_rows_none == [])
        cmp_text_none = format_compare_text(date_iso, cmp_rows_none)
        ck("format_compare_text: 観測行0件でも案内文を返す", "ありません" in cmp_text_none)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\nPASS" if not fails else f"\nFAIL: {len(fails)}")
    for name in fails:
        print("  - " + name)
    return len(fails)


if __name__ == "__main__":
    main()
