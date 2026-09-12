"""Pure utility helpers for the Home Energy Manager controller."""
import math
from datetime import datetime


def as_float(v):
    try:
        if v in (None, '', 'unknown', 'unavailable'):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except Exception:
        return None


def clamp(v, a, b):
    return max(a, min(b, v))


def iso(v):
    return v.isoformat() if v else None


def weighted_quantile(vals, wts, q):
    pts = sorted((float(v), float(w)) for v, w in zip(vals, wts) if w > 0)
    if not pts:
        return None
    tgt = sum(w for _, w in pts) * q
    acc = 0
    for v, w in pts:
        acc += w
        if acc >= tgt:
            return v
    return pts[-1][0]


def recency_weight(ts, now, half=45.0):
    age = max(0, (now - ts).total_seconds() / 86400)
    return .5 ** (age / half)
