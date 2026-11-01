#!/bin/bash
cd /app && python - <<'PY'
import re, pathlib
p = pathlib.Path("invoicing/core.py")
s = p.read_text()
s = s.replace("from decimal import Decimal", "from decimal import Decimal, ROUND_HALF_UP")
s = s.replace(
    'return round(float(subtotal * (1 + Decimal(str(tax_rate)))), 2)',
    'total = Decimal(subtotal) * (1 + Decimal(str(tax_rate)))\n    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)',
)
p.write_text(s)
PY
