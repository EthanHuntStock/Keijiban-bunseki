# -*- coding: utf-8 -*-
"""
backtest.py - baseline vs candidate の損益シミュ = 唯一の昇格KPI(仕様§10)。

規律: 相関/CCF/AUC は screening 専用で「改善」の根拠にしない。改善は
baseline-vs-candidate の Δpnl(起点日明示)が真OOSで正の時のみ主張する
([[feedback-kpi-is-pnl-simulation]])。IS seed(is_oos=false)と真OOSは分離。
方向は 未検証 ゆえ本シミュは「サイズ調整/レンジ日フィルタ」であって方向オラクルではない。
純関数(台帳行+日足のみ)。
"""
import math

import config


# ============================================================================
# ルール(row -> size_mult ∈ [0,1])。方向でなくサイズ/フィルタ。
# ============================================================================
def rule_baseline(row):
    """常時フルサイズ(=何もしないベースライン)。"""
    return 1.0


def rule_vol_sized(row):
    """高 vol_regime_score でサイズ縮小(sl恒久2.0%は別途前提)。"""
    vrs = _f(row.get("vol_regime_score"))
    if vrs is None:
        return 1.0
    return max(0.3, min(1.0, 1.0 - 0.6 * vrs))


def rule_range_standdown(row):
    """高 range_day_score(チョップ期待)で枚数を落とす/見送り。"""
    rds = _f(row.get("range_day_score"))
    if rds is None:
        return 1.0
    return max(0.4, min(1.0, 1.0 - 0.5 * rds))


def rule_contrarian_dir(row):
    """おにや式・逆張りの売買ロジック(未検証)＝方向を「検証可能」にする最小ルール。
    方向candidateを符号付きポジション[-1,1]に落とす:
      fade_down(総悲観の底)→ +strength(ロング=買い) /
      fade_up(過熱の天井)  → -strength(ショート=売り) /
      none                 →  0(ノートレード)。
    これで pnl = position * forward_return となり、baseline(常時ロング)や flat と損益比較できる。
    ※方向は未検証。真OOSの Δpnl(+PBO/人承認)が出るまで"候補"であって売買助言ではない。"""
    side = row.get("dir_candidate_side")
    stg = _f(row.get("dir_candidate_strength"))
    stg = 0.0 if stg is None else max(0.0, min(1.0, stg))
    if side == "fade_down":
        return +stg
    if side == "fade_up":
        return -stg
    return 0.0


def rule_flat(row):
    """無ポジション(方向ルールの中立ベースライン=常時ロングでなく0)。"""
    return 0.0


BUILTIN_RULES = {
    "baseline": rule_baseline,
    "vol_sized": rule_vol_sized,
    "range_standdown": rule_range_standdown,
    "contrarian_dir": rule_contrarian_dir,
    "flat": rule_flat,
}


# ============================================================================
# シミュ
# ============================================================================
def simulate_rule(rule, ledger_rows, price_daily=None, *, is_oos_only=True,
                  horizon="1d", roundtrip_cost=None):
    """
    リアルなfill前提の簡易サイズ・シミュ。各成熟した凍結OOS日を1トレードとし、
    その日の frozen シグナルで size を決め、実現 forward_return_<horizon> を取る
    (シグナルは凍結後=先読みなし)。rule は callable(row)->size or ルール名。

    ★コスト補正(連携ログ 2026-07-09 番犬systemic警鐘=全プロトのΔNetはgross偏り)：
    各アクティブ日に往復コスト |pos|*roundtrip_cost を net から差し引く(=唯一KPIはnet)。
    トレード頻度の高い戦略ほどコストを払う(方向系=極値日のみ発火は往復少=有利に働く)。
    roundtrip_cost=None は config.BACKTEST_ROUNDTRIP_COST(実測spread≈0.06%)。0で gross。
    戻り値: {n, pnls(net), pnls_gross, sizes, dates, roundtrip_cost, cost_total}。純関数。
    """
    if isinstance(rule, str):
        rule = BUILTIN_RULES.get(rule, rule_baseline)
    if roundtrip_cost is None:
        roundtrip_cost = getattr(config, "BACKTEST_ROUNDTRIP_COST", 0.0006)
    col = f"forward_return_{horizon}"
    pnls, gross, sizes, dates = [], [], [], []
    cost_total = 0.0
    for r in ledger_rows or []:
        if is_oos_only and str(r.get("is_oos")) != "True":
            continue
        ret = _f(r.get(col))
        if ret is None:
            continue    # 未成熟
        # ポジション[-1,1]: サイズ系ルールは[0,1](ロング)、方向系は符号付き(ショート可)。
        pos = max(-1.0, min(1.0, float(rule(r))))
        g = pos * ret
        c = abs(pos) * roundtrip_cost      # 建て+返しの往復コスト(名目比・size でスケール)
        gross.append(g)
        pnls.append(g - c)                 # net(コスト後)=唯一KPI
        cost_total += c
        sizes.append(pos)
        dates.append(r.get("date"))
    return {"n": len(pnls), "pnls": pnls, "pnls_gross": gross, "sizes": sizes,
            "dates": dates, "roundtrip_cost": roundtrip_cost,
            "cost_total": round(cost_total, 6)}


