"""Behavioural configuration loaded from YAML (`config/config.yaml`).

The schema mirrors `config/config.example.yaml`; unknown keys are rejected so typos
never silently fall back to defaults.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Pct = Decimal  # documented alias: values are percentages (1.0 == %1)


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class EgressMode(StrEnum):
    DIRECT_VPS = "direct_vps"
    WIREGUARD = "wireguard"
    PROXY = "proxy"
    MANAGED_PROXY = "managed_proxy"

    @property
    def requires_proxy_url(self) -> bool:
        return self in (EgressMode.PROXY, EgressMode.MANAGED_PROXY)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EgressConfig(_Strict):
    mode: EgressMode = EgressMode.WIREGUARD
    check_interval_minutes: int = Field(default=5, gt=0)
    request_timeout_seconds: int = Field(default=10, gt=0, le=120)
    ip_check_services: tuple[str, ...] = Field(
        default=("https://api.ipify.org", "https://ifconfig.me/ip"), min_length=2
    )
    # Fail-closed is not optional (CLAUDE.md rule 8): `false` is rejected.
    fail_closed: Literal[True] = True

    @model_validator(mode="after")
    def _check_services(self) -> EgressConfig:
        if len(set(self.ip_check_services)) < 2:
            raise ValueError("En az 2 bağımsız IP doğrulama servisi gerekli.")
        if any(not s.startswith("https://") for s in self.ip_check_services):
            raise ValueError("IP doğrulama servisleri https:// olmalı.")
        return self


class ExchangeConfig(_Strict):
    enabled: bool = False
    market_type: Literal["spot", "futures"] = "spot"
    testnet: bool = True


class ScannerConfig(_Strict):
    top_n: int = Field(default=10, gt=0)
    min_24h_volume_usdt: Decimal = Field(default=Decimal(50_000_000), ge=0)
    max_spread_pct: Pct = Field(default=Decimal("0.05"), gt=0)


class UniverseConfig(_Strict):
    mode: Literal["fixed", "scanner"] = "fixed"
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)

    @model_validator(mode="after")
    def _check_symbols(self) -> UniverseConfig:
        if self.mode == "fixed" and not self.symbols:
            raise ValueError("universe.mode=fixed iken en az bir sembol gerekli.")
        for s in self.symbols:
            if "/" not in s:
                raise ValueError(f"Sembol BASE/QUOTE biçiminde olmalı: {s}")
        return self


class TimeframesConfig(_Strict):
    long: tuple[str, ...] = ("1w", "1d")
    mid: tuple[str, ...] = ("4h", "1h")
    short: tuple[str, ...] = ("15m", "5m")


DEFAULT_CONFLUENCE_WEIGHTS = {
    "htf_trend_alignment": Decimal(25),
    "zone_proximity": Decimal(20),
    "structure_confirmation": Decimal(15),
    "volume_confirmation": Decimal(10),
    "pattern": Decimal(10),
    "divergence": Decimal(10),
    "regime_fit": Decimal(10),
}


class ConfluenceConfig(_Strict):
    min_score: Decimal = Field(default=Decimal(70), ge=0, le=100)
    weights: dict[str, Decimal] = Field(default_factory=lambda: dict(DEFAULT_CONFLUENCE_WEIGHTS))
    # Lower-TF setup against the higher-TF trend: score is multiplied by this (spec §5.3.a).
    htf_conflict_penalty: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    recent_bars: int = Field(default=5, ge=1)  # patterns/divergences count if this recent

    @model_validator(mode="after")
    def _check_weights(self) -> ConfluenceConfig:
        unknown = set(self.weights) - set(DEFAULT_CONFLUENCE_WEIGHTS)
        if unknown:
            raise ValueError(f"Bilinmeyen confluence bileşeni: {sorted(unknown)}")
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("Confluence ağırlıkları negatif olamaz.")
        if self.weights and sum(self.weights.values()) <= 0:
            raise ValueError("Confluence ağırlıklarının toplamı pozitif olmalı.")
        return self


class TradePlanConfig(_Strict):
    min_risk_reward: Decimal = Field(default=Decimal(2), gt=0)
    sl_atr_buffer: Decimal = Field(default=Decimal("0.5"), gt=0)
    max_stop_atr: Decimal = Field(default=Decimal(4), gt=0)  # farther stops are rejected
    take_profits: tuple[Decimal, ...] = Field(
        default=(Decimal(1), Decimal(2), Decimal(3)), min_length=1, max_length=3
    )
    tp1_close_pct: Pct = Field(default=Decimal(40), ge=0, le=100)
    move_sl_to_breakeven_after_tp1: bool = True
    trailing: Literal["atr", "structure", "none"] = "atr"
    time_exit_bars: int = Field(default=48, gt=0)

    @model_validator(mode="after")
    def _check_tps(self) -> TradePlanConfig:
        tps = list(self.take_profits)
        if any(tp <= 0 for tp in tps) or tps != sorted(set(tps)):
            raise ValueError("take_profits pozitif ve kesin artan R katları olmalı.")
        return self


class IndicatorParams(_Strict):
    ema_periods: tuple[int, ...] = (9, 21, 50, 200)
    rsi_period: int = Field(default=14, gt=1)
    macd_fast: int = Field(default=12, gt=0)
    macd_slow: int = Field(default=26, gt=0)
    macd_signal: int = Field(default=9, gt=0)
    bb_period: int = Field(default=20, gt=1)
    bb_std: Decimal = Field(default=Decimal(2), gt=0)
    atr_period: int = Field(default=14, gt=0)
    adx_period: int = Field(default=14, gt=0)
    volume_sma: int = Field(default=20, gt=0)


class RegimeParams(_Strict):
    adx_trend: Decimal = Field(default=Decimal(25), gt=0)
    ema_fast: int = Field(default=50, gt=0)
    ema_slow: int = Field(default=200, gt=0)
    slope_bars: int = Field(default=10, gt=0)
    percentile_lookback: int = Field(default=200, gt=10)
    volatility_lookback: int = Field(default=500, gt=10)
    volatile_atr_ratio: Decimal = Field(default=Decimal("2.0"), gt=1)
    squeeze_bb_percentile: Decimal = Field(default=Decimal("0.15"), gt=0, le=1)


class StructureParams(_Strict):
    swing_lookback: int = Field(default=3, ge=1)  # fractal: n bars each side


class ZoneParams(_Strict):
    cluster_atr: Decimal = Field(default=Decimal("0.6"), gt=0)
    min_touches: int = Field(default=2, ge=1)
    max_zones: int = Field(default=8, ge=1)
    impulse_atr: Decimal = Field(default=Decimal("1.5"), gt=0)
    base_max_body_atr: Decimal = Field(default=Decimal("0.5"), gt=0)
    equal_level_atr: Decimal = Field(default=Decimal("0.15"), gt=0)
    proximity_atr: Decimal = Field(default=Decimal("1.0"), gt=0)


class VolumeProfileParams(_Strict):
    bins: int = Field(default=50, ge=5)
    value_area_pct: Decimal = Field(default=Decimal(70), gt=0, lt=100)
    lookback_bars: int = Field(default=300, ge=10)


class PatternParams(_Strict):
    doji_body_pct: Decimal = Field(default=Decimal("0.1"), gt=0, lt=1)
    pin_wick_body_ratio: Decimal = Field(default=Decimal(2), gt=0)
    level_tolerance_atr: Decimal = Field(default=Decimal("0.5"), gt=0)
    min_pattern_bars: int = Field(default=10, ge=3)
    triangle_flat_slope_atr: Decimal = Field(default=Decimal("0.05"), ge=0)
    flag_pole_atr: Decimal = Field(default=Decimal(3), gt=0)
    channel_lookback: int = Field(default=50, ge=10)
    channel_std: Decimal = Field(default=Decimal(2), gt=0)


class DivergenceParams(_Strict):
    max_bars_between: int = Field(default=60, ge=5)
    min_bars_between: int = Field(default=5, ge=1)


class AnalysisConfig(_Strict):
    timeframes: TimeframesConfig = Field(default_factory=TimeframesConfig)
    indicators: IndicatorParams = Field(default_factory=IndicatorParams)
    regime: RegimeParams = Field(default_factory=RegimeParams)
    structure: StructureParams = Field(default_factory=StructureParams)
    zones: ZoneParams = Field(default_factory=ZoneParams)
    volume_profile: VolumeProfileParams = Field(default_factory=VolumeProfileParams)
    patterns: PatternParams = Field(default_factory=PatternParams)
    divergence: DivergenceParams = Field(default_factory=DivergenceParams)
    analysis_bars: int = Field(default=500, ge=100)  # bars per timeframe fed to the engine
    chart_bars: int = Field(default=200, ge=20)
    confluence: ConfluenceConfig = Field(default_factory=ConfluenceConfig)
    trade_plan: TradePlanConfig = Field(default_factory=TradePlanConfig)
    llm_commentary: bool = False


class WalkForwardConfig(_Strict):
    train_bars: int = Field(default=24 * 180, ge=100)  # ~6 months of 1h bars
    test_bars: int = Field(default=24 * 60, ge=50)  # ~2 months
    objective: Literal["expectancy", "profit_factor", "sharpe"] = "expectancy"
    min_trades: int = Field(default=10, ge=1)


class BacktestConfig(_Strict):
    initial_equity: Decimal = Field(default=Decimal(10_000), gt=0)
    commission_pct: Pct = Field(default=Decimal("0.1"), ge=0, le=5)  # per side, taker
    slippage_bps: Decimal = Field(default=Decimal(5), ge=0, le=500)
    latency_bars: int = Field(default=1, ge=1)  # signal at close t -> fill at open t+latency
    allow_short: bool = True
    trailing_atr_mult: Decimal = Field(default=Decimal(2), gt=0)
    structure_trail_bars: int = Field(default=10, ge=2)
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    param_grids: dict[str, dict[str, list[Any]]] = Field(default_factory=dict)


class StrategyConfig(BaseModel):
    """Strategy parameters vary per strategy; typed per strategy in Aşama 4."""

    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool = False


class CrossExchangeConfig(_Strict):
    enabled: bool = False
    pairs: tuple[str, ...] = ()
    min_net_spread_pct: Pct = Field(default=Decimal("0.25"), gt=0)
    max_leg_latency_ms: int = Field(default=500, gt=0)


class TriangularConfig(_Strict):
    enabled: bool = False
    exchange: str = "binance"
    min_net_profit_pct: Pct = Field(default=Decimal("0.15"), gt=0)


class FundingRateConfig(_Strict):
    enabled: bool = False


class ArbitrageConfig(_Strict):
    cross_exchange: CrossExchangeConfig = Field(default_factory=CrossExchangeConfig)
    triangular: TriangularConfig = Field(default_factory=TriangularConfig)
    funding_rate: FundingRateConfig = Field(default_factory=FundingRateConfig)


class QualityMultiplierConfig(_Strict):
    # Spec §5.6: high-quality setups may raise risk to at most 1.5x.
    high_score: Decimal = Field(default=Decimal("1.5"), gt=0, le=Decimal("1.5"))
    low_score: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    high_threshold: Decimal = Field(default=Decimal(85), ge=0, le=100)  # score >= -> high
    low_threshold: Decimal = Field(default=Decimal(75), ge=0, le=100)  # score < -> low


class KellyConfig(_Strict):
    enabled: bool = False
    # Spec §5.6: at most quarter Kelly, only after >= 100 trades.
    fraction: Decimal = Field(default=Decimal("0.25"), gt=0, le=Decimal("0.25"))
    min_trades: int = Field(default=100, ge=100)


class DrawdownScalingConfig(_Strict):
    start_pct: Pct = Field(default=Decimal(5), gt=0, le=100)
    risk_factor: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)


class LossStreakConfig(_Strict):
    count: int = Field(default=3, gt=0)
    risk_factor: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)


class ProfitLockConfig(_Strict):
    enabled: bool = True
    every_gain_pct: Pct = Field(default=Decimal(20), gt=0)
    lock_pct: Pct = Field(default=Decimal(25), ge=0, le=100)


class AutoAllocationConfig(_Strict):
    enabled: bool = True
    lookback_trades: int = Field(default=30, gt=0)
    pause_if_expectancy_below: Decimal = Decimal(0)
    min_trades: int = Field(default=10, ge=1)  # judge a strategy/coin only after this many
    max_multiplier: Decimal = Field(default=Decimal("1.25"), ge=1, le=Decimal("1.5"))
    # paused combinations get a fresh trial after this cool-down (no trades = no new evidence)
    pause_hours: int = Field(default=168, ge=1)


class GrowthConfig(_Strict):
    compounding: bool = True
    quality_multiplier: QualityMultiplierConfig = Field(default_factory=QualityMultiplierConfig)
    kelly: KellyConfig = Field(default_factory=KellyConfig)
    drawdown_scaling: DrawdownScalingConfig = Field(default_factory=DrawdownScalingConfig)
    loss_streak: LossStreakConfig = Field(default_factory=LossStreakConfig)
    profit_lock: ProfitLockConfig = Field(default_factory=ProfitLockConfig)
    auto_allocation: AutoAllocationConfig = Field(default_factory=AutoAllocationConfig)


class EventWindow(_Strict):
    """Optional macro/event calendar entry (FOMC, CPI, token unlock...)."""

    label: str
    start: datetime
    end: datetime
    risk_factor: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    symbols: tuple[str, ...] = ()  # empty = all


class RiskConfig(_Strict):
    risk_per_trade_pct: Pct = Field(default=Decimal(1), gt=0, le=100)
    max_open_positions: int = Field(default=5, gt=0)
    max_exposure_per_symbol_pct: Pct = Field(default=Decimal(20), gt=0, le=100)
    max_total_exposure_pct: Pct = Field(default=Decimal(60), gt=0, le=100)
    daily_loss_limit_pct: Pct = Field(default=Decimal(3), gt=0, le=100)
    max_drawdown_pct: Pct = Field(default=Decimal(10), gt=0, le=100)
    max_consecutive_errors: int = Field(default=5, gt=0)
    # Spec §5.6: default 1x, hard ceiling 3x.
    max_leverage: int = Field(default=1, ge=1, le=3)
    max_spread_pct: Pct = Field(default=Decimal("0.1"), gt=0)
    on_kill_switch: Literal["close_all", "keep_positions"] = "close_all"
    max_correlation: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)
    var_confidence: Decimal = Field(default=Decimal("0.95"), gt=0, lt=1)
    correlation_lookback_bars: int = Field(default=720, ge=30)
    correlation_reduce_factor: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    max_correlated_positions: int = Field(default=2, ge=1)
    min_depth_ratio: Decimal = Field(default=Decimal(3), gt=0)  # depth within 0.5% vs notional
    stablecoins: tuple[str, ...] = ("USDT", "USDC", "FDUSD", "DAI", "TUSD")
    event_windows: tuple[EventWindow, ...] = ()
    growth: GrowthConfig = Field(default_factory=GrowthConfig)

    @model_validator(mode="after")
    def _check_exposure(self) -> RiskConfig:
        if self.max_exposure_per_symbol_pct > self.max_total_exposure_pct:
            raise ValueError("Parite başı maruziyet toplam maruziyetten büyük olamaz.")
        return self


class LowLiquidityConfig(_Strict):
    enabled: bool = True
    weekend_risk_factor: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)


class OperationsConfig(_Strict):
    live_capital_cap_pct: Pct = Field(default=Decimal(10), gt=0, le=100)
    shadow_mode: bool = True
    stablecoin_depeg_threshold_pct: Pct = Field(default=Decimal("0.5"), gt=0)
    max_balance_per_exchange_usdt: Decimal = Field(default=Decimal(5000), gt=0)
    low_liquidity_mode: LowLiquidityConfig = Field(default_factory=LowLiquidityConfig)
    watch_exchange_announcements: bool = True


class TelegramConfig(_Strict):
    enabled: bool = False
    daily_summary_hour: int = Field(default=23, ge=0, le=23)


class MarketDataConfig(_Strict):
    enabled: bool = True
    candle_timeframes: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d")
    order_book_depth: int = Field(default=20, ge=1, le=1000)
    warmup_bars: int = Field(default=300, ge=0)


class PaperConfig(_Strict):
    """Starting balances of the simulated (paper) account."""

    initial_balances: dict[str, Decimal] = Field(default_factory=lambda: {"USDT": Decimal(10_000)})

    @model_validator(mode="after")
    def _check_balances(self) -> PaperConfig:
        if any(v < 0 for v in self.initial_balances.values()):
            raise ValueError("Paper bakiyeleri negatif olamaz.")
        return self


class HeartbeatConfig(_Strict):
    """Periodic ping to an external monitor (URL in `.env` HEARTBEAT_URL)."""

    enabled: bool = False
    interval_seconds: int = Field(default=60, ge=10)


class NotifyConfig(_Strict):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class ApiConfig(_Strict):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class AppConfig(_Strict):
    mode: Mode = Mode.PAPER
    live_trading_confirmed: bool = False
    base_currency: str = "USDT"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    egress: EgressConfig = Field(default_factory=EgressConfig)
    exchanges: dict[str, ExchangeConfig] = Field(
        default_factory=lambda: {"binance": ExchangeConfig(enabled=True)}
    )
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)
    arbitrage: ArbitrageConfig = Field(default_factory=ArbitrageConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    operations: OperationsConfig = Field(default_factory=OperationsConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    marketdata: MarketDataConfig = Field(default_factory=MarketDataConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

    @property
    def enabled_exchanges(self) -> dict[str, ExchangeConfig]:
        return {name: ex for name, ex in self.exchanges.items() if ex.enabled}
