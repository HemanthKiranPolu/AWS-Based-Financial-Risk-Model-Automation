from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd


@dataclass
class CreditRiskModel:
    """
    Lightweight credit risk modeling utility for synthetic data generation,
    PD calibration, and stress testing. Intended for demos, integration tests,
    and local experimentation without external data dependencies.
    """

    seed: int = 42
    segments: tuple[str, ...] = ("Prime", "Near-Prime", "Subprime")
    base_pd: Dict[str, float] = field(
        default_factory=lambda: {"Prime": 0.005, "Near-Prime": 0.015, "Subprime": 0.045}
    )
    pd_floor: float = 0.0005
    pd_cap: float = 0.40
    lgd_range: tuple[float, float] = (0.25, 0.65)
    ead_range: tuple[int, int] = (1_000, 25_000)
    coupon_range: tuple[float, float] = (0.025, 0.175)
    term_range_months: tuple[int, int] = (12, 72)

    rng: np.random.Generator = field(init=False)
    pd_scalers: Dict[str, float] = field(init=False)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)
        self.pd_scalers = {segment: 1.0 for segment in self.segments}

    def generate_baseline_data(
        self,
        num_accounts: int,
        as_of_date: Optional[dt.date] = None,
    ) -> pd.DataFrame:
        """
        Create a synthetic point-in-time portfolio with PD/LGD/EAD features.
        Returns a DataFrame with deterministic reproducibility from the seed.
        """
        as_of_date = as_of_date or dt.date.today()
        segment_probs = self._segment_probs()

        segments = self.rng.choice(self.segments, size=num_accounts, p=segment_probs)
        ead = self.rng.integers(self.ead_range[0], self.ead_range[1] + 1, size=num_accounts)
        lgd = self.rng.uniform(self.lgd_range[0], self.lgd_range[1], size=num_accounts)
        coupons = self.rng.uniform(self.coupon_range[0], self.coupon_range[1], size=num_accounts)
        terms = self.rng.integers(self.term_range_months[0], self.term_range_months[1] + 1, size=num_accounts)

        baseline_pd = np.array([self.base_pd[seg] for seg in segments])
        # Add mild idiosyncratic noise to PDs to avoid perfect uniformity.
        noise = self.rng.normal(loc=1.0, scale=0.15, size=num_accounts)
        pd_estimate = np.clip(baseline_pd * noise, self.pd_floor, self.pd_cap)

        df = pd.DataFrame(
            {
                "account_id": [f"ACC-{i:06d}" for i in range(num_accounts)],
                "as_of_date": as_of_date,
                "segment": segments,
                "ead": ead.astype(float),
                "lgd": lgd,
                "coupon": coupons,
                "term_months": terms,
                "pd_estimate": pd_estimate,
            }
        )
        return df

    def generate_historical_data(
        self,
        periods: int = 12,
        accounts_per_period: int = 500,
        start_date: Optional[dt.date] = None,
        period_length_days: int = 30,
    ) -> pd.DataFrame:
        """
        Build a synthetic panel with defaults and losses. Each period generates
        a new micro-portfolio so that calibration can operate segment-by-segment.
        """
        start_date = start_date or (dt.date.today() - dt.timedelta(days=periods * period_length_days))
        records: list[pd.DataFrame] = []

        for i in range(periods):
            as_of = start_date + dt.timedelta(days=i * period_length_days)
            snap = self.generate_baseline_data(accounts_per_period, as_of_date=as_of)
            default_flags = self.rng.binomial(n=1, p=snap["pd_estimate"].values)
            realized_lgd = self.rng.uniform(self.lgd_range[0], self.lgd_range[1], size=accounts_per_period)
            losses = default_flags * snap["ead"].values * realized_lgd

            snap = snap.assign(
                default_flag=default_flags.astype(bool),
                realized_lgd=realized_lgd,
                loss=losses,
                period=i + 1,
            )
            records.append(snap)

        return pd.concat(records, ignore_index=True)

    def calibrate_pd(self, history: pd.DataFrame) -> pd.DataFrame:
        """
        Calibrate PD scaling factors by segment using observed default rates.
        Scalers are stored on the instance and applied by score_portfolio.
        """
        self._require_columns(history, {"segment", "pd_estimate", "default_flag"})

        grouped = history.groupby("segment")
        calibration = []
        for segment, frame in grouped:
            observed_rate = frame["default_flag"].mean()
            expected_rate = frame["pd_estimate"].mean()
            if expected_rate == 0:
                scaler = 1.0
            else:
                scaler = np.clip(observed_rate / expected_rate, 0.25, 4.0)
            self.pd_scalers[segment] = scaler
            calibration.append(
                {
                    "segment": segment,
                    "expected_pd": expected_rate,
                    "observed_default_rate": observed_rate,
                    "pd_scaler": scaler,
                }
            )

        return pd.DataFrame(calibration)

    def score_portfolio(self, portfolio: pd.DataFrame) -> pd.DataFrame:
        """
        Apply calibrated PDs and compute expected loss for each account.
        """
        self._require_columns(portfolio, {"account_id", "segment", "ead", "lgd", "pd_estimate"})

        scaled_pd = []
        for seg, pd_est in zip(portfolio["segment"], portfolio["pd_estimate"]):
            scaler = self.pd_scalers.get(seg, 1.0)
            scaled_pd.append(np.clip(pd_est * scaler, self.pd_floor, self.pd_cap))

        portfolio = portfolio.copy()
        portfolio["pd_calibrated"] = scaled_pd
        portfolio["expected_loss"] = portfolio["ead"] * portfolio["lgd"] * portfolio["pd_calibrated"]
        return portfolio

    def run_stress_scenario(
        self,
        portfolio: pd.DataFrame,
        pd_multiplier: float = 1.35,
        lgd_shift: float = 0.05,
        macro_shock: Optional[float] = None,
    ) -> pd.DataFrame:
        """
        Generate a stressed view of the portfolio by bumping PDs and LGDs.
        Optional macro_shock shifts segment PDs proportionally to their base PD.
        """
        scored = self.score_portfolio(portfolio)
        shock = macro_shock if macro_shock is not None else 0.0

        stressed_pd = np.clip(
            scored["pd_calibrated"] * pd_multiplier + shock * scored["pd_calibrated"],
            self.pd_floor,
            self.pd_cap,
        )
        stressed_lgd = np.clip(scored["lgd"] + lgd_shift, 0.0, 1.0)

        stressed = scored.copy()
        stressed["pd_stressed"] = stressed_pd
        stressed["lgd_stressed"] = stressed_lgd
        stressed["expected_loss_stressed"] = stressed["ead"] * stressed["lgd_stressed"] * stressed["pd_stressed"]
        return stressed

    def _segment_probs(self) -> np.ndarray:
        """
        Allocate slightly fewer Prime accounts to make test distributions interesting.
        Probabilities sum to 1 and are stable under the configured segments.
        """
        if len(self.segments) != 3:
            return np.ones(len(self.segments)) / len(self.segments)
        probs = np.array([0.45, 0.35, 0.20])
        return probs

    @staticmethod
    def _require_columns(frame: pd.DataFrame, cols: Iterable[str]) -> None:
        missing = set(cols) - set(frame.columns)
        if missing:
            raise ValueError(f"DataFrame missing required columns: {sorted(missing)}")
