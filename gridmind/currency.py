"""Traceable INR localization for GridMind reports.

Units stay in the solver model (USD) unless scaling is proven equivalent. This
module keeps USD as the canonical stored value, derives INR from it with a
single explicit, configurable conversion rate, records provenance (source/date/
direction), and provides Indian-formatted currency strings for display.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


#: Canonical source currency (never relabelled)
SOURCE_CURRENCY = "USD"

#: Display currency for India-localized reports
INR_CURRENCY = "INR"

#: Default rate used when none is supplied. This is a *configurable* default,
#: not a hidden constant; provenance must be recorded whenever it is used.
DEFAULT_USD_TO_INR = 83.50


@dataclass(frozen=True)
class ConversionConfig:
    """Explicit, traceable USD->INR conversion configuration."""
    usd_to_inr: float = DEFAULT_USD_TO_INR
    source: str = "manual/default (configurable)"
    date: str = date.today().isoformat()
    direction: str = "USD_to_INR"
    note: str = ""

    def __post_init__(self) -> None:
        if self.usd_to_inr <= 0:
            raise ValueError("usd_to_inr must be > 0")
        if self.direction != "USD_to_INR":
            raise ValueError("direction must be 'USD_to_INR'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_currency": SOURCE_CURRENCY,
            "target_currency": INR_CURRENCY,
            "usd_to_inr": float(self.usd_to_inr),
            "source": self.source,
            "date": self.date,
            "direction": self.direction,
            "note": self.note,
        }


def usd_to_inr(value_usd: float | None, config: ConversionConfig | None = None
               ) -> float | None:
    if value_usd is None:
        return None
    cfg = config or ConversionConfig()
    return float(value_usd) * float(cfg.usd_to_inr)


def inr_to_usd(value_inr: float | None, config: ConversionConfig | None = None
               ) -> float | None:
    if value_inr is None:
        return None
    cfg = config or ConversionConfig()
    return float(value_inr) / float(cfg.usd_to_inr)


def format_inr(value_inr: float | None) -> str:
    """Format INR with Indian grouping (lakhs/crores style)."""
    if value_inr is None:
        return "n/a"
    v = float(value_inr)
    sign = "-" if v < 0 else ""
    v = abs(v)
    s = f"{v:.2f}"
    int_part, frac = s.split(".")
    if len(int_part) <= 3:
        return f"{sign}₹{int_part}.{frac}"
    last3 = int_part[-3:]
    rest = int_part[:-3]
    out = []
    while len(rest) > 2:
        out.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        out.append(rest)
    out_rev = list(reversed(out))
    return f"{sign}₹{','.join(out_rev)},{last3}.{frac}"


def add_currency_metadata(payload: Mapping[str, Any],
                          config: ConversionConfig | None = None) -> dict[str, Any]:
    """Return a copy with currency provenance attached."""
    cfg = config or ConversionConfig()
    out = dict(payload)
    out["currency"] = {
        "canonical": SOURCE_CURRENCY,
        "display": INR_CURRENCY,
        "conversion": cfg.to_dict(),
    }
    return out


def convert_numeric_tree(obj: Any, config: ConversionConfig | None = None,
                         suffix: str = "_inr") -> Any:
    """Recursively convert numeric values ending with *_usd to *_inr.

    USD values are preserved unchanged; only derived INR fields are added.
    """
    cfg = config or ConversionConfig()
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k.endswith("_usd"):
                out[k] = v
                out[k[:-4] + suffix] = usd_to_inr(v, cfg)
            elif isinstance(v, dict):
                out[k] = convert_numeric_tree(v, cfg, suffix)
            elif isinstance(v, list):
                out[k] = convert_numeric_tree(v, cfg, suffix)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [convert_numeric_tree(v, cfg, suffix) for v in obj]
    return obj


def traceability_table(rows: list[dict[str, Any]],
                       config: ConversionConfig | None = None) -> list[dict[str, Any]]:
    """Build a USD->INR traceability table with provenance."""
    cfg = config or ConversionConfig()
    out = []
    for r in rows:
        out.append({
            "label": r.get("label"),
            "value_usd": r.get("value_usd"),
            "value_inr": usd_to_inr(r.get("value_usd"), cfg),
            "source_currency": SOURCE_CURRENCY,
            "target_currency": INR_CURRENCY,
            **cfg.to_dict(),
        })
    return out


def load_config(path: str | None = None) -> ConversionConfig:
    if path is None:
        return ConversionConfig()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return ConversionConfig(**data)
