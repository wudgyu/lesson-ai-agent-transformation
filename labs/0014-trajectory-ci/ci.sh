#!/usr/bin/env bash
# 最小 CI 门禁：单测 → 数据审计 → 发布 gate。
# 用法：
#   ./ci.sh                         # 用 hotfix 作为 candidate（应 PASS/WARN）
#   ./ci.sh runs/v04-candidate.jsonl # 用 candidate 验证门禁会 BLOCK
set -euo pipefail
cd "$(dirname "$0")"

python3 -m unittest test_trajectory_ci
python3 ci_cli.py audit
python3 ci_cli.py gate \
  --baseline runs/v03-baseline.jsonl \
  --candidate "${1:-runs/v05-hotfix.jsonl}"
