"""FRED access by search, never by hardcoded identifier.

The old design kept a hand-curated `fred_series.csv` so the model could not
invent a series ID. That catalogue cannot survive generalisation -- it was written
for one macro course -- but the protection it provided must, so the flow is
inverted rather than dropped:

    concept from the retrieved passage
       -> model proposes a SEARCH QUERY, never an ID
       -> FRED /series/search returns real IDs, ranked by popularity
       -> the ID is validated to return data for the requested range
       -> fetch, align, transform

The guarantee is enforced structurally, not by instruction: `fetch_observations`
refuses any series ID that did not come out of a search in this session
(`SeriesRegistry`). A model that emits `GDPFAKE123` gets a refusal, not a plot.

When search finds nothing usable, that is the answer. There is no fallback to a
guessed identifier.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import settings
from ..store import SQLiteStore, get_store
from .transforms import Series, apply_transform

SEARCH_URL = "https://api.stlouisfed.org/fred/series/search"
OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT = 12.0
_SEARCH_TTL = 7 * 24 * 3600      # series metadata changes slowly
_OBS_TTL = 6 * 3600              # monthly series do not change intraday


class FredError(RuntimeError):
    pass


class UnverifiedSeriesError(FredError):
    """Raised when a series ID was not produced by a search in this session."""


@dataclass
class SeriesHit:
    series_id: str
    title: str
    frequency: str
    units: str
    seasonal_adjustment: str = ""
    popularity: int = 0
    observation_start: str = ""
    observation_end: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "series_id": self.series_id,
            "title": self.title,
            "frequency": self.frequency,
            "units": self.units,
            "seasonal_adjustment": self.seasonal_adjustment,
            "popularity": self.popularity,
            "range": f"{self.observation_start} to {self.observation_end}",
        }


@dataclass
class SeriesRegistry:
    """Series IDs this session has actually seen come back from a search.

    This is the replacement for the hand-curated catalogue: instead of listing
    the legal IDs up front, we record the ones FRED itself returned, and refuse
    everything else. It is per-conversation so one question cannot authorise an
    ID for the next.
    """

    verified: dict[str, SeriesHit] = field(default_factory=dict)

    def record(self, hits: list[SeriesHit]) -> None:
        for hit in hits:
            self.verified[hit.series_id.upper()] = hit

    def check(self, series_id: str) -> SeriesHit:
        hit = self.verified.get((series_id or "").upper())
        if hit is None:
            raise UnverifiedSeriesError(
                f"{series_id!r} did not come from a search. Call find_data_series "
                "with a concept query first and use an id it returns. Series ids "
                "must never be written from memory."
            )
        return hit


class FredClient:
    def __init__(self, store: Optional[SQLiteStore] = None):
        self.store = store or get_store()

    # -- search ------------------------------------------------------------

    def search(self, query: str, limit: int = 6, allow_synthetic: bool = False) -> list[SeriesHit]:
        query = (query or "").strip()
        if not query:
            return []

        cached = self.store.cached_search(query, _SEARCH_TTL)
        if cached is not None:
            hits = [SeriesHit(**row) for row in cached["hits"]]
            # A cache written by a benchmark run may hold synthetic ids; never
            # serve those to a caller that has not opted in.
            return hits if allow_synthetic else [h for h in hits if not h.series_id.startswith("SYNTH")]

        if not settings.has_fred:
            # No key. Synthetic series are a benchmark fixture only; the product
            # gets nothing, so it renders no chart rather than a fabricated one.
            hits = _synthetic_search(query, limit) if allow_synthetic else []
        else:
            params = urllib.parse.urlencode(
                {
                    "search_text": query,
                    "api_key": settings.fred_api_key,
                    "file_type": "json",
                    "limit": limit,
                    # Order by relevance, not popularity: the flow searches a
                    # specific concept ("disposable personal income"), and
                    # popularity surfaces broadly-popular series over the relevant
                    # one -- it returned PAYEMS for income and even CPI for
                    # "unemployment rate". search_rank returns the series the text
                    # actually names.
                    "order_by": "search_rank",
                    "sort_order": "desc",
                }
            )
            try:
                with urllib.request.urlopen(f"{SEARCH_URL}?{params}", timeout=_TIMEOUT) as r:
                    payload = json.loads(r.read().decode())
            except Exception as exc:  # noqa: BLE001
                raise FredError(f"FRED search failed for {query!r}: {exc}") from exc
            hits = [
                SeriesHit(
                    series_id=s["id"],
                    title=s.get("title", ""),
                    frequency=s.get("frequency", ""),
                    units=s.get("units", ""),
                    seasonal_adjustment=s.get("seasonal_adjustment_short", ""),
                    popularity=int(s.get("popularity", 0)),
                    observation_start=s.get("observation_start", ""),
                    observation_end=s.get("observation_end", ""),
                )
                for s in payload.get("seriess", [])[:limit]
            ]

        # Only persist real hits to the shared cache, so a benchmark run can never
        # poison the product path with synthetic ids.
        if all(not h.series_id.startswith("SYNTH") for h in hits):
            self.store.cache_search(query, {"hits": [h.__dict__ for h in hits]})
        return hits

    # -- observations ------------------------------------------------------

    def fetch_observations(
        self,
        series_id: str,
        registry: SeriesRegistry,
        start: str = "1960-01-01",
        end: Optional[str] = None,
        transform: Optional[str] = None,
        allow_synthetic: bool = False,
    ) -> Series:
        """Fetch one series. Refuses any id not produced by a search."""
        hit = registry.check(series_id)          # structural guarantee
        series_id = hit.series_id
        if hit.series_id.startswith("SYNTH") and not allow_synthetic:
            raise FredError(
                f"{series_id} is a synthetic benchmark id and must never be fetched "
                "in the product path"
            )

        cached = self.store.cached_series(series_id, "raw", _OBS_TTL)
        if cached is not None:
            base = Series(
                series_id=series_id, dates=cached["dates"], values=cached["values"],
                frequency=hit.frequency or "Monthly", units=hit.units,
                synthetic=cached.get("synthetic", False),
                fetched_at=cached.get("fetched_at", ""),
            )
        else:
            base = self._fetch_raw(hit)
            self.store.cache_series(
                series_id, "raw",
                {"dates": base.dates, "values": base.values, "synthetic": base.synthetic},
            )

        if not base.dates:
            raise FredError(f"{series_id} returned no observations")

        # Validate the requested window actually contains data, rather than
        # silently returning an empty plot.
        window = base.window(start, end)
        if len(window.dates) < 3:
            raise FredError(
                f"{series_id} has no usable data between {start} and {end or 'now'} "
                f"(series covers {base.dates[0]} to {base.dates[-1]})"
            )

        if transform:
            window = apply_transform(window, transform)
        return window

    def _fetch_raw(self, hit: SeriesHit) -> Series:
        if not settings.has_fred:
            return _synthetic_series(hit)

        params = urllib.parse.urlencode(
            {
                "series_id": hit.series_id,
                "api_key": settings.fred_api_key,
                "file_type": "json",
                "observation_start": "1947-01-01",
            }
        )
        try:
            with urllib.request.urlopen(f"{OBS_URL}?{params}", timeout=_TIMEOUT) as r:
                payload = json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise FredError(f"FRED fetch failed for {hit.series_id}: {exc}") from exc

        dates, values = [], []
        for obs in payload.get("observations", []):
            if obs.get("value") in {".", "", None}:
                continue
            dates.append(obs["date"])
            values.append(float(obs["value"]))
        return Series(
            series_id=hit.series_id, dates=dates, values=values,
            frequency=hit.frequency or "Monthly", units=hit.units,
            synthetic=False, fetched_at=datetime.now(timezone.utc).isoformat(),
        )


# ---------------------------------------------------------------------------
# Offline stand-ins -- always stamped
# ---------------------------------------------------------------------------


def _synthetic_search(query: str, limit: int) -> list[SeriesHit]:
    """Plausible-looking hits so the search->validate->fetch path is exercised
    with no key. Every id is prefixed SYNTH so it can never be mistaken for a
    real FRED identifier in a screenshot or a log."""
    import hashlib

    digest = hashlib.sha1(query.encode()).hexdigest()[:6].upper()
    return [
        SeriesHit(
            series_id=f"SYNTH{digest}{i}",
            title=f"[SYNTHETIC] {query} (variant {i + 1})",
            frequency="Monthly", units="Index",
            popularity=90 - i * 10,
            observation_start="1960-01-01", observation_end="2026-01-01",
        )
        for i in range(min(limit, 3))
    ]


def _synthetic_series(hit: SeriesHit) -> Series:
    import numpy as np

    rng = np.random.default_rng(abs(hash(hit.series_id)) % (2**32))
    n = (2026 - 1960) * 12
    level, scale = 100.0, 20.0
    values, current = [], level
    for i in range(n):
        current = 0.97 * current + 0.03 * level + rng.normal(0, scale * 0.04)
        if 2020 <= 1960 + i / 12 < 2022:
            current += scale * 0.5
        values.append(float(current))
    dates = [f"{1960 + i // 12:04d}-{1 + i % 12:02d}-01" for i in range(n)]
    return Series(
        series_id=hit.series_id, dates=dates, values=values,
        frequency="Monthly", units=hit.units, synthetic=True,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


_client: Optional[FredClient] = None


def get_fred() -> FredClient:
    global _client
    if _client is None:
        _client = FredClient()
    return _client
