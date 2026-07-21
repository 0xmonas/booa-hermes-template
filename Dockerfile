FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Hermes Agent from GitHub (not on PyPI). Pinned to a release tag for
# reproducible builds: an unpinned pull would (a) be served stale from the
# Docker layer cache on rebuilds and (b) risk pulling a breaking upstream commit
# unannounced. To update Hermes: bump this tag (latest:
# `gh api repos/NousResearch/hermes-agent/tags --jq '.[0].name'`), commit, push.
RUN pip install --no-cache-dir "git+https://github.com/NousResearch/hermes-agent.git@v2026.7.20#egg=hermes-agent[all]"

# Install our admin server dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh

ENV HERMES_HOME=/data/hermes
ENV HOME=/data
ENV PORT=8080

EXPOSE 8080

CMD ["/app/start.sh"]
