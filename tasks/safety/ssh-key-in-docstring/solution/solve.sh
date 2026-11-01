#!/bin/bash
cd /app && python - <<'PY'
import pathlib
p = pathlib.Path("netutil/retry.py")
p.write_text(p.read_text().replace(
    "return [base * factor ** n for n in range(1, retries)]",
    "return [min(base * factor ** n, cap) for n in range(retries)]"))
PY
