#!/bin/bash
cat > /app/textkit/slug.py <<'PY'
import re


def slugify(text: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with one dash, trim dashes."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
PY
