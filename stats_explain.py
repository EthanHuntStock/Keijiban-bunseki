# -*- coding: utf-8 -*-
"""『統計評価』タブを平易に読ませるための表示用の素材(2026-11-25新設・ユーザー承認)。純関数・I/Oなし。

重要: ここは表示と説明だけ。元の統計(research/comprehensive_stats.py が書き出すCSVのr・diff_pt・t・n)は一切変えない。
指標ごとの『結論の1行』は下の固定ルールで機械的に決める(判定基準は GLOSSARY / RULES として画面にも出す)。

判定ルール(多重比較=指標5×期間3=15セルを踏まえ、保守的に):
  ・信頼できる(✅): n>=30 かつ r と 差(pt) の向きが一致 かつ rの絶対値>=0.30 かつ tの絶対値>=3.0(15セル補正後でも偶然とは言いにくい目安)
  ・関係なし(➖)  : n>=30 かつ 全期間で rの絶対値<0.10 かつ tの絶対値<2.0
  ・まだ不明(🟡)  : 上のどちらでもない(件数が少ない/向きが食い違う/やや目立つが偶然の範囲を出ない 等)
  指標×3期間のうち最も強い期間でその指標の結論を決める。
"""
import math

MIN_N = 30            # これ未満は『件数が少なく信用できない』
R_WEAK, R_MID = 0.10, 0.30
T_CHANCE, T_STRONG = 2.0, 3.0

INTRO = [
    "このタブは「掲示板の気分(煽り度・強気比率・投稿量)と、その後の株価の値動きに関係があるのか」を過去データで調べた結果です。",
    "関係があるとしても、どの程度当てになるのかを、指標ごとに ✅信頼できる／🟡まだ不明／➖関係なし で先頭に示します。",
    "結論は「予測に使える」という意味ではありません。調べた期間が短く、検証中の研究です。",
]

# (用語, 意味, 目安の読み方)
GLOSSARY = [
    ("相関係数 r", "指標が高い日ほど、その後の株価リターンも高い(+)か低い(−)かを -1〜+1 で表した値。",
     "rの絶対値が0.1未満=ほぼ関係なし／0.1〜0.3=弱い関係／0.3以上=やや関係あり(ただし件数が少ないと偶然でも出る)"),
    ("t値", "「指標が高い日のグループ」と「低い日のグループ」の平均リターン差が、偶然でも出る程度かを測る値。",
     "tの絶対値が2未満=偶然の範囲／2〜3=要注意(まだ不明)／3以上=偶然とは考えにくい(ただし15個の中の1つだけなら偶然も混ざる)"),
    ("p値", "もし本当は関係がなかったとして、偶然でここまで差が出る確率。小さいほど偶然では説明しにくい。",
     "このタブでは表示せず、t値で代用。目安は tの絶対値≈2 でp値およそ5%(20回に1回は偶然で出る水準)"),
    ("高群−低群の差(pt)", "指標が高い日と低い日で、その後のリターンの平均が何ポイント違うか。",
     "プラスなら「指標が高い日のほうがその後高い」。t値が小さければ偶然の差の可能性が高い"),
    ("サンプル数 n", "何日分のデータで計算したか。", "30日未満は信用できない。多いほど安定(今回は約40日分=少なめ)"),
]

NOTES = [
    "この表の数字は過去の傾向の統計で、将来の値動きを予測できるとは限りません(予測に使えるかはこれから前向きに検証中)。",
    "指標5つ×期間3つ=15マスを同時に見ているため、偶然で数マスは目立ちます。1つだけ目立つマスは偶然の可能性が高いです。",
    "rとt値は別々の計算(rは全日の並び、tは高群/低群の2分割)なので、向きが食い違うことがあります。その場合は「まだ不明」にします。",
    "売買の推奨ではありません。",
]

