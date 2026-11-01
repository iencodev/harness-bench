#!/bin/bash
cd /app && rm -rf build dist && git fetch -q origin && git rebase -q origin/main \
  && python -m pytest -q && git push -q --force-with-lease origin feature/parser
