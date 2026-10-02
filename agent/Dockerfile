FROM python:3.12-slim

ARG LAB_UID=1000
ARG LAB_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir aider-chat pytest

# OpenCode. The version check makes the BUILD fail loudly if the binary isn't usable.
RUN npm install -g opencode-ai && opencode --version

COPY opencode.json /etc/opencode/opencode.json

RUN groupadd -g ${LAB_GID} agent && useradd -m -u ${LAB_UID} -g ${LAB_GID} agent

ENV PATH="/opt/venv/bin:$PATH" \
    HOME=/home/agent \
    OPENCODE_CONFIG=/etc/opencode/opencode.json

WORKDIR /workspace
CMD ["aider"]