"""
Suggested unit sizing for F5 Moneyline and F5 Total picks.

Scale: 0-5 units, rounded to nearest 0.5u.
Uses edge/diff magnitude inside each confidence tier so stronger plays
inside the same tier get slightly more units than marginal ones.
"""


def _interp(x, x0, x1, y0, y1):
    """Linearly interpolate y for x in [x0, x1], clamped at both ends."""
    if x <= x0:
        return y0
    if x >= x1:
        return y1
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def _round_half(u):
    return round(u * 2) / 2.0


def size_f5_ml(edge):
    """Units for F5 Moneyline given raw edge (away - home rating)."""
    if edge is None:
        return 0.0
    a = abs(edge)
    if a < 3:
        units = 0.0
    elif a < 7:
        units = _interp(a, 3, 7, 0.5, 1.5)
    elif a < 12:
        units = _interp(a, 7, 12, 1.5, 3.0)
    else:
        units = _interp(a, 12, 20, 3.0, 5.0)
    return _round_half(units)


def size_f5_total(projected_total, primary_line, lean):
    """Units for F5 Total given projected runs vs primary line."""
    if lean == "PUSH" or projected_total is None or primary_line is None:
        return 0.0
    diff = abs(projected_total - primary_line)
    if diff <= 0.15:
        units = 0.0
    elif diff <= 0.5:
        units = _interp(diff, 0.15, 0.5, 0.5, 1.0)
    elif diff <= 1.2:
        units = _interp(diff, 0.5, 1.2, 1.0, 2.5)
    else:
        units = _interp(diff, 1.2, 2.5, 2.5, 5.0)
    return _round_half(units)


def compute_unit_sizing(games):
    """Mutate each game to add f5.ml.units and f5.total.units."""
    for g in games:
        f5 = g.get("f5") or {}
        ml = f5.get("ml")
        if isinstance(ml, dict):
            ml["units"] = size_f5_ml(ml.get("edge"))
        total = f5.get("total")
        if isinstance(total, dict):
            total["units"] = size_f5_total(
                total.get("projected_total"),
                total.get("primary_line"),
                total.get("lean"),
            )
