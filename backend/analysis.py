"""Blood group call from the three wells' impedance curves.

Two ways to decide whether a well agglutinated:
  relative: % change from the start of the sweep (copper board: the reading rises)
  range:    final impedance falls in a known kOhm range (published graphite values)
"""
from statistics import median

WELLS = ("Anti-A", "Anti-B", "Anti-D")


def analyse(readings: list[dict], s: dict) -> dict:
    """readings: [{"t_ms":..., "z":[a,b,d]}] in time order. s: analysis settings."""
    method, thr, settle = s.get("method", "relative"), float(s.get("threshold", 8)), int(s.get("settle", 3))
    flags: list[str] = []
    usable = readings[settle:]
    if len(usable) < 6:
        return {"valid": False, "group": None, "confidence": 0, "method": method, "wells": [],
                "flags": ["Too few readings: let the sweep run longer."]}

    wells = []
    for k, name in enumerate(WELLS):
        series = [r["z"][k] for r in usable if r["z"][k] is not None]
        if len(series) < 6:
            flags.append(f"{name}: missing readings.")
            wells.append({"well": name, "value": None, "unit": "", "reaction": "indeterminate"})
            continue
        start, end = median(series[:3]), median(series[-5:])
        tail = series[-5:]
        spread = (max(tail) - min(tail)) / end * 100 if end else 0
        if end > 100_000:
            flags.append(f"{name}: looks empty (over 100 kΩ). Check the sample and contacts.")
        elif end < 50:
            flags.append(f"{name}: shorted (under 50 Ω). Check calibration and wiring.")
        if method == "range":
            kohm = end / 1000
            if s.get("agg_low", 14) <= kohm <= s.get("agg_high", 19):
                reaction = "positive"
            elif s.get("non_low", 30) <= kohm <= s.get("non_high", 38):
                reaction = "negative"
            else:
                reaction = "indeterminate"
            margin = 1.0 if reaction != "indeterminate" else 0.0
            wells.append({"well": name, "value": round(kohm, 2), "unit": "kΩ", "reaction": reaction, "_m": margin})
        else:
            change = (end - start) / start * 100 if start else 0
            signed = change if s.get("direction", "rise") == "rise" else -change
            reaction = "positive" if signed >= thr else "negative"
            if spread > thr:
                flags.append(f"{name}: still unstable at the end of the sweep.")
            margin = min(1.0, abs(abs(signed) - thr) / thr) if signed >= 0 else 1.0
            wells.append({"well": name, "value": round(change, 1), "unit": "%", "reaction": reaction, "_m": margin})

    zs = [[r["z"][k] for r in usable] for k in range(3)]
    for i in range(3):
        for j in range(i + 1, 3):
            if zs[i] == zs[j]:
                flags.append(f"{WELLS[i]} and {WELLS[j]} read identical values: the wells may be bridged.")

    if any(w["reaction"] == "indeterminate" for w in wells):
        flags.append("At least one well is outside both ranges: retest.")
    valid = not flags
    group = None
    if valid:
        a, b, d = (w["reaction"] == "positive" for w in wells)
        group = ("AB" if a and b else "A" if a else "B" if b else "O") + ("+" if d else "−")
    confidence = round(0.5 + 0.49 * min(w.get("_m", 0) for w in wells), 2) if valid else 0
    for w in wells:
        w.pop("_m", None)
    return {"valid": valid, "group": group, "confidence": confidence, "method": method, "wells": wells, "flags": flags}