def pnl_kpi(sim):
    """pnl/trade・累積・勝率・平均勝ち/負け・Sharpe様・最大DD。純関数。"""
    pnls = sim.get("pnls", [])
    n = len(pnls)
    if n == 0:
        return {"n": 0, "cum_pnl": None, "pnl_per_trade": None, "win_rate": None,
                "avg_win": None, "avg_loss": None, "sharpe_like": None, "max_dd": None}
    cum = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    mean = cum / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / n) if n > 1 else 0.0
    # equity / max drawdown
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {
        "n": n,
        "cum_pnl": round(cum, 6),
        "pnl_per_trade": round(mean, 6),
        "win_rate": round(len(wins) / n, 3),
        "avg_win": round(sum(wins) / len(wins), 6) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 6) if losses else None,
        "sharpe_like": round(mean / sd, 3) if sd > 0 else None,
        "max_dd": round(dd, 6),
    }


def baseline_vs_candidate(baseline_sim, candidate_sim, *, start_date):
    """
    Δpnl(起点日明示)= 昇格メトリクス。真OOSで正の時のみ「改善」を主張。純関数。
    """
    b = pnl_kpi(baseline_sim)
    c = pnl_kpi(candidate_sim)
    if not b["n"] or not c["n"]:
        return {"start_date": start_date, "verdict": "REJECT(標本不足)",
                "n": min(b["n"], c["n"]), "baseline": b, "candidate": c,
                "delta_cum_pnl": None, "delta_sharpe": None}
    dcum = round((c["cum_pnl"] or 0) - (b["cum_pnl"] or 0), 6)
    dsh = (round((c["sharpe_like"] or 0) - (b["sharpe_like"] or 0), 3)
           if (c["sharpe_like"] is not None and b["sharpe_like"] is not None) else None)
    enough = min(b["n"], c["n"]) >= config.SIG_MIN_CALIB_DAYS
    if not enough:
        verdict = f"REJECT(OOS標本不足 n={min(b['n'], c['n'])}/{config.SIG_MIN_CALIB_DAYS})"
    elif dcum > 0 and (dsh is None or dsh >= 0):
        verdict = "候補: Δpnl>0(要PBO/人承認)"
    else:
        verdict = "REJECT(Δpnl<=0)"
    return {"start_date": start_date, "verdict": verdict,
            "n": min(b["n"], c["n"]), "baseline": b, "candidate": c,
            "delta_cum_pnl": dcum, "delta_sharpe": dsh}


