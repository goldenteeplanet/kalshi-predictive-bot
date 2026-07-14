#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LOG_FILE="${NIGHTLY_LOG_FILE:-reports/nightly_paper_runner.log}"
PID_FILE="${NIGHTLY_PID_FILE:-reports/nightly_paper_runner.pid}"
LOCK_FILE="${NIGHTLY_LOCK_FILE:-reports/nightly_paper_runner.lock}"
MAX_CYCLES="${MAX_CYCLES:-32}"
INTERVAL_MINUTES="${INTERVAL_MINUTES:-15}"

mkdir -p reports data

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

export KALSHI_ENV="${KALSHI_ENV:-demo}"
export EXECUTION_ENABLED="false"
export LEARNING_MODE="${LEARNING_MODE:-true}"
export LEARNING_BLOCK_DEMO_EXECUTION="true"
export LEARNING_BLOCK_LIVE_EXECUTION="true"
export LEARNING_PRIORITIZE_FAST_SETTLEMENT="${LEARNING_PRIORITIZE_FAST_SETTLEMENT:-true}"
export LEARNING_MAX_DAYS_TO_SETTLEMENT="${LEARNING_MAX_DAYS_TO_SETTLEMENT:-3}"
export LEARNING_MAX_DAILY_PAPER_TRADES="${LEARNING_MAX_DAILY_PAPER_TRADES:-100}"
export LEARNING_TARGET_TRADES_PER_CYCLE="${LEARNING_TARGET_TRADES_PER_CYCLE:-10}"
export LEARNING_MIN_TRADES_PER_CYCLE="${LEARNING_MIN_TRADES_PER_CYCLE:-5}"
export LEARNING_CANDIDATE_SCAN_LIMIT="${LEARNING_CANDIDATE_SCAN_LIMIT:-500}"
export PAPER_LIQUIDITY_STARTING_CAPITAL="${PAPER_LIQUIDITY_STARTING_CAPITAL:-100}"
export CRYPTO_SYMBOLS="${CRYPTO_SYMBOLS:-BTC,ETH,SOL,XRP,DOGE}"
export SPORTS_LEAGUES="${SPORTS_LEAGUES:-MLB,WNBA,SOCCER}"
export SPORTS_DAYS_AHEAD="${SPORTS_DAYS_AHEAD:-3}"
export MARKET_LEG_PARSE_LIMIT="${MARKET_LEG_PARSE_LIMIT:-0}"
export ECONOMIC_INPUT_FILE="${ECONOMIC_INPUT_FILE:-data/economic_sample.json}"
export NEWS_INPUT_FILE="${NEWS_INPUT_FILE:-data/news_sample.json}"
export ENABLE_RSS_NEWS="${ENABLE_RSS_NEWS:-false}"
export PREFLIGHT_TIMEOUT_SECONDS="${PREFLIGHT_TIMEOUT_SECONDS:-90}"

if [[ -z "${DATABASE_URL:-}" && -d "/home/james/projects/kalshi-predictive-bot/data" ]]; then
  export DATABASE_URL="sqlite:////home/james/projects/kalshi-predictive-bot/data/kalshi_phase1.db"
fi

run_optional() {
  local label="$1"
  shift
  echo "[$(date --iso-8601=seconds)] $label"
  if "$@"; then
    echo "- $label: OK"
  else
    echo "- $label: WARNING/FAILED; continuing paper-only run"
  fi
}

active_tonight_run() {
  pgrep -af "[k]alshi-bot tonight-run"
}

