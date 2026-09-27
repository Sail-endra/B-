"""Series transforms and frequency alignment.

The transform is not a detail. Correlating two highly persistent *level* series
is a spurious-regression setup: both are close to unit-root processes, so the
correlation reflects shared trend rather than the relationship being examined.
CPI is an index, not an inflation rate, and must be differenced before it can be
called one.

So every chart and statistic states the transform it used, and the transform is
chosen from the series' own units rather than guessed at call time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

TRANSFORMS = {
    "level": "as published",
    "yoy_pct": "year-over-year percent change",
    "pct_change": "period-over-period percent change",
    "log_diff": "first difference of natural log",
    "diff": "first difference",
}

_PERIODS_PER_YEAR = {
    "Monthly": 12, "Quarterly": 4, "Annual": 1, "Daily": 252, "Weekly": 52,
    "Semiannual": 2,
}


def default_transform_for(units: str, title: str = "") -> str:
    """Pick a transform from the series' declared units.

    An index or a dollar level is meaningless untransformed in a correlation; a
    series already published as a percent or a rate is not.
    """
    text = f"{units} {title}".lower()
    if any(k in text for k in ("percent", "rate", "ratio", "per cent")):
        return "level"
    if any(k in text for k in ("index", "dollars", "billions", "millions", "thousands",
                               "number", "persons", "units")):
        return "yoy_pct"
    return "level"


@dataclass
class Series:
    series_id: str
    dates: list[str]
    values: list[float]
    frequency: str = "Monthly"
    units: str = ""
    transform: str = "level"
    synthetic: bool = False
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.values):
            raise ValueError(f"{self.series_id}: dates and values differ in length")

    @property
    def array(self) -> np.ndarray:
        return np.asarray(self.values, dtype=float)

    def window(self, start: Optional[str] = None, end: Optional[str] = None) -> "Series":
        keep = [
            i for i, d in enumerate(self.dates)
            if (start is None or d >= start) and (end is None or d <= end)
        ]
        return Series(
            series_id=self.series_id,
            dates=[self.dates[i] for i in keep],
            values=[self.values[i] for i in keep],
            frequency=self.frequency, units=self.units, transform=self.transform,
            synthetic=self.synthetic, fetched_at=self.fetched_at,
        )

    def summary(self) -> dict[str, object]:
        """What the agent sees. Never raw rows -- a 900-observation series would
        consume the whole tool-output budget and add nothing a summary lacks."""
        arr = self.array
        finite = arr[np.isfinite(arr)]
        return {
            "series_id": self.series_id,
            "transform": self.transform,
            "transform_meaning": TRANSFORMS.get(self.transform, self.transform),
            "units": self.units,
            "frequency": self.frequency,
            "n": int(finite.size),
            "start": self.dates[0] if self.dates else None,
            "end": self.dates[-1] if self.dates else None,
            "first": round(float(finite[0]), 4) if finite.size else None,
            "last": round(float(finite[-1]), 4) if finite.size else None,
            "mean": round(float(finite.mean()), 4) if finite.size else None,
            "min": round(float(finite.min()), 4) if finite.size else None,
            "max": round(float(finite.max()), 4) if finite.size else None,
            "synthetic": self.synthetic,
        }

    def points(self, max_points: int = 400) -> list[dict[str, object]]:
        """Downsampled series for plotting. Evenly strided so the shape survives."""
        step = max(1, len(self.dates) // max_points)
        return [
            {"date": d, "value": round(v, 5)}
            for d, v in zip(self.dates[::step], self.values[::step])
            if np.isfinite(v)
        ]


def apply_transform(series: Series, transform: str) -> Series:
    if transform == series.transform:
        return series
    if transform not in TRANSFORMS:
        raise ValueError(f"unknown transform {transform!r}; known: {sorted(TRANSFORMS)}")

    values = series.array
    periods = _PERIODS_PER_YEAR.get(series.frequency, 12)

    if transform == "level":
        out, dates = values, series.dates
    elif transform == "yoy_pct":
        if values.size <= periods:
            raise ValueError(f"{series.series_id}: too few points for a year-over-year change")
        with np.errstate(divide="ignore", invalid="ignore"):
            out = (values[periods:] / values[:-periods] - 1.0) * 100.0
        dates = series.dates[periods:]
    elif transform == "pct_change":
        with np.errstate(divide="ignore", invalid="ignore"):
            out = (values[1:] / values[:-1] - 1.0) * 100.0
        dates = series.dates[1:]
    elif transform == "log_diff":
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.diff(np.log(values))
        dates = series.dates[1:]
    else:  # diff
        out = np.diff(values)
        dates = series.dates[1:]

    return Series(
        series_id=series.series_id, dates=list(dates),
        values=[float(v) for v in out], frequency=series.frequency,
        units="percent" if transform in {"yoy_pct", "pct_change"} else series.units,
        transform=transform, synthetic=series.synthetic, fetched_at=series.fetched_at,
    )


def align(series: Sequence[Series]) -> tuple[list[str], list[np.ndarray]]:
    """Inner-join several series on their common dates.

    Mixed frequencies keep only the dates they share rather than forward-filling:
    filling a quarterly series into monthly slots invents observations and
    inflates any correlation computed on it.
    """
    if not series:
        return [], []
    common: Optional[set[str]] = None
    for s in series:
        dates = set(s.dates)
        common = dates if common is None else (common & dates)
    ordered = sorted(common or set())
    if not ordered:
        return [], []
    out = []
    for s in series:
        lookup = dict(zip(s.dates, s.values))
        out.append(np.asarray([lookup[d] for d in ordered], dtype=float))
    return ordered, out


def correlation(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    if a.size < 3 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan"), int(a.size)
    return float(np.corrcoef(a, b)[0, 1]), int(a.size)
