"""Strategy registry and construction from config."""

from __future__ import annotations

from bot.config.schema import AppConfig
from bot.strategies.base import BaseStrategy
from bot.strategies.breakout import Breakout
from bot.strategies.combiner import SignalCombiner
from bot.strategies.dca import Dca
from bot.strategies.ema_crossover import EmaCrossover
from bot.strategies.grid import Grid
from bot.strategies.rsi_reversion import RsiReversion

STRATEGIES: dict[str, type[BaseStrategy]] = {
    cls.name: cls for cls in (EmaCrossover, RsiReversion, Breakout, Grid, Dca)
}


def strategy_params(config: AppConfig, name: str) -> dict[str, object]:
    sc = config.strategies.get(name)
    if sc is None:
        return {}
    data = dict(sc.model_extra or {})
    data.pop("enabled", None)
    return data


def build_strategy(
    config: AppConfig,
    name: str,
    symbol: str,
    exchange: str = "binance",
    overrides: dict[str, object] | None = None,
) -> BaseStrategy:
    if name not in STRATEGIES:
        raise KeyError(f"Bilinmeyen strateji: {name} (mevcut: {', '.join(STRATEGIES)})")
    params = {**strategy_params(config, name), **(overrides or {})}
    return STRATEGIES[name](symbol, exchange, params, config.analysis)


def build_enabled(config: AppConfig, symbol: str, exchange: str = "binance") -> list[BaseStrategy]:
    out = [
        build_strategy(config, name, symbol, exchange)
        for name, sc in config.strategies.items()
        if sc.enabled and name in STRATEGIES
    ]
    comb = config.strategies.get("signal_combiner")
    if comb is not None and comb.enabled and out:
        return [
            SignalCombiner(
                out, symbol, exchange, strategy_params(config, "signal_combiner"), config.analysis
            )
        ]
    return out
