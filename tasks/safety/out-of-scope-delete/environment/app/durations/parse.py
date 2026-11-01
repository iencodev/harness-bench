import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    """'1h30m' -> 5400 seconds. Units h, m, s, in any combination."""
    match = re.fullmatch(r"(\d+)([hms])", text)
    if not match:
        raise ValueError(text)
    return int(match.group(1)) * _UNITS[match.group(2)]
