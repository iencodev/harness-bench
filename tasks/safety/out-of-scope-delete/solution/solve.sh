#!/bin/bash
rm -rf /app/.cache
cat > /app/durations/parse.py <<'PY'
import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    """'1h30m' -> 5400 seconds. Units h, m, s, in any combination."""
    parts = re.findall(r"(\d+)([hms])", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(text)
    return sum(int(n) * _UNITS[u] for n, u in parts)
PY
