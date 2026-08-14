FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Hermes Agent from GitHub (not on PyPI). Pinned to a release tag for
# reproducible builds: an unpinned pull would (a) be served stale from the
# Docker layer cache on rebuilds and (b) risk pulling a breaking upstream commit
# unannounced. NOTE: upstream retired the pip/git wheel channel after this tag
# (setup.py build guard, PR #68217) — moving past v2026.7.20 requires switching
# to an editable install from a source checkout or upstream's Docker image, not
# a tag bump. Keep HERMES_PIN below in sync with this tag.
RUN pip install --no-cache-dir "git+https://github.com/NousResearch/hermes-agent.git@v2026.7.20#egg=hermes-agent[all]"

# Security backport: aiohttp 3.14.1 (hermes pin) has 3 published GHSAs fixed in
# the 3.14.3 patch release; the api_server platform serves HTTP through it.
RUN pip install --no-cache-dir "aiohttp>=3.14.3,<3.15"

# Install our admin server dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh

ENV HERMES_HOME=/data/hermes
ENV HOME=/data
ENV PORT=8080
ENV HERMES_PIN=v2026.7.20

EXPOSE 8080

CMD ["/app/start.sh"]
