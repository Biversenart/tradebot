"""Backtest bias checks (spec §5.13). Every report states whether these passed.

- lookahead: the strategy re-run on truncated history must give identical past values.
- realistic costs: commission >= `min_commission_pct`, slippage >= `min_slippage_bps`,
  latency >= 1 bar (signal at close, fill at the next open).
- survivorship: data for delisted coins (`backtest.delisted_symbols`) present and included;
  otherwise a warning (a fixed universe of survivors is optimistic).
- data gaps: missing bars above 1 % of the expected count is a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import pandas as pd

from bot.config.schema import BacktestConfig
from bot.core.timeframes import timeframe_delta


class CheckStatus(StrEnum):
    PASS = "GEÇTİ"  # noqa: S105 - status label, not a secret
    WARN = "UYARI"
    FAIL = "BAŞARISIZ"


@dataclass(frozen=True)
class BiasCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class BiasChecks:
    checks: list[BiasCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.status is not CheckStatus.FAIL for c in self.checks)

    def get(self, name: str) -> BiasCheck:
        return next(c for c in self.checks if c.name == name)

    def lines(self) -> list[str]:
        head = (
            "Önyargı kontrolleri: TÜMÜ GEÇTİ"
            if all(c.status is CheckStatus.PASS for c in self.checks)
            else (
                "Önyargı kontrolleri: uyarılarla geçti"
                if self.passed
                else "Önyargı kontrolleri: BAŞARISIZ — sonuçlara güvenmeyin"
            )
        )
        return [head, *(f"{c.name}: {c.status.value} — {c.detail}" for c in self.checks)]


def gap_ratio(df: pd.DataFrame, timeframe: str) -> float:
    if len(df) < 2:
        return 0.0
    span = df.index[-1] - df.index[0]
    expected = int(span / timeframe_delta(timeframe)) + 1
    return max(0.0, 1 - len(df) / expected) if expected > 0 else 0.0


def bias_checks(
    cfg: BacktestConfig,
    lookahead_passed: bool,
    symbols: list[str],
    frames: dict[str, pd.DataFrame] | None = None,
    timeframe: str = "1h",
) -> BiasChecks:
    checks = [
        BiasCheck(
            "Lookahead (geleceği görme) testi",
            CheckStatus.PASS if lookahead_passed else CheckStatus.FAIL,
            "kısaltılmış geçmişle yeniden hesaplama aynı sonucu verdi"
            if lookahead_passed
            else "strateji gelecekteki veriyi kullanıyor",
        )
    ]
    cost_ok = (
        cfg.commission_pct >= cfg.min_commission_pct and cfg.slippage_bps >= cfg.min_slippage_bps
    )
    checks.append(
        BiasCheck(
            "Gerçekçi maliyet",
            CheckStatus.PASS if cost_ok and cfg.latency_bars >= 1 else CheckStatus.FAIL,
            f"komisyon %{cfg.commission_pct:g}/taraf (min %{cfg.min_commission_pct:g}), kayma "
            f"{cfg.slippage_bps:g} bps (min {cfg.min_slippage_bps:g}), gecikme {cfg.latency_bars} "
            "mum (sinyal kapanışta, dolum sonraki açılışta)",
        )
    )
    delisted = [s for s in cfg.delisted_symbols if s in symbols]
    if delisted:
        surv = BiasCheck(
            "Survivorship",
            CheckStatus.PASS,
            f"delist olmuş coinler dahil: {', '.join(delisted)}",
        )
    else:
        surv = BiasCheck(
            "Survivorship",
            CheckStatus.WARN,
            "yalnızca bugün işlem gören semboller test edildi; delist olmuş coinler dahil değil "
            "(`backtest.delisted_symbols` + verisi). Sonuçlar iyimser olabilir.",
        )
    checks.append(surv)
    if frames:
        worst = max(((gap_ratio(df, timeframe), s) for s, df in frames.items()), default=(0.0, ""))
        checks.append(
            BiasCheck(
                "Veri bütünlüğü",
                CheckStatus.WARN if worst[0] > 0.01 else CheckStatus.PASS,
                f"en yüksek eksik mum oranı %{worst[0] * 100:.2f} ({worst[1]})",
            )
        )
    return BiasChecks(checks)