ingest_sports_files_if_present() {
  shopt -s nullglob
  local files=(data/sports_*.json data/sports_*.csv)
  shopt -u nullglob
  if (( ${#files[@]} == 0 )); then
    echo "- sports ingestion: skipped; no data/sports_*.json or data/sports_*.csv files"
    return 0
  fi
  local file base league
  for file in "${files[@]}"; do
    base="$(basename "$file" | tr '[:lower:]' '[:upper:]')"
    league="ALL"
    [[ "$base" == *MLB* ]] && league="MLB"
    [[ "$base" == *NBA* ]] && league="NBA"
    [[ "$base" == *NFL* ]] && league="NFL"
    [[ "$base" == *NHL* ]] && league="NHL"
    run_optional "ingest-sports $league $file" kalshi-bot ingest-sports --league "$league" --input-file "$file"
  done
}

preflight() {
  echo "Nightly paper runner preflight"
  echo "- root: $ROOT_DIR"
  echo "- db: ${DATABASE_URL:-settings default}"
  echo "- max cycles: $MAX_CYCLES"
  echo "- interval minutes: $INTERVAL_MINUTES"
  echo "- paper liquidity start: $PAPER_LIQUIDITY_STARTING_CAPITAL"
  echo "- crypto symbols: $CRYPTO_SYMBOLS"
  echo "- sports leagues: $SPORTS_LEAGUES"
  echo "- sports days ahead: $SPORTS_DAYS_AHEAD"
  echo "- economic input: $ECONOMIC_INPUT_FILE"
  echo "- news input: $NEWS_INPUT_FILE"
  if [[ "${KALSHI_ENV,,}" =~ ^(prod|production|live)$ ]]; then
    echo "BLOCKED: KALSHI_ENV=$KALSHI_ENV is not allowed for this paper runner."
    exit 2
  fi
  if timeout "$PREFLIGHT_TIMEOUT_SECONDS" kalshi-bot learning-status; then
    echo "- learning-status: OK"
  else
    echo "- learning-status: WARNING/TIMEOUT; continuing paper-only run"
  fi
  if timeout "$PREFLIGHT_TIMEOUT_SECONDS" kalshi-bot paper-settlement-doctor --limit 50 --output-dir reports/paper_settlement_reconciliation; then
    echo "- paper-settlement-doctor: OK"
  else
    echo "- paper-settlement-doctor: WARNING/TIMEOUT; continuing paper-only run"
  fi
}

ingest_economic_if_present() {
  if [[ ! -f "$ECONOMIC_INPUT_FILE" ]]; then
    echo "- economic ingestion: skipped; missing $ECONOMIC_INPUT_FILE"
    return 0
  fi
  run_optional "ingest-economic $ECONOMIC_INPUT_FILE" kalshi-bot ingest-economic --input-file "$ECONOMIC_INPUT_FILE"
  run_optional "build-economic-features" kalshi-bot build-economic-features
  run_optional "link-economic-markets" kalshi-bot link-economic-markets
}

ingest_news_if_present() {
  if [[ -f "$NEWS_INPUT_FILE" ]]; then
    run_optional "ingest-news $NEWS_INPUT_FILE" kalshi-bot ingest-news --input-file "$NEWS_INPUT_FILE"
  else
    echo "- news file ingestion: skipped; missing $NEWS_INPUT_FILE"
  fi
  if [[ "${ENABLE_RSS_NEWS,,}" == "true" ]]; then
    run_optional "ingest-news rss" kalshi-bot ingest-news --source rss
  fi
  run_optional "link-news-markets" kalshi-bot link-news-markets
  run_optional "build-news-features" kalshi-bot build-news-features
}

bootstrap_sports_schedules() {
  run_optional "phase3af sports schedule bootstrap" \
    kalshi-bot phase3af-sports-schedule-bootstrap \
      --leagues "$SPORTS_LEAGUES" \
      --days-ahead "$SPORTS_DAYS_AHEAD" \
      --ingest \
      --output-dir reports/phase3af \
      --schedule-output-dir data/sports_schedules
}

refresh_links() {
  run_optional "collect-once open markets" kalshi-bot collect-once --status open --limit 100 --max-pages 5
  run_optional "market legs parse" kalshi-bot market-legs-parse --limit "$MARKET_LEG_PARSE_LIMIT" --refresh
  run_optional "ingest-crypto $CRYPTO_SYMBOLS" kalshi-bot ingest-crypto --symbols "$CRYPTO_SYMBOLS" --source coinbase
  run_optional "build-crypto-features $CRYPTO_SYMBOLS" kalshi-bot build-crypto-features --symbols "$CRYPTO_SYMBOLS"
  run_optional "link-crypto-markets" kalshi-bot link-crypto-markets
  run_optional "ingest-weather kansas_city" kalshi-bot ingest-weather --lat 39.0997 --lon -94.5786 --location-key kansas_city
  run_optional "build-weather-features kansas_city" kalshi-bot build-weather-features --location-key kansas_city
  run_optional "link-weather-markets" kalshi-bot link-weather-markets
  ingest_economic_if_present
  ingest_news_if_present
  bootstrap_sports_schedules
  ingest_sports_files_if_present
  run_optional "derive-sports-schedule" kalshi-bot derive-sports-schedule --build-features
  run_optional "link-sports-markets" kalshi-bot link-sports-markets --league ALL
  run_optional "build-sports-features" kalshi-bot build-sports-features --league ALL
  run_optional "phase3ae verified sports connector" kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae
  run_optional "phase3ag sports ambiguity coverage" kalshi-bot phase3ag-sports-ambiguity-coverage --output-dir reports/phase3ag_sports
}

run_cycle() {
  local cycle="$1"
  echo "[$(date --iso-8601=seconds)] Nightly paper cycle $cycle/$MAX_CYCLES"
  refresh_links
  run_optional "forecast all" kalshi-bot forecast --model all
  run_optional "forecast crypto_v2" kalshi-bot forecast --model crypto_v2
  run_optional "forecast weather_v2" kalshi-bot forecast --model weather_v2
  run_optional "forecast sports_v1" kalshi-bot forecast --model sports_v1
  run_optional "forecast economic_v1" kalshi-bot forecast --model economic_v1
  run_optional "forecast news_v1" kalshi-bot forecast --model news_v1
  run_optional "find crypto opportunities" kalshi-bot find-opportunities --model-name crypto_v2 --limit 100 --output reports/opportunities_crypto_v2.md
  run_optional "find weather opportunities" kalshi-bot find-opportunities --model-name weather_v2 --limit 100 --output reports/opportunities_weather_v2.md
  run_optional "find sports opportunities" kalshi-bot find-opportunities --model-name sports_v1 --limit 100 --output reports/opportunities_sports_v1.md
  run_optional "sports opportunities report" kalshi-bot sports-opportunities --model-name sports_v1 --league ALL --limit 100 --output reports/sports_opportunities.md
  run_optional "find economic opportunities" kalshi-bot find-opportunities --model-name economic_v1 --limit 100 --output reports/opportunities_economic_v1.md
  run_optional "find news opportunities" kalshi-bot find-opportunities --model-name news_v1 --limit 100 --output reports/opportunities_news_v1.md
  run_optional "learning-once" kalshi-bot learning-once
  run_optional "sync-settlements" kalshi-bot sync-settlements
  run_optional "paper-pnl" kalshi-bot paper-pnl
  run_optional "model-confidence" kalshi-bot model-confidence
  run_optional "learning-report" kalshi-bot learning-report --output reports/learning_report.md
  run_optional "phase3aa settlement eta" kalshi-bot phase3aa-realize --output-dir reports/phase3aa --sync-settlements --dry-run --limit 500
  run_optional "phase3ab learning governor" kalshi-bot phase3ab-learning-governor --output-dir reports/phase3ab --limit 500
  run_optional "phase3ag crypto pipeline" kalshi-bot phase3ag-crypto-pipeline --output-dir reports/phase3ag_crypto --limit 500
  run_optional "market coverage doctor" kalshi-bot market-coverage-doctor --output-dir reports/market_coverage
  run_optional "paper-settlement-doctor" kalshi-bot paper-settlement-doctor --limit 200 --output-dir reports/paper_settlement_reconciliation
  run_optional "learning-status" kalshi-bot learning-status
}

run_loop() {
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Another nightly paper runner is already active. Refusing to start a second writer."
    exit 3
  fi
  if active_tonight_run >/dev/null; then
    echo "Another kalshi-bot tonight-run process is already active. Refusing to start a second writer."
    active_tonight_run
    exit 3
  fi
  echo "$$" > "$PID_FILE"
  trap 'echo "Stopping nightly paper runner"; rm -f "$PID_FILE"; exit 0' INT TERM
  preflight
  local cycle
  for ((cycle = 1; cycle <= MAX_CYCLES; cycle++)); do
    run_cycle "$cycle"
    if (( cycle < MAX_CYCLES )); then
      sleep "$((INTERVAL_MINUTES * 60))"
    fi
  done
  run_optional "final tonight-report" kalshi-bot tonight-report --output reports/tonight_report.md
  run_optional "final phase3ag crypto pipeline" kalshi-bot phase3ag-crypto-pipeline --output-dir reports/phase3ag_crypto --limit 1000
  run_optional "final phase3ae verified sports connector" kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae
  run_optional "final market coverage doctor" kalshi-bot market-coverage-doctor --output-dir reports/market_coverage
  rm -f "$PID_FILE"
  echo "Nightly paper runner completed $MAX_CYCLES cycle(s)."
}

start() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "nightly paper runner already running as PID $(cat "$PID_FILE")"
    exit 0
  fi
  if active_tonight_run >/dev/null; then
    echo "kalshi-bot tonight-run is already active:"
    active_tonight_run
    exit 0
  fi
  nohup "$0" run >> "$LOG_FILE" 2>&1 < /dev/null &
  echo "$!" > "$PID_FILE"
  echo "started nightly paper runner PID $(cat "$PID_FILE")"
  echo "log: $ROOT_DIR/$LOG_FILE"
}

status() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "RUNNING pid=$(cat "$PID_FILE")"
  elif active_tonight_run >/dev/null; then
    echo "RUNNING external tonight-run"
  else
    echo "STOPPED"
  fi
  active_tonight_run || true
}

stop() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill -INT "$(cat "$PID_FILE")" || true
  fi
  pkill -INT -f "[k]alshi-bot tonight-run" || true
  rm -f "$PID_FILE"
  echo "stop requested"
}

tail_log() {
  touch "$LOG_FILE"
  tail -f "$LOG_FILE"
}

case "${1:-run}" in
  run) run_loop ;;
  start) start ;;
  status) status ;;
  stop) stop ;;
  tail) tail_log ;;
  preflight) preflight ;;
  refresh-links) refresh_links ;;
  *)
    echo "Usage: $0 {start|run|status|stop|tail|preflight|refresh-links}"
    exit 1
    ;;
esac
