"""Command-line interface:  python -m gridmind <command> [options]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from gridmind import __version__
from gridmind.analysis import binding_constraints, congestion_analysis, line_utilization, run_sensitivity
from gridmind.benchmark import (aggregate_benchmark_summary, compare_to_baseline, evaluate_scenario,
                                export_csv, run_benchmark, run_mp_benchmark)
from gridmind.multiperiod import ScheduleProblem, load_schedule_problem, sample_schedule_problem
from gridmind.network import Network, load_network
from gridmind.optimizer import ENV_SOLVER_PATH, SolveResult, solve_dispatch
from gridmind.resilience import run_resilience_suite
from gridmind.control import (CONTROLLER_MODES, FAILURE_POLICIES, ControlConfig,
                              compare_control, write_control_result)
from gridmind.experiment import (ExperimentConfig, default_experiment_config,
                                 run_comparative_benchmark, write_experiment)
from gridmind.risk_eval import compare_policies
from gridmind.scheduling import BATTERY_MODES, ScheduleResult, solve_schedule
from gridmind.uncertainty import UncertaintyConfig, generate_scenarios, split_scenarios
from gridmind.scenarios import (ALL_HORIZON_SCENARIOS, ALL_SCENARIOS, CORE_HORIZON_SCENARIOS,
                                CORE_SCENARIOS, HorizonScenario, apply_scenario, get_horizon_scenario,
                                get_scenario, generate_random_horizon_scenarios,
                                generate_random_scenarios)

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=float), encoding="utf-8")


def _fmt(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:,.{nd}f}"


def _print_result(net: Network, res: SolveResult) -> None:
    print(f"status={res.status} solver={res.solver} termination={res.termination_condition} "
          f"runtime={res.runtime_seconds:.4f}s")
    print(f"message: {res.message}")
    if res.status != "optimal":
        return
    print(f"objective = {_fmt(res.objective)} USD (generation {_fmt(res.generation_cost)} + unmet penalty {_fmt(res.unmet_penalty_cost)})")
    print(f"demand {_fmt(res.total_demand_mw)} MW | served {_fmt(res.served_demand_mw)} MW | unmet {_fmt(res.unmet_demand_mw)} MW")
    print("dispatch (MW):")
    for g in net.generators:
        print(f"  {g.id:<14} {res.dispatch[g.id]:>9.2f} / limit {g.effective_limit_mw:>7.2f}  @ {g.cost_per_mwh:g} USD/MWh")
    print("line flows (MW, + = from->to):")
    for u in line_utilization(net, res.flows):
        util = "out of service" if not u["in_service"] else f"{100 * u['utilization']:.1f}% of {u['capacity_mw']:g}"
        print(f"  {u['line']:<20} {u['flow_mw']:>9.2f}  {util}")
    bind = binding_constraints(net, res)
    if bind:
        print("binding constraints: " + ", ".join(f"{b['type']}:{b['id']}" for b in bind))
    if res.validation:
        print(f"independent validation: {'PASSED' if res.validation.passed else 'FAILED'}")


def _load(args: argparse.Namespace) -> Network:
    net = load_network(args.network) if args.network else load_network()
    if getattr(args, "penalty", None) is not None:
        from dataclasses import replace
        net = replace(net, unmet_penalty_per_mwh=args.penalty)
    return net


def _load_horizon(args: argparse.Namespace) -> ScheduleProblem:
    prob = load_schedule_problem(args.horizon) if args.horizon else sample_schedule_problem()
    if getattr(args, "network", None):
        from dataclasses import replace
        prob = replace(prob, network=load_network(args.network))
    if getattr(args, "penalty", None) is not None:
        from dataclasses import replace
        prob = replace(prob, network=replace(prob.network, unmet_penalty_per_mwh=args.penalty))
    prob.validate()
    return prob


def _print_schedule(res: ScheduleResult) -> None:
    print(f"status={res.status} solver={res.solver} termination={res.termination_condition} "
          f"runtime={res.runtime_seconds:.4f}s")
    print(f"message: {res.message}")
    if res.status != "optimal":
        return
    print(f"periods={res.periods} step_hours={res.step_hours} "
          f"objective={_fmt(res.objective)} USD "
          f"(generation {_fmt(res.generation_cost_usd)} + unmet {_fmt(res.unmet_penalty_cost_usd)} "
          f"+ battery throughput {_fmt(res.battery_throughput_cost_usd)})")
    print(f"demand {_fmt(res.total_demand_mwh)} MWh | served {_fmt(res.served_demand_mwh)} MWh "
          f"| unmet {_fmt(res.unmet_energy_mwh)} MWh ({100.0 - res.demand_served_pct:.2f}% unserved)")
    if res.max_unmet_periods:
        print(f"max unmet {_fmt(res.max_unmet_mw)} MW in periods {list(res.max_unmet_periods)}")
    if res.validation is not None:
        print(f"validation: passed={res.validation.passed} "
              f"(failed checks: {[c.name for c in res.validation.failed()]})")
        print(f"battery_mode={res.battery_mode} battery_exclusive={res.validation.battery_exclusive}")


def cmd_check_solver(args: argparse.Namespace) -> int:
    from gridmind.solver import solver_inventory
    inv = solver_inventory()
    ok_any = False
    for row in inv:
        print(f"solver={row['solver']:<12} available={row['available']}  {row['detail']}")
        ok_any = ok_any or row["available"]
    if not ok_any:
        print(f"Set {ENV_SOLVER_PATH} to your glpsol.exe, e.g. set {ENV_SOLVER_PATH}=C:\\glpk\\w64\\glpsol.exe")
        print("Or `pip install highspy` for the portable HiGHS fallback.")
    return 0 if ok_any else 2


def cmd_scenarios(args: argparse.Namespace) -> int:
    for s in ALL_SCENARIOS:
        print(f"{s.id:<18} {s.name} - {s.description}")
    print("-- multi-period (horizon) scenarios --")
    for s in ALL_HORIZON_SCENARIOS:
        print(f"{s.id:<18} {s.name} - {s.description}")
    return 0


def cmd_solve(args: argparse.Namespace) -> int:
    net = apply_scenario(_load(args), get_scenario(args.scenario))
    res = solve_dispatch(net, allow_unmet=not args.strict)
    _print_result(net, res)
    payload = {"scenario": args.scenario, "result": res.to_dict(), "baseline": compare_to_baseline(net, res)}
    if args.json:
        if args.json == "-":
            print(json.dumps(payload, indent=2, default=float))
        else:
            _write_json(Path(args.json), payload)
            print(f"JSON written to {args.json}")
    return 0 if res.ok else 1


def cmd_compare(args: argparse.Namespace) -> int:
    import pandas as pd
    net = _load(args)
    df = pd.DataFrame([evaluate_scenario(net, s) for s in CORE_SCENARIOS])
    cols = ["scenario_id", "status", "objective_usd", "baseline_cost_usd", "saving_usd", "saving_pct",
            "copperplate_bound_usd", "congestion_cost_usd", "unmet_mw", "runtime_s"]
    print(df[cols].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    out = Path(args.out)
    export_csv(df, out / "comparison.csv")
    _write_json(out / "comparison.json", df.to_dict(orient="records"))
    print(f"wrote {out / 'comparison.csv'} and comparison.json")
    return 0 if (df["status"] == "optimal").all() else 1


def cmd_sensitivity(args: argparse.Namespace) -> int:
    net = apply_scenario(_load(args), get_scenario(args.scenario))
    mults = [float(x) for x in args.multipliers.split(",")] if args.multipliers else None
    df = run_sensitivity(net, args.kind, mults, args.target)
    print(df.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    tag = f"{args.kind}_{args.target}" if args.target else args.kind
    path = export_csv(df, Path(args.out) / f"sensitivity_{tag}.csv")
    print(f"wrote {path}")
    return 0 if (df["status"] == "optimal").all() else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    net = _load(args)
    scenarios = list(CORE_SCENARIOS) + generate_random_scenarios(net, args.n, args.seed)
    df = run_benchmark(net, scenarios)
    path = export_csv(df, args.out)
    counts = df["status"].value_counts().to_dict()
    print(f"{len(df)} scenarios -> {path}\nstatus counts: {counts}")
    print(f"total solver time {df['runtime_s'].sum():.2f}s, mean {df['runtime_s'].mean():.4f}s, "
          f"scenarios with unmet demand: {int((df['unmet_mw'].fillna(0) > 1e-6).sum())}")
    return 0 if (df["status"] == "optimal").all() else 1


def cmd_demo(args: argparse.Namespace) -> int:
    net = _load(args)
    normal_net, outage_net = net, apply_scenario(net, get_scenario("line_outage"))
    normal, outage = solve_dispatch(normal_net), solve_dispatch(outage_net)
    feeder_net = apply_scenario(net, get_scenario("feeder_outage"))
    feeder, strict = solve_dispatch(feeder_net), solve_dispatch(feeder_net, allow_unmet=False)

    print("== 1. Outage changes the optimal dispatch (line-central-east out) ==")
    if normal.status == outage.status == "optimal":
        print(f"{'generator':<14}{'normal MW':>11}{'outage MW':>11}{'change':>9}")
        for g in net.generators:
            a, b = normal.dispatch[g.id], outage.dispatch[g.id]
            print(f"{g.id:<14}{a:>11.2f}{b:>11.2f}{b - a:>+9.2f}")
        print(f"objective: {normal.objective:,.2f} -> {outage.objective:,.2f} USD "
              f"(+{outage.objective - normal.objective:,.2f}); flow on failed line: {outage.flows['line-central-east']:.2f} MW")
    else:
        print(f"could not solve: normal={normal.status} outage={outage.status}")
    print("\n== 2. Network cannot satisfy all demand (line-central-demand out) ==")
    print(f"status={feeder.status}; unmet={_fmt(feeder.unmet_demand_mw)} MW of {_fmt(feeder.total_demand_mw)} MW; "
          f"objective={_fmt(feeder.objective)} USD")
    print("\n== 3. Same case with unmet demand forbidden (strict) -> infeasible, no invented numbers ==")
    print(f"status={strict.status}; objective={strict.objective}; dispatch={strict.dispatch}; message: {strict.message}")
    report = {"outage_vs_normal": {"normal": normal.to_dict(), "outage": outage.to_dict()},
              "unserved_demand": feeder.to_dict(), "strict_infeasible": strict.to_dict(),
              "congestion_normal": congestion_analysis(normal_net) if normal.status == "optimal" else None}
    _write_json(Path(args.out) / "demo_report.json", report)
    print(f"\nwrote {Path(args.out) / 'demo_report.json'}")
    return 0 if all(r.status == "optimal" for r in (normal, outage, feeder)) and strict.status == "infeasible" else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    prob = _load_horizon(args)
    scen = get_horizon_scenario(args.scenario)
    from gridmind.scenarios import apply_horizon_scenario
    stressed = apply_horizon_scenario(prob, scen)
    res = solve_schedule(stressed, allow_unmet=not args.strict, battery_mode=args.battery_mode)
    _print_schedule(res)
    if res.status == "optimal":
        print("per-period (operating cost, unmet):")
        print(f"  {'t':>3} {'demand MWh':>11} {'unmet MWh':>10} {'cost USD':>12}")
        dt = res.step_hours
        for t in range(res.periods):
            demand = sum(stressed.horizon.demand_mw[t].values())
            unmet = sum(res.unmet[n][t] for n in res.unmet)
            print(f"  {t:>3} {dt * demand:>11.2f} {dt * unmet:>10.2f} {res.cost_by_period_usd[t]:>12.2f}")
        for b in res.soc:
            print(f"battery {b} SoC (MWh after each period): "
                  + " ".join(f"{v:.1f}" for v in res.soc[b]))
        print(f"battery totals: charge {dt * sum(sum(s) for s in res.charge.values()):.1f} MWh, "
              f"discharge {dt * sum(sum(s) for s in res.discharge.values()):.1f} MWh")
    payload = {"scenario": scen.id, "horizon": args.horizon or "data/sample_horizon.json",
               "battery_mode": res.battery_mode, "result": res.to_dict()}
    if args.json:
        if args.json == "-":
            print(json.dumps(payload, indent=2, default=float))
        else:
            _write_json(Path(args.json), payload)
            print(f"JSON written to {args.json}")
    return 0 if res.ok else 1


def cmd_resilience(args: argparse.Namespace) -> int:
    prob = _load_horizon(args)
    scenarios: tuple[HorizonScenario, ...] = (
        (get_horizon_scenario(args.scenario),) if args.scenario else CORE_HORIZON_SCENARIOS)
    df = run_resilience_suite(prob, scenarios, battery_mode=args.battery_mode)
    cols = ["scenario_id", "storage", "status", "operating_cost_usd", "unmet_energy_mwh",
            "demand_served_pct", "max_unmet_mw", "max_unmet_periods",
            "battery_discharge_mwh", "cost_savings_vs_no_storage_usd",
            "unmet_reduction_vs_no_storage_mwh", "validation_passed"]
    print(df[cols].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    out = Path(args.out)
    export_csv(df, out / "resilience.csv")
    _write_json(out / "resilience.json", df.to_dict(orient="records"))
    print(f"wrote {out / 'resilience.csv'} and resilience.json")
    return 0 if (df["status"] == "optimal").all() else 1


def cmd_mp_benchmark(args: argparse.Namespace) -> int:
    prob = _load_horizon(args)
    scenarios = list(CORE_HORIZON_SCENARIOS) + generate_random_horizon_scenarios(prob, args.n, args.seed)
    df = run_mp_benchmark(prob, scenarios, battery_mode=args.battery_mode)
    path = export_csv(df, args.out)
    summary = aggregate_benchmark_summary(df)
    print(f"{len(df)} horizon scenarios -> {path}")
    print(f"status counts: {summary['status_counts']}")
    print(f"total solver time {summary['total_solver_runtime_s']:.2f}s, "
          f"mean {summary['mean_solver_runtime_s']:.4f}s, "
          f"scenarios with unmet energy: {summary['scenarios_with_unmet']}")
    _write_json(Path(args.out).with_suffix(".summary.json"), summary)
    return 0 if (df["status"] == "optimal").all() else 1


def cmd_stochastic(args: argparse.Namespace) -> int:
    prob = _load_horizon(args)
    config = UncertaintyConfig(n_scenarios=args.scenarios, seed=args.seed,
                               demand_sigma=args.demand_sigma,
                               renewable_sigma=args.renewable_sigma,
                               demand_ar1=args.demand_ar1)
    scenario_set = generate_scenarios(prob, config)
    in_sample, holdout = split_scenarios(scenario_set, holdout_fraction=args.holdout_fraction)
    record = compare_policies(prob, in_sample, holdout, risk_weight=args.risk_weight,
                              cvar_alpha=args.cvar_alpha, lead_periods=args.lead_periods,
                              battery_mode=args.battery_mode)
    record["uncertainty"] = config.to_dict()
    record["seed"] = args.seed
    record["horizon"] = args.horizon or "data/sample_horizon.json"

    print(f"solver={record['solver'] or 'n/a'} battery_mode={record['battery_mode']} "
          f"lead_periods={record['lead_periods']}")
    print(f"uncertainty: {len(scenario_set.scenarios)} scenarios (seed={args.seed}) -> "
          f"{record['in_sample_scenarios']} in-sample / {record['holdout_scenarios']} holdout")
    r = record["risk"]
    print(f"risk: weight={r['risk_weight']} cvar_alpha={r['cvar_alpha']}")
    print("fit (in-sample):")
    for name, fit in record["fit"].items():
        if name == "deterministic":
            print(f"  {'deterministic':<14} status={fit['status']} objective={_fmt(fit['objective_usd'])} USD")
        else:
            print(f"  {name:<14} status={fit['status']} E={_fmt(fit.get('expected_cost_usd'))} "
                  f"CVaR={_fmt(fit.get('cvar_usd'))} obj={_fmt(fit.get('objective_usd'))} USD")
    print("out-of-sample evaluation (same holdout scenarios for every policy):")
    print(f"  {'policy':<14} {'status':<8} {'E[cost] USD':>14} {'CVaR USD':>14} "
          f"{'worst USD':>14} {'P(unmet)':>9} {'valid':>6}")
    for name, ev in record["evaluations"].items():
        print(f"  {name:<14} {ev['status']:<8} {_fmt(ev['expected_cost_usd']):>14} "
              f"{_fmt(ev['cvar_usd']):>14} {_fmt(ev['worst_case_cost_usd']):>14} "
              f"{'n/a' if ev['probability_of_unmet'] is None else format(ev['probability_of_unmet'], '.2f'):>9} "
              f"{str(ev['validation_all_passed']):>6}")
    print("note: synthetic scenarios - these are model outputs, not real grid measurements.")

    if args.json:
        if args.json == "-":
            print(json.dumps(record, indent=2, default=float))
        else:
            _write_json(Path(args.json), record)
            print(f"JSON written to {args.json}")
    ok = all(ev["status"] == "optimal" for ev in record["evaluations"].values())
    return 0 if ok else 1


def cmd_experiment(args: argparse.Namespace) -> int:
    """Multi-seed comparative policy benchmark -> results/stage5a_benchmark/."""
    prob = _load_horizon(args)
    seeds = tuple(args.seed) if args.seed else default_experiment_config().seeds
    config = ExperimentConfig(
        seeds=seeds, n_scenarios=args.scenarios,
        demand_sigma=args.demand_sigma, renewable_sigma=args.renewable_sigma,
        demand_ar1=args.demand_ar1, holdout_fraction=args.holdout_fraction,
        risk_weight=args.risk_weight, cvar_alpha=args.cvar_alpha,
        lead_periods=args.lead_periods, battery_mode=args.battery_mode)
    out_dir = Path(args.out)
    print(f"stage 5A comparative benchmark: {len(config.seeds)} seeds x "
          f"{config.n_scenarios} scenarios (battery_mode={config.battery_mode})")
    print(f"  risk_weight={config.risk_weight} cvar_alpha={config.cvar_alpha} "
          f"lead_periods={config.lead_periods} holdout_fraction={config.holdout_fraction}")
    record = run_comparative_benchmark(prob, config)

    agg = record.aggregate
    print("aggregate over seeds (out-of-sample, synthetic scenarios):")
    print(f"  {'policy':<14} {'feasible':>9} {'E[cost] USD':>14} {'CVaR USD':>14} "
          f"{'worst USD':>13} {'P(unmet)':>9} {'runtime s':>10}")
    for policy in ("deterministic", "risk_neutral", "risk_aware"):
        p = agg["policies"][policy]
        m = p["metrics"]
        rate = p["feasible_run_rate"]
        print(f"  {policy:<14} {p['feasible_runs']:>4}/{agg['n_runs']:<4} "
              f"{_fmt(m['expected_cost_usd']['mean']):>14} "
              f"{_fmt(m['cvar_usd']['mean']):>14} "
              f"{_fmt(m['worst_case_cost_usd']['mean']):>13} "
              f"{_fmt(m['probability_of_unmet']['mean'], 3):>9} "
              f"{_fmt(m['runtime_seconds']['mean']):>10}"
              + ("" if rate is None else f"  (feasible rate {rate:.0%})"))
    print("paired comparisons (per-scenario realized cost, pooled across seeds):")
    for key, comp in record.comparisons["pairs"].items():
        dz = comp["cohens_dz"]
        print(f"  {comp['comparison']:<32} mean={_fmt(comp['mean_diff_usd'])} USD "
              f"sd={_fmt(comp['sd_diff_usd'])} dz="
              f"{'n/a' if dz is None else format(dz, '.3f')} "
              f"(seeds {comp['seeds_used']}, {comp['n_pairs']} pairs)")
    print("note: effect sizes only - synthetic scenarios, no significance claims.")

    paths = write_experiment(record, out_dir)
    for label, path in paths.items():
        print(f"  wrote {label}: {path}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    """Stage 5B rolling-horizon control vs fixed open-loop schedule."""
    prob = _load_horizon(args)
    config = ControlConfig(
        lookahead=args.lookahead, mode=args.mode, failure_policy=args.failure_policy,
        battery_mode=args.battery_mode, allow_unmet=not args.strict,
        n_scenarios=args.scenarios, risk_weight=args.risk_weight,
        cvar_alpha=args.cvar_alpha, lead_periods=args.lead_periods,
        demand_sigma=args.demand_sigma, renewable_sigma=args.renewable_sigma,
        seed=args.seed, cvar_confidence=args.cvar_confidence)
    comparison = compare_control(prob, config)
    ctrl = comparison.controller
    base = comparison.baseline

    print(f"rolling-horizon control: mode={ctrl.mode} lookahead={ctrl.lookahead} "
          f"battery_mode={ctrl.battery_mode} solver={ctrl.solver or 'n/a'}")
    print(f"  failure_policy={ctrl.failure_policy} forecast seed={args.seed} "
          f"(demand_sigma={config.demand_sigma} renewable_sigma={config.renewable_sigma})")
    print(f"  status={ctrl.status} executed={ctrl.periods_executed}/{prob.periods} "
          f"failed replans={ctrl.failed_replans} recovered={ctrl.recovered_replans}")
    print(f"  realized operating cost {_fmt(ctrl.realized_operating_cost_usd)} USD | "
          f"unmet {_fmt(ctrl.realized_unmet_energy_mwh, 3)} MWh | "
          f"battery throughput {_fmt(ctrl.realized_battery_throughput_mwh, 3)} MWh")
    print(f"  realized CVaR({config.cvar_confidence}) {_fmt(ctrl.realized_cvar_usd)} USD | "
          f"solve time mean {_fmt(ctrl.mean_solve_seconds, 4)}s "
          f"max {_fmt(ctrl.max_solve_seconds, 4)}s")
    print(f"  executed feasibility={ctrl.realized_physical_feasible} | "
          f"execution fallbacks={ctrl.execution_fallbacks} | "
          f"curtailed battery {_fmt(ctrl.realized_battery_curtailed_mwh, 3)} MWh, "
          f"charge {_fmt(ctrl.realized_battery_charge_curtailed_mwh, 3)} MWh, "
          f"renewable {_fmt(ctrl.realized_renewable_curtailed_mwh, 3)} MWh")
    print("fixed open-loop baseline (identical realized conditions):")
    print(f"  status={base.status} solver={base.solver or 'n/a'} | "
          f"executed cost {_fmt(base.realized_operating_cost_usd)} USD | "
          f"unmet {_fmt(base.realized_unmet_energy_mwh, 3)} MWh | "
          f"CVaR {_fmt(base.realized_cvar_usd)} USD")
    print(f"  as-planned cost {_fmt(base.planned_operating_cost_usd)} USD | "
          f"as-planned physically feasible={base.planned_physical_feasible} | "
          f"executed physically feasible={base.realized_physical_feasible} | "
          f"execution fallbacks={base.execution_fallbacks}")
    print("difference (controller - baseline): "
          f"cost {_fmt(comparison.cost_difference_usd)} USD, "
          f"unmet {_fmt(comparison.unmet_difference_mwh, 3)} MWh, "
          f"CVaR {_fmt(comparison.cvar_difference_usd)} USD")
    print("note: synthetic forecast-error model; negative cost difference means the "
          "controller was cheaper. No superiority claim - measured result only.")

    if args.json:
        if args.json == "-":
            print(json.dumps(comparison.to_dict(), indent=2, default=float))
        else:
            _write_json(Path(args.json), comparison.to_dict())
            print(f"JSON written to {args.json}")
    if args.out:
        paths = write_control_result(comparison.to_dict(), Path(args.out), name=args.name)
        print(f"  wrote {paths['path']}")
    return 0 if ctrl.ok else 1


def cmd_uncertainty(args: argparse.Namespace) -> int:
    cfg = UncertaintyConfig()
    d = cfg.to_dict()
    print("default synthetic uncertainty configuration (NOT a real-world forecast):")
    for k, v in d.items():
        print(f"  {k:<20} {v}")
    print("stress families: ordinary, high_demand, low_renewable, compound")
    print("generate scenarios with `stochastic`; the seed makes them reproducible.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gridmind", description="GridMind: LP dispatch optimiser for a SIMULATED network (research prototype).")
    p.add_argument("--version", action="version", version=f"gridmind {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--network", help="network JSON (default: data/sample_network.json)")
        sp.add_argument("--penalty", type=float, help="unmet-demand penalty, USD/MWh (default from network file)")

    sub.add_parser("check-solver", help="verify Pyomo + GLPK are usable").set_defaults(func=cmd_check_solver)
    sub.add_parser("scenarios", help="list built-in scenarios").set_defaults(func=cmd_scenarios)

    sp = sub.add_parser("solve", help="solve one scenario"); common(sp)
    sp.add_argument("--scenario", default="normal"); sp.add_argument("--strict", action="store_true", help="forbid unmet demand")
    sp.add_argument("--json", help="write JSON result to this file ('-' = stdout)"); sp.set_defaults(func=cmd_solve)

    sp = sub.add_parser("compare", help="four core scenarios vs baseline"); common(sp)
    sp.add_argument("--out", default=str(RESULTS_DIR)); sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("sensitivity", help="demand / generator-cost / line-capacity sensitivity"); common(sp)
    sp.add_argument("--kind", choices=["demand", "generator_cost", "line_capacity"], required=True)
    sp.add_argument("--target", help="generator id or line id (default: all)"); sp.add_argument("--scenario", default="normal")
    sp.add_argument("--multipliers", help="comma list, e.g. 0.8,1,1.2"); sp.add_argument("--out", default=str(RESULTS_DIR))
    sp.set_defaults(func=cmd_sensitivity)

    sp = sub.add_parser("benchmark", help="batch run: 4 core + N seeded random scenarios -> CSV"); common(sp)
    sp.add_argument("--n", type=int, default=50); sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--out", default=str(RESULTS_DIR / "benchmark.csv")); sp.set_defaults(func=cmd_benchmark)

    sp = sub.add_parser("demo", help="outage changes dispatch / unmet demand / infeasible case"); common(sp)
    sp.add_argument("--out", default=str(RESULTS_DIR)); sp.set_defaults(func=cmd_demo)

    def horizon_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--horizon", help="horizon JSON (default: data/sample_horizon.json)")
        sp.add_argument("--network", help="override the network JSON referenced by the horizon file")
        sp.add_argument("--penalty", type=float, help="unmet-demand penalty, USD/MWh (default from network file)")
        sp.add_argument("--battery-mode", choices=BATTERY_MODES, default="lp",
                        help="battery model: 'lp' (continuous, default) or 'exclusive' "
                             "(binary charge/discharge exclusivity; needs a MIP solver)")

    sp = sub.add_parser("schedule", help="multi-period optimisation over a horizon")
    horizon_common(sp)
    sp.add_argument("--scenario", default="h-normal", help="horizon scenario id (see `scenarios`)")
    sp.add_argument("--strict", action="store_true", help="forbid unmet demand")
    sp.add_argument("--json", help="write JSON result to this file ('-' = stdout)")
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("resilience", help="resilience suite: stress scenarios with vs without storage")
    horizon_common(sp)
    sp.add_argument("--scenario", help="single horizon scenario id (default: all core scenarios)")
    sp.add_argument("--out", default=str(RESULTS_DIR))
    sp.set_defaults(func=cmd_resilience)

    sp = sub.add_parser("mp-benchmark", help="multi-period benchmark batch -> CSV")
    horizon_common(sp)
    sp.add_argument("--n", type=int, default=20, help="number of seeded random horizon scenarios")
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--out", default=str(RESULTS_DIR / "mp_benchmark.csv"))
    sp.set_defaults(func=cmd_mp_benchmark)

    sub.add_parser("uncertainty", help="show the default synthetic uncertainty configuration"
                   ).set_defaults(func=cmd_uncertainty)

    sp = sub.add_parser("stochastic", help="two-stage risk-aware scheduling, evaluated out-of-sample")
    horizon_common(sp)
    sp.add_argument("--scenarios", type=int, default=40, help="number of synthetic scenarios")
    sp.add_argument("--seed", type=int, default=20260101, help="scenario RNG seed (reproducible)")
    sp.add_argument("--demand-sigma", type=float, default=0.08, help="relative demand noise")
    sp.add_argument("--renewable-sigma", type=float, default=0.18, help="relative renewable noise")
    sp.add_argument("--demand-ar1", type=float, default=0.6, help="lag-1 demand autocorrelation")
    sp.add_argument("--risk-weight", type=float, default=0.5, help="lambda in [0,1]: 0=E[cost], 1=pure CVaR")
    sp.add_argument("--cvar-alpha", type=float, default=0.95, help="CVaR confidence level in (0,1)")
    sp.add_argument("--lead-periods", type=int, default=1, help="first-stage (committed) periods")
    sp.add_argument("--holdout-fraction", type=float, default=0.5, help="fraction of scenarios held out")
    sp.add_argument("--json", help="write the experiment record to this file ('-' = stdout)")
    sp.set_defaults(func=cmd_stochastic)

    sp = sub.add_parser("experiment", help="stage 5A: multi-seed comparative policy benchmark")
    horizon_common(sp)
    sp.add_argument("--seed", type=int, action="append",
                    help="scenario seed (repeatable); default: 5 fixed seeds")
    sp.add_argument("--scenarios", type=int, default=40, help="scenarios per seed")
    sp.add_argument("--demand-sigma", type=float, default=0.08, help="relative demand noise")
    sp.add_argument("--renewable-sigma", type=float, default=0.18, help="relative renewable noise")
    sp.add_argument("--demand-ar1", type=float, default=0.6, help="lag-1 demand autocorrelation")
    sp.add_argument("--risk-weight", type=float, default=0.5, help="lambda in [0,1] for risk_aware")
    sp.add_argument("--cvar-alpha", type=float, default=0.95, help="CVaR confidence level in (0,1)")
    sp.add_argument("--lead-periods", type=int, default=1, help="first-stage (committed) periods")
    sp.add_argument("--holdout-fraction", type=float, default=0.5, help="fraction of scenarios held out")
    sp.add_argument("--out", default=str(RESULTS_DIR / "stage5a_benchmark"),
                    help="output directory for the experiment artefacts")
    sp.set_defaults(func=cmd_experiment)

    sp = sub.add_parser("control", help="stage 5B: rolling-horizon control vs open-loop")
    horizon_common(sp)
    sp.add_argument("--lookahead", type=int, default=6, help="look-ahead horizon (periods)")
    sp.add_argument("--mode", choices=CONTROLLER_MODES, default="deterministic",
                    help="deterministic (LP/MIP re-solve) or stochastic (two-stage) replanning")
    sp.add_argument("--failure-policy", choices=FAILURE_POLICIES, default="hold",
                    help="action when a replan fails: hold (safe recourse + continue) or abort")
    sp.add_argument("--scenarios", type=int, default=20, help="scenarios per replan (stochastic mode)")
    sp.add_argument("--risk-weight", type=float, default=0.5, help="lambda in [0,1] (stochastic mode)")
    sp.add_argument("--cvar-alpha", type=float, default=0.95, help="CVaR level for replanning (stochastic)")
    sp.add_argument("--lead-periods", type=int, default=1, help="first-stage periods (stochastic mode)")
    sp.add_argument("--demand-sigma", type=float, default=0.06, help="forecast demand error sigma")
    sp.add_argument("--renewable-sigma", type=float, default=0.12, help="forecast renewable error sigma")
    sp.add_argument("--seed", type=int, default=20260201, help="forecast-error seed (reproducible)")
    sp.add_argument("--cvar-confidence", type=float, default=0.95, help="realized-CVaR reporting level")
    sp.add_argument("--strict", action="store_true", help="forbid unmet demand")
    sp.add_argument("--json", help="write the comparison record to this file ('-' = stdout)")
    sp.add_argument("--out", help="write the comparison record into this directory")
    sp.add_argument("--name", default="control", help="output file stem when --out is given")
    sp.set_defaults(func=cmd_control)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (KeyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
