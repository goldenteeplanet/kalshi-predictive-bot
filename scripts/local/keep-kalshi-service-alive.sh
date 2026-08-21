#!/usr/bin/env bash
set -euo pipefail

systemctl --user start kalshi-fixed-rate-refresh.service
exec sleep infinity
