#!/bin/bash
# batch_backtest.sh — Run all strategies on a date range, one strategy at a time.
# Usage:
#   ./batch_backtest.sh 2023          # run all strategies on 2023 dates
#   ./batch_backtest.sh 2024          # run all strategies on 2024 dates
#   ./batch_backtest.sh 2025          # run all strategies on 2025 dates
#   nohup ./batch_backtest.sh 2023 > logs/batch_2023.log 2>&1 &   # background
#
# Each year batch takes ~10 hours. Run overnight with nohup or in tmux.

set -euo pipefail

YEAR="${1:-2023}"
FROM="${YEAR}-01-01"
TO="${YEAR}-12-31"
BASE="/var/www/horseracing"
cd "$BASE"

mkdir -p logs

STRATEGIES=(
    "均衡基礎策略"
    "大樣本信任策略"
    "強平滑策略"
    "步速主導策略"
    "深度推算策略"
    "熱門過濾策略"
    "穩健保守策略"
    "純技術指標策略"
    "黑馬獵手策略"
)

echo "============================================================"
echo "BATCH BACKTEST: $FROM → $TO"
echo "Strategies: ${#STRATEGIES[@]}"
echo "Started:    $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

TOTAL_STRATS=${#STRATEGIES[@]}
CURRENT=0
OVERALL_START=$(date +%s)

for STRAT in "${STRATEGIES[@]}"; do
    CURRENT=$((CURRENT + 1))
    STRAT_START=$(date +%s)
    echo ""
    echo "[$CURRENT/$TOTAL_STRATS] $STRAT — starting at $(date '+%H:%M:%S')"

    python3 -u backtest.py \
        --model "$STRAT" \
        --from "$FROM" \
        --to "$TO" \
        --force \
        2>&1 | tee "logs/${STRAT}_${YEAR}.log"

    STRAT_ELAPSED=$(($(date +%s) - STRAT_START))
    echo "[$CURRENT/$TOTAL_STRATS] $STRAT — done in $((STRAT_ELAPSED / 60))m $((STRAT_ELAPSED % 60))s"
done

OVERALL_ELAPSED=$(($(date +%s) - OVERALL_START))
echo ""
echo "============================================================"
echo "BATCH COMPLETE: $FROM → $TO"
echo "Total time: $((OVERALL_ELAPSED / 3600))h $(((OVERALL_ELAPSED % 3600) / 60))m"
echo "Finished:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

# Final summary
python3 -c "
from model_config import list_models
print()
print(f'{'策略':20s} {'Top-1':>6s} {'下注':>5s} {'勝':>3s} {'ROI':>10s} {'Dates':>6s}')
print('-' * 55)
for m in list_models():
    s = m.get('_summary', {})
    print(f\"{m['name']:20s} {s.get('top1_pct',0):>5.1f}% {s.get('bets_placed',0):>5d} {s.get('bets_won',0):>3d} {s.get('roi_units',0):>+9.1f}u {s.get('dates_run',0):>6d}\")
"