def run_default_grid(ledger_rows, price_daily=None, *, start_date=None):
    """
    内蔵ルール群を baseline と比較(真OOSのみ)。ダッシュボード④⑤用の要約。純関数。
    現状は標本不足で全ルール REJECT が既定(正直な verdict)。
    """
    start_date = start_date or config.HARNESS_START_DATE
    base = simulate_rule("baseline", ledger_rows, price_daily)
    out = {}
    for name in ("vol_sized", "range_standdown", "contrarian_dir"):
        cand = simulate_rule(name, ledger_rows, price_daily)
        out[name] = baseline_vs_candidate(base, cand, start_date=start_date)
    return out


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================================
# 純関数テスト(方向=符号付き売買ロジックの損益が正しく出るか)
# ============================================================================
def _run_selftests():
    fails = []

    def check(name, cond):
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # 逆張りルールの符号: fade_down→+ / fade_up→- / none→0
    check("contrarian fade_down -> long(+)",
          rule_contrarian_dir({"dir_candidate_side": "fade_down",
                               "dir_candidate_strength": "0.8"}) == 0.8)
    check("contrarian fade_up -> short(-)",
          rule_contrarian_dir({"dir_candidate_side": "fade_up",
                               "dir_candidate_strength": "0.7"}) == -0.7)
    check("contrarian none -> flat(0)",
          rule_contrarian_dir({"dir_candidate_side": "none"}) == 0.0)

    # ショートは下落(ret<0)で利益・上昇(ret>0)で損失
    rows = [
        {"is_oos": "True", "dir_candidate_side": "fade_up",
         "dir_candidate_strength": "1.0", "forward_return_1d": "-0.02"},   # 天井売り→下落=+0.02
        {"is_oos": "True", "dir_candidate_side": "fade_down",
         "dir_candidate_strength": "1.0", "forward_return_1d": "0.03"},    # 底買い→上昇=+0.03
        {"is_oos": "True", "dir_candidate_side": "none",
         "dir_candidate_strength": "", "forward_return_1d": "0.05"},       # ノートレード=0
    ]
    # gross の符号/値(roundtrip_cost=0 でコスト無効化して素の機構を検証)
    sim = simulate_rule("contrarian_dir", rows, roundtrip_cost=0.0)
    check("signed sim n=3", sim["n"] == 3)
    check("short profits on drop (+0.02)", abs(sim["pnls"][0] - 0.02) < 1e-9)
    check("long profits on rise (+0.03)", abs(sim["pnls"][1] - 0.03) < 1e-9)
    check("none -> 0 pnl", abs(sim["pnls"][2]) < 1e-9)
    kpi = pnl_kpi(sim)
    check("cum_pnl = 0.05 (gross)", abs((kpi["cum_pnl"] or 0) - 0.05) < 1e-9)

    # baseline(常時ロング)は同じ3日で -0.02+0.03+0.05 = 0.06、逆張りは 0.05
    base = simulate_rule("baseline", rows, roundtrip_cost=0.0)
    check("baseline long-only cum = 0.06 (gross)",
          abs((pnl_kpi(base)["cum_pnl"] or 0) - 0.06) < 1e-9)
    bvc = baseline_vs_candidate(base, sim, start_date="2026-07-08")
    check("baseline_vs_candidate has delta", bvc["delta_cum_pnl"] is not None)
    # 標本不足(<SIG_MIN_CALIB_DAYS)は REJECT が正
    check("small-sample verdict REJECT", "REJECT" in bvc["verdict"])

    # ---- コスト補正(番犬systemic: gross偏りを避ける) ----
    rc = 0.001
    sim_c = simulate_rule("contrarian_dir", rows, roundtrip_cost=rc)
    base_c = simulate_rule("baseline", rows, roundtrip_cost=rc)
    # 逆張りはアクティブ2日(|pos|=1)ゆえ cost=2*rc、baselineは3日ゆえ 3*rc
    check("contrarian cost = 2*rc", abs(sim_c["cost_total"] - 2 * rc) < 1e-9)
    check("baseline cost = 3*rc", abs(base_c["cost_total"] - 3 * rc) < 1e-9)
    check("net = gross - cost (contrarian)",
          abs((pnl_kpi(sim_c)["cum_pnl"] or 0) - (0.05 - 2 * rc)) < 1e-9)
    check("pnls_gross preserved", abs(sum(sim_c["pnls_gross"]) - 0.05) < 1e-9)
    # 高頻度(baseline)ほどコスト大=方向系(極値のみ発火)が相対的に有利になる方向
    check("fewer-trade rule pays less cost", sim_c["cost_total"] < base_c["cost_total"])
    check("default cost from config applied",
          simulate_rule("baseline", rows)["roundtrip_cost"] == config.BACKTEST_ROUNDTRIP_COST)

    print(f"\n{'PASS' if not fails else 'FAIL'}: {len(fails)} failure(s)")
    return len(fails)


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_selftests() else 0)
