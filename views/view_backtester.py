"""3.8 Historical Backtester."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from infinity.backtest import BacktestConfig, ExitRule, run_backtest
from infinity.scanners import REGISTRY
from infinity.ui.charts import equity_chart
from views.common import cached_bars, cached_universe, shell, universe_picker


def render() -> None:
    shell("3.8  Historical Backtester")
    st.subheader("3.8 Historical Backtester")
    st.caption(
        "Replays a scanner over a historical window. Results are reported gross "
        "and net of costs, against an equal-weight buy-and-hold benchmark."
    )

    universe = universe_picker()
    st.divider()

    c1, c2, c3 = st.columns(3)
    with c1:
        scanner = st.selectbox(
            "Strategy",
            sorted(REGISTRY),
            format_func=lambda n: f"{REGISTRY[n].section} {REGISTRY[n].title}",
            key="bt_scanner",
        )
        min_score = st.slider("Min score to enter", 50, 100, 80, 5, key="bt_score")
    with c2:
        exit_rule = st.selectbox(
            "Exit rule",
            list(ExitRule),
            format_func=lambda r: {
                ExitRule.FIXED_BARS: "Fixed holding period",
                ExitRule.STOP_TARGET: "Stop loss / target",
                ExitRule.SIGNAL_INVALIDATION: "Signal invalidation",
            }[r],
            key="bt_exit",
        )
        if exit_rule is ExitRule.FIXED_BARS:
            hold = st.number_input("Hold (bars)", 1, 120, 10, key="bt_hold")
            stop = target = 0.0
        elif exit_rule is ExitRule.STOP_TARGET:
            stop = st.number_input("Stop %", -50.0, -0.5, -5.0, 0.5, key="bt_stop")
            target = st.number_input("Target %", 0.5, 100.0, 10.0, 0.5, key="bt_target")
            hold = 10
        else:
            hold, stop, target = 10, -5.0, 10.0
    with c3:
        cost = st.number_input(
            "Round-trip cost %", 0.0, 2.0, 0.15, 0.01, key="bt_cost",
            help="Brokerage + STT + slippage.",
        )
        split_policy = st.selectbox(
            "Corporate actions",
            ("exclude", "adjust", "ignore"),
            format_func={
                "exclude": "Exclude affected symbols (safest)",
                "adjust": "Back-adjust prices",
                "ignore": "Ignore (spec-literal, unsafe)",
            }.get,
            key="bt_splits",
        )
        limit = st.number_input(
            "Symbol cap", 0, 500, 50, 10, key="bt_limit",
            help="0 = whole universe. A full run is slow.",
        )

    d1, d2 = st.columns(2)
    start = d1.date_input("Start", pd.Timestamp("2024-01-01").date(), key="bt_start")
    end = d2.date_input("End", pd.Timestamp.today().date(), key="bt_end")

    if not st.button("Run backtest", type="primary", key="bt_run"):
        st.info("Configure the run above, then press **Run backtest**.", icon="⚙️")
        return

    ul = cached_universe(universe.value)
    bars = cached_bars(universe.value, int(limit))
    industries = {m.symbol: m.industry for m in ul.members}

    cfg = BacktestConfig(
        scanner=scanner,
        exit_rule=exit_rule,
        hold_bars=int(hold),
        stop_pct=float(stop),
        target_pct=float(target),
        min_score=float(min_score),
        cost_pct=float(cost),
        start=str(start),
        end=str(end),
        split_policy=split_policy,
    )

    with st.spinner(f"Replaying {len(bars)} symbols..."):
        result = run_backtest(cfg, bars, industries)

    st.divider()
    for warning in result.warnings:
        st.warning(warning, icon="⚠️")

    metrics = result.metrics()
    if metrics.get("Total Trades", 0) == 0:
        st.info("No trades were generated. Try lowering the minimum score.", icon="🔍")
        return

    keys = list(metrics)
    for chunk in (keys[:5], keys[5:]):
        cols = st.columns(len(chunk))
        for col, key in zip(cols, chunk, strict=False):
            col.metric(key, metrics[key])

    bench = result.benchmark_return()
    delta = result.portfolio_return() - bench
    st.caption(
        f"Strategy {result.portfolio_return():+.2f}% vs buy-and-hold {bench:+.2f}% "
        f"— {'outperformed' if delta > 0 else 'underperformed'} by {abs(delta):.2f} pts."
    )

    st.plotly_chart(
        equity_chart(result.equity_curve, result.benchmark_curve), width="stretch"
    )

    if result.excluded:
        with st.expander(f"Excluded symbols ({len(result.excluded)})"):
            st.dataframe(result.excluded, hide_index=True, width="stretch")

    st.markdown("**Trade log**")
    trades = [t.to_dict(cfg.cost_pct) for t in result.trades]
    st.dataframe(trades, hide_index=True, width="stretch", height=380)
    st.download_button(
        "Download trade log (CSV)",
        pd.DataFrame(trades).to_csv(index=False).encode(),
        file_name=f"backtest_{scanner}_{start}_{end}.csv",
        mime="text/csv",
        key="bt_csv",
    )
