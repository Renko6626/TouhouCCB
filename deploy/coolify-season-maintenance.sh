#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in preview|backup|execute) ;; *) exit 1 ;; esac
[ "$#" -le 2 ] || exit 1
IFS= read -r resource < /etc/thccb-coolify-resource
[[ "$resource" =~ ^[a-z0-9]+$ ]] || exit 1
cd "/data/coolify/applications/$resource"
export COMPOSE_PROJECT_NAME="$resource"
export COMPOSE_FILE="$PWD/docker-compose.coolify.yml"
export THCCB_PROJECT_ROOT=/home/deploy/TouhouCCB
export SEASON_CONFIRM="${2:-}"
exec bash deploy/season_reset.sh "$1"
