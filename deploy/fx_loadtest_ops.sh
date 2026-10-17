#!/usr/bin/env bash
# Narrow maintenance operations for the authorized FX capacity run.
set -euo pipefail
cd "$(dirname "$0")/.."
action=${1:?configure or observe} tag=${2:?run ID}
[[ "$tag" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$ ]] || exit 1
umask 077
mkdir -p "backups/fx-loadtest/$tag"
if [ "$action" = configure ]; then
  target=$(readlink -f /etc/nginx/sites-enabled/thccb.conf)
  [ -f "$target" ] && [[ "$target" == /etc/nginx/* ]] || { echo 'Unexpected enabled site path'; exit 1; }
  stat -c "Site owner=%U mode=%a path=%n" "$target"
  sudo -n -l
  sudo -n nginx -t
  candidate=$(mktemp)
  trap 'rm -f "$candidate"' EXIT
  cat "$target" > "$candidate"
  if ! grep -q 'zone=api_general_user:10m' "$candidate"; then
    patch --dry-run "$candidate" < deploy/fx_loadtest_nginx.patch
    patch "$candidate" < deploy/fx_loadtest_nginx.patch
    backup="backups/fx-loadtest/$tag/nginx-before-$(date -u +%Y%m%dT%H%M%SZ).conf"
    cp "$candidate" "backups/fx-loadtest/$tag/nginx-candidate.conf"
    cp -p "$target" "$backup"
    if [ -w "$target" ]; then
      install -m 644 "$candidate" "$target"
    else
      sudo -n install -m 644 "$candidate" "$target"
    fi
    if ! sudo -n nginx -t; then
      sudo -n cp -p "$backup" "$target"
      sudo -n nginx -t
      exit 1
    fi
    sudo -n systemctl reload nginx
  fi
  sudo -n nginx -t
  echo 'Nginx token buckets installed; no load-generator whitelist added.'
  docker compose exec -T backend python -c 'from app.services.realtime import *; print("Backend import OK")'
  docker stats --no-stream --format '{{json .}}' thccb-backend thccb-postgres
elif [ "$action" = observe ]; then
  # Fixed 5-minute sample window. Contains aggregate counters, no identities or query text.
  output="backups/fx-loadtest/$tag/server-metrics.jsonl"
  : > "$output"
  for i in $(seq 1 30); do
    date -u +'{"sample_at":"%Y-%m-%dT%H:%M:%SZ"}' >> "$output"
    docker stats --no-stream --format '{{json .}}' thccb-backend thccb-postgres >> "$output"
    docker compose exec -T postgres psql -U thccb -d thccb -Atc "select json_build_object('connections',count(*),'active',count(*) filter(where state='active'),'waiting',count(*) filter(where wait_event_type='Lock')) from pg_stat_activity where datname='thccb';" >> "$output"
    sleep 8
  done
else
  exit 1
fi
