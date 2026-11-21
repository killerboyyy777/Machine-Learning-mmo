"""Price-discovery measurements for the player market (#628, read-only).

The server owns the market and its Elo ladder. This module never places an
order and imports no game code: it derives a reference band for each item
from vendor prices and recipe costs, then compares observed mid prices from
a measurement file against that band. It also reports rolling volatility and
the Gini coefficient of player gold. The output is a sibling JSON file next
to the measurement, for analysis only.
"""

import json
import math
import os
import statistics

# The market records item display names, while world.json is keyed by id.
# A missing value is dropped rather than guessed, so a reference band is
# only as complete as the world data that produced it.


def _item_names(world):
    """Map display name -> item id from world.json's items table."""
    names = {}
    for iid, spec in (world.get("items") or {}).items():
        name = spec.get("name") if isinstance(spec, dict) else None
        if isinstance(name, str) and name:
            names[name] = iid
    return names


def _finite(value):
    """Return a finite float, or None for missing/non-numeric values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def vendor_bounds(world):
    """Per item id, the vendor sell value (low) and shop buy price (high).

    The vendor pays `items[iid]['value']` when a player sells and charges
    `npcs.merchant.shop[iid]` when a player buys. Those two numbers bound a
    no-arbitrage price band before the player market is considered.
    """
    items = world.get("items") or {}
    shop = (world.get("npcs") or {}).get("merchant", {}).get("shop") or {}
    bounds = {}
    for iid, spec in items.items():
        sell = _finite(spec.get("value")) if isinstance(spec, dict) else None
        buy = _finite(shop.get(iid))
        if sell is not None or buy is not None:
            bounds[iid] = {"sell": sell, "buy": buy}
    return bounds


def recipe_costs(world):
    """Per result item id, the sum of input sell values.

    A recipe cost is a production floor: below it, a crafter loses value by
    making the item instead of selling the inputs. Inputs with no known
    value are skipped, so a partial cost is better than an invented one.
    """
    items = world.get("items") or {}
    costs = {}
    for spec in (world.get("recipes") or {}).values():
        if not isinstance(spec, dict):
            continue
        result = spec.get("result")
        total = 0.0
        known = False
        for iid, qty in (spec.get("inputs") or {}).items():
            entry = items.get(iid)
            value = _finite(entry.get("value")) if isinstance(entry, dict) else None
            if value is None:
                continue
            try:
                total += value * float(qty)
                known = True
            except (TypeError, ValueError):
                continue
        if known and isinstance(result, str):
            costs[result] = round(total, 6)
    return costs


def reference_prices(world):
    """Static reference band per item id: {low, high, mid}.

    `low` is the vendor sell value, raised to the recipe cost when the item
    is crafted. `high` is the vendor shop buy price. `mid` is the midpoint
    of whichever bounds exist (one bound uses that value directly).
    """
    bounds = vendor_bounds(world)
    costs = recipe_costs(world)
    refs = {}
    for iid, band in bounds.items():
        low = band["sell"]
        high = band["buy"]
        cost = costs.get(iid)
        if low is None:
            low = cost
        elif cost is not None:
            low = max(low, cost)
        pair = [v for v in (low, high) if v is not None]
        if not pair:
            continue
        refs[iid] = {
            "low": low,
            "high": high,
            "mid": round(sum(pair) / len(pair), 6),
        }
    for iid, cost in costs.items():
        if iid not in refs:
            refs[iid] = {"low": cost, "high": None, "mid": cost}
    return refs


def observed_mids(market_state, names=None, names_to_ids=None):
    """Best available price per item id from one market snapshot.

    With both an ask and a bid the mid is their mean; with only one side,
    that side's best price is the available signal. Display names are mapped
    to ids so the keys line up with the reference band.
    """
    names_to_ids = names_to_ids if names_to_ids is not None else {}
    asks = {}
    for row in market_state.get("orders") or []:
        if not isinstance(row, dict):
            continue
        iid = names_to_ids.get(row.get("item"), row.get("item"))
        price = _finite(row.get("price"))
        if iid is not None and price is not None:
            # The server keeps append-ordered lists; the best ask is the
            # LOWEST price, not the one listed last (#705 review).
            if iid not in asks or price < asks[iid]:
                asks[iid] = price
    bids = {}
    for row in market_state.get("bids") or []:
        if not isinstance(row, dict):
            continue
        iid = names_to_ids.get(row.get("item"), row.get("item"))
        price = _finite(row.get("price"))
        if iid is not None and price is not None:
            # Best bid is the HIGHEST price, regardless of list order.
            if iid not in bids or price > bids[iid]:
                bids[iid] = price
    mids = dict(asks)
    for iid, bid in bids.items():
        ask = mids.get(iid)
        if ask is None:
            mids[iid] = bid
        else:
            mids[iid] = round((ask + bid) / 2.0, 6)
    return mids


def mid_deviation_pct(observed, reference):
    """Signed percent deviation of an observed mid from a reference mid."""
    if observed is None or reference is None or reference == 0:
        return None
    return round((observed - reference) / reference * 100.0, 6)


def rolling_volatility(values, window=10):
    """Mean standard deviation of percent price changes in rolling windows.

    Returns 0.0 when there are fewer than two changes. Using percent changes
    rather than raw prices keeps the number comparable across cheap arrows
    and expensive deeds.
    """
    changes = []
    for prev, curr in zip(values, values[1:]):
        a, b = _finite(prev), _finite(curr)
        if a is None or b is None or a == 0:
            continue
        changes.append((b - a) / a)
    if not changes:
        return 0.0
    if window is not None and window > 0:
        changes = changes[-window:]
    if len(changes) < 2:
        return 0.0
    return round(statistics.pstdev(changes) * 100.0, 6)


def gold_gini(values):
    """Gini coefficient of a gold distribution (0 equal, 1 concentrated)."""
    clean = sorted(v for v in (_finite(x) for x in values) if v is not None and v >= 0)
    n = len(clean)
    if n == 0:
        return 0.0
    total = sum(clean)
    if total == 0:
        return 0.0
    weighted = sum((i + 1) * v for i, v in enumerate(clean))
    gini = (2.0 * weighted) / (n * total) - (n + 1.0) / n
    return round(max(0.0, min(1.0, gini)), 6)


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def write_equilibrium(measurement_path, world_path=None, out_path=None):
    """Derive a price-discovery report from a measurement file.

    Reads `samples[].state.market` and `samples[].state.players`, compares
    the last observed mid per item to the vendor/recipe reference band, and
    records per-item volatility plus the gold Gini over the run. Writes a
    sibling JSON and returns its path. Pure analysis: no server, no orders.
    """
    measurement_path = os.path.abspath(measurement_path)
    base = os.path.basename(measurement_path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    world_path = world_path or os.path.join(root, "world.json")
    with open(world_path, encoding="utf-8") as handle:
        world = json.load(handle)
    with open(measurement_path, encoding="utf-8") as handle:
        measurement = json.load(handle)

    names_to_ids = _item_names(world)
    refs = reference_prices(world)
    id_to_name = {
        iid: spec.get("name", iid)
        for iid, spec in (world.get("items") or {}).items()
        if isinstance(spec, dict)
    }

    mids_by_item = {}
    gold_series = []
    for sample in measurement.get("samples") or []:
        state = sample.get("state") or {}
        mids = observed_mids(state.get("market") or {}, names_to_ids=names_to_ids)
        for iid, mid in mids.items():
            mids_by_item.setdefault(iid, []).append(mid)
        golds = [
            p.get("gold") for p in (state.get("players") or []) if isinstance(p, dict)
        ]
        if golds:
            gold_series.append(gold_gini(golds))

    items = {}
    for iid, band in refs.items():
        series = mids_by_item.get(iid) or []
        last = series[-1] if series else None
        items[id_to_name.get(iid, iid)] = {
            "item_id": iid,
            "reference": band,
            "last_mid": last,
            "deviation_pct": mid_deviation_pct(last, band["mid"]),
            "volatility_pct": rolling_volatility(series),
            "samples": len(series),
        }

    if out_path is None:
        stem = base[:-5] if base.endswith(".json") else base
        # The sibling name must never resolve to the input itself: without a
        # `measurements-` prefix the replace is a no-op and the report would
        # overwrite the measurement it was derived from (#705 review).
        if "measurements-" in stem:
            name = stem.replace("measurements-", "equilibrium-") + ".json"
        else:
            name = stem + "-equilibrium.json"
        out_path = os.path.join(os.path.dirname(measurement_path), name)
    out_path = os.path.abspath(out_path)
    report = {
        "schema_version": 1,
        "source": base,
        "world": os.path.basename(world_path),
        "gold_gini_latest": gold_series[-1] if gold_series else 0.0,
        "gold_gini_mean": (
            round(sum(gold_series) / len(gold_series), 6) if gold_series else 0.0
        ),
        "items": items,
    }
    _atomic_write_json(out_path, report)
    return out_path
