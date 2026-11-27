"""Read-only daily energy history for a Remeha appliance.

Adapted from hugo-leij/remeha_home (energy_history.py), reshaped for the
figures that reconcile on a heat pump with an electric backup heater:

- The appliance per-use figures (heating, hot water, cooling; consumed and
  delivered) are consistent and include the backup heater.
- Producer totals reconcile with the appliance (heat pump consumed + backup
  consumed = appliance consumed), but the per-use split per producer does not,
  so only producer totals are returned.

Values are the cloud's whole-kWh estimates. A missing value stays None; it is
never counted as zero.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math

MAX_HISTORY_DAYS = 366
HISTORY_BATCH_DAYS = 31
# The cloud keeps adding the last hours of a day until the early morning
# after it, so a day only counts as complete from this local time next day.
DAY_COMPLETE_TIME = time(6, 0)

APPLIANCE_FIGURES = {
    "heating_consumed_kwh": "heatingEnergyConsumed",
    "heating_delivered_kwh": "heatingEnergyDelivered",
    "hot_water_consumed_kwh": "hotWaterEnergyConsumed",
    "hot_water_delivered_kwh": "hotWaterEnergyDelivered",
    "cooling_consumed_kwh": "coolingEnergyConsumed",
    "cooling_delivered_kwh": "coolingEnergyDelivered",
}
PRODUCER_FIGURES = {
    "consumed_kwh": "energyConsumptionTotal",
    "delivered_kwh": "energyProductionTotal",
}


def last_complete_day(now: datetime) -> date:
    """Return the last local day whose figures no longer change."""
    return now.date() - timedelta(days=1 if now.time() >= DAY_COMPLETE_TIME else 2)


def history_batches(start: date, end: date):
    """Yield inclusive date ranges of at most HISTORY_BATCH_DAYS days."""
    if end < start:
        raise ValueError("The end date must be on or after the start date")
    if (end - start).days >= MAX_HISTORY_DAYS:
        raise ValueError(f"A request may cover at most {MAX_HISTORY_DAYS} days")
    cursor = start
    while cursor <= end:
        batch_end = min(end, cursor + timedelta(days=HISTORY_BATCH_DAYS - 1))
        yield cursor, batch_end
        cursor = batch_end + timedelta(days=1)


def _energy(value) -> float | None:
    """Return a finite, non-negative number, or None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _sum(values: list[float | None]) -> float | None:
    """Sum the values; unknown if there are none or any is unknown."""
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def row_date(item: dict) -> date | None:
    """Return the local date a daily row belongs to.

    Finished days are stamped at midnight UTC of their own date. The current
    day carries a report time (e.g. T07:00:00+00:00), so its raw date is used
    as well; both read as the row's date.
    """
    stamp = item.get("timeStamp")
    if not isinstance(stamp, str) or len(stamp) < 10:
        return None
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def producer_key(producer: dict) -> str | None:
    """Return the same '<producerType>_<instance>' key the sensors use."""
    kind = producer.get("producerType")
    if not isinstance(kind, str) or not kind:
        return None
    return f"{kind}_{producer.get('instanceWithinDevice') or 1}"


def normalize_day(item: dict) -> dict | None:
    """Return one JSON-safe day, or None if the row has no data."""
    if not isinstance(item, dict) or item.get("isDataReported") is False:
        return None
    day = row_date(item)
    if day is None:
        return None

    figures = {key: _energy(item.get(field)) for key, field in APPLIANCE_FIGURES.items()}
    consumed = _sum([figures[k] for k in figures if k.endswith("_consumed_kwh")])
    delivered = _sum([figures[k] for k in figures if k.endswith("_delivered_kwh")])

    producers: dict[str, dict] = {}
    statistics = item.get("producerPerformanceStatistics")
    raw = statistics.get("producers") if isinstance(statistics, dict) else None
    duplicated: set[str] = set()
    for producer in raw if isinstance(raw, list) else []:
        if not isinstance(producer, dict) or (key := producer_key(producer)) is None:
            continue
        if key in producers:
            duplicated.add(key)
            continue
        producers[key] = {
            "producer_type": producer["producerType"],
            "energy_type": producer.get("energyType"),
            **{f: _energy(producer.get(src)) for f, src in PRODUCER_FIGURES.items()},
        }
    # The API can repeat a producer; don't guess which record is right.
    for key in duplicated:
        producers[key].update(dict.fromkeys(PRODUCER_FIGURES))

    return {
        "date": day.isoformat(),
        **figures,
        "total_consumed_kwh": consumed,
        "total_delivered_kwh": delivered,
        "cop": _ratio(delivered, consumed),
        "producers": producers,
    }


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def combine_history(start: date, end: date, responses: list[dict]) -> dict:
    """Combine batch responses into days within [start, end] plus period totals."""
    by_date: dict[str, dict] = {}
    conflicting: set[str] = set()
    for response in responses:
        rows = response.get("data") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            raise TypeError("The energy history response has no data list")
        for raw_day in rows:
            row = normalize_day(raw_day)
            if row is None or not start <= date.fromisoformat(row["date"]) <= end:
                continue
            day = row["date"]
            if day in conflicting:
                continue
            if day in by_date and by_date[day] != row:
                conflicting.add(day)
                del by_date[day]
                continue
            by_date[day] = row

    days = [by_date[key] for key in sorted(by_date)]
    requested = (end - start).days + 1
    missing = [
        d.isoformat()
        for d in (start + timedelta(days=n) for n in range(requested))
        if d.isoformat() not in by_date
    ]

    def total(field: str) -> float | None:
        return _sum([row[field] for row in days])

    period = {key: total(key) for key in (*APPLIANCE_FIGURES, "total_consumed_kwh", "total_delivered_kwh")}
    period["cop"] = _ratio(period["total_delivered_kwh"], period["total_consumed_kwh"])
    period["heating_cop"] = _ratio(period["heating_delivered_kwh"], period["heating_consumed_kwh"])
    period["hot_water_cop"] = _ratio(period["hot_water_delivered_kwh"], period["hot_water_consumed_kwh"])

    producer_keys = sorted({key for row in days for key in row["producers"]})
    producers = {}
    for key in producer_keys:
        entries = [row["producers"].get(key) for row in days]
        first = next(e for e in entries if e is not None)
        producers[key] = {
            "producer_type": first["producer_type"],
            "energy_type": first["energy_type"],
            **{
                f: _round(_sum([e[f] if e else None for e in entries]))
                for f in PRODUCER_FIGURES
            },
        }
    backup = [p for p in producers.values() if p["producer_type"] == "ElectricBackup"]
    backup_consumed = _sum([p["consumed_kwh"] for p in backup]) if backup else None
    period["backup_heater_share"] = _ratio(backup_consumed, period["total_consumed_kwh"])

    for row in days:
        row["cop"] = _round(row["cop"])
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "complete": not missing,
        "days_requested": requested,
        "days_reported": len(days),
        "missing_dates": missing,
        "period": {
            **{k: _round(v) for k, v in period.items()},
            "producers": producers,
        },
        "days": days,
        "note": "Remeha cloud estimates in whole kWh per day; daily COP is coarse, use the period COP.",
    }