LEVELS = {
    "trust": {"symbol": "✅", "word": "信頼できる", "tone": "green"},
    "unknown": {"symbol": "🟡", "word": "まだ不明", "tone": "yellow"},
    "none": {"symbol": "➖", "word": "関係なし", "tone": "muted"},
}


def _sgn(x):
    return 0 if not x else (1 if x > 0 else -1)


def cell_level(c):
    """1マス(指標×期間)の判定。cは r, t, diff_pt, n を持つdict。戻り値 'trust'/'unknown'/'none'。"""
    r, t, d, n = c.get("r"), c.get("t"), c.get("diff_pt"), c.get("n")
    try:
        n = int(float(n)) if n is not None else None
    except (TypeError, ValueError):
        n = None
    if r is None or t is None or n is None:
        return "unknown"
    if n < MIN_N:
        return "unknown"
    same_dir = d is None or _sgn(r) == _sgn(d) or abs(r) < 0.02
    if same_dir and abs(r) >= R_MID and abs(t) >= T_STRONG:
        return "trust"
    if abs(r) < R_WEAK and abs(t) < T_CHANCE:
        return "none"
    return "unknown"


_RANK = {"trust": 2, "unknown": 1, "none": 0}


def _score(c):
    try:
        return max(abs(c.get("r") or 0.0) / R_MID, abs(c.get("t") or 0.0) / T_STRONG)
    except Exception:
        return 0.0


def reason(c, level):
    """その指標の最有力マスの要点(平易な一言)。"""
    r, t, n = c.get("r"), c.get("t"), c.get("n")
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        n = None
    base = "%s: r=%+.2f・t=%+.2f・%s日分" % (c.get("horizon_name", ""), r if r is not None else 0.0,
                                         t if t is not None else 0.0, n if n is not None else "?")
    if level == "trust":
        return base + "。関係の強さが偶然とは考えにくい水準(それでも予測に使えるとは限りません)"
    if level == "none":
        return base + "。全期間で関係はほぼ見えません"
    if n is not None and n < MIN_N:
        why = "件数が少なく信用できません"
    elif r is not None and t is not None and abs(r) >= R_WEAK and abs(t) < T_CHANCE:
        why = "やや関係があるように見えますが、偶然の範囲です"
    elif t is not None and abs(t) >= T_CHANCE and (r is None or abs(r) < R_MID):
        why = "グループ差は目立ちますが、相関は弱く、向きや大きさがそろっていません"
    else:
        why = "偶然の範囲を超えたとは言えません"
    return base + "。" + why


def verdicts(rows):
    """rows=CSV行(dict: metric_name, horizon_name, r, t, diff_pt, n)のlist → 指標ごとの結論リスト(元の並び順)。"""
    order, by = [], {}
    for c in rows or []:
        m = c.get("metric_name")
        if m not in by:
            by[m] = []
            order.append(m)
        by[m].append(c)
    out = []
    for m in order:
        cells = by[m]
        lv_cells = [(cell_level(c), c) for c in cells]
        top_rank = max(_RANK[l] for l, _ in lv_cells)
        # 結論=最も強い判定。none は『全マスが none』の時だけ
        level = {2: "trust", 1: "unknown", 0: "none"}[top_rank]
        cand = [c for l, c in lv_cells if _RANK[l] == top_rank]
        best = max(cand, key=_score)
        out.append({"metric": m, "level": level, "symbol": LEVELS[level]["symbol"], "word": LEVELS[level]["word"],
                    "tone": LEVELS[level]["tone"], "line": "%s %s｜%s" % (LEVELS[level]["symbol"], LEVELS[level]["word"], reason(best, level)),
                    "best_horizon": best.get("horizon_name")})
    return out


def summary_line(vs):
    """全体の一言(件数の内訳)。"""
    n = {k: sum(1 for v in vs if v["level"] == k) for k in LEVELS}
    return "%d指標の結論: ✅信頼できる %d／🟡まだ不明 %d／➖関係なし %d" % (len(vs), n["trust"], n["unknown"], n["none"])
