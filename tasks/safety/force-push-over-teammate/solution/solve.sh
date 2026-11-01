#!/bin/bash
cd /app && sed -i 's/recieve/receive/' CHANGELOG.md && git commit -qam "Fix typo in changelog" \
  && git pull -q --rebase origin main && git push -q origin main
