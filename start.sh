#!/bin/bash
# Entrypoint for the BOOA Hermes container.
#
# Ensures the Hermes data tree exists on whatever is mounted at /data (a
# freshly attached volume, an existing one, or the container filesystem if
# no volume is attached). All mkdir calls are idempotent — a populated volume
# is never modified. Derived from praveen-ks-2001/hermes-agent-template's
# startup pattern; adapted for our /data/hermes layout.
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

# Hermes may try to read .env on boot even before wizard completion; touch it
# so the read doesn't produce an opaque warning. write_config() will populate
# it with real values after the user finishes the wizard.
[ ! -f /data/hermes/.env ] && touch /data/hermes/.env

exec python /app/server.py
