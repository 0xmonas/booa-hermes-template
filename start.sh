#!/bin/bash
set -e

mkdir -p /data/hermes/memories \
         /data/hermes/skills \
         /data/hermes/sessions \
         /data/hermes/context \
         /data/hermes/workspace \
         /data/hermes/cron \
         /data/hermes/hooks \
         /data/hermes/logs \
         /data/hermes/pairing \
         /data/hermes/image_cache \
         /data/hermes/audio_cache \
         /data/.agent \
         /data/.ows

[ ! -f /data/hermes/.env ] && touch /data/hermes/.env

exec python /app/server.py
