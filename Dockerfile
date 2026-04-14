FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ripgrep && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Hermes Agent from GitHub (not on PyPI)
RUN pip install --no-cache-dir "git+https://github.com/NousResearch/hermes-agent.git#egg=hermes-agent[all]"

# Install our admin server dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HERMES_HOME=/data/hermes
ENV HOME=/data
ENV PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]
