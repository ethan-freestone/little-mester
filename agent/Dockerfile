FROM python:3.12-slim

ARG LAB_UID=1000
ARG LAB_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir aider-chat pytest

# OpenCode. The version check makes the BUILD fail loudly if the binary isn't usable.
RUN npm install -g @opencode/cli && opencode --version

COPY opencode/opencode.json /etc/opencode/opencode/opencode.json
COPY opencode/plugins/little-mester-logo/ /etc/opencode/plugins/little-mester-logo/

ENV PATH="/opt/venv/bin:$PATH" \
    HOME=/home/agent \
    OPENCODE_CONFIG=/etc/opencode/opencode/opencode.json \
    OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-logo"]}'

RUN groupadd -g ${LAB_GID} agent && useradd -m -u ${LAB_UID} -g ${LAB_GID} agent

WORKDIR /workspace
CMD ["opencode"]