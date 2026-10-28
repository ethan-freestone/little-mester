FROM python:3.12-slim

ARG LAB_UID=1000
ARG LAB_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates nodejs npm \
 && (apt-get install -y --no-install-recommends bats \
      || (rm -rf /usr/local/share/man/man1 /usr/local/share/man/man7 \
          && git clone --depth 1 https://github.com/bats-core/bats-core.git /tmp/bats \
          && bash /tmp/bats/install.sh /usr/local \
          && rm -rf /tmp/bats)) \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir aider-chat pytest

# OpenCode. Install without lifecycle scripts so platform selection can be diagnosed
# before running the package's postinstall in its normal package directory.
COPY diagnose-opencode-install.cjs /tmp/diagnose-opencode-install.cjs
RUN npm install -g --ignore-scripts @opencode/cli || { \
      status=$?; \
      echo "OpenCode npm install failed (status ${status}); sanitized npm diagnostic tail follows:"; \
      log="$(ls -t /root/.npm/_logs/*-debug-0.log 2>/dev/null | head -n 1)"; \
      if [ -n "$log" ]; then \
        grep -viE 'authorization|_auth|token|password' "$log" | tail -n 160; \
      else \
        echo "No npm debug log found under /root/.npm/_logs"; \
      fi; \
      exit "$status"; \
    } \
 && npm_root="$(npm root -g)" \
 && node /tmp/diagnose-opencode-install.cjs "$npm_root" \
 && (cd "$npm_root/@opencode/cli" && node ./postinstall.mjs) || { \
      status=$?; \
      echo "OpenCode postinstall failed (status ${status}); sanitized npm diagnostic tail follows:"; \
      log="$(ls -t /root/.npm/_logs/*-debug-0.log 2>/dev/null | head -n 1)"; \
      if [ -n "$log" ]; then \
        grep -viE 'authorization|_auth|token|password' "$log" | tail -n 160; \
      else \
        echo "No npm debug log found under /root/.npm/_logs"; \
      fi; \
      exit "$status"; \
    } \
 && rm /tmp/diagnose-opencode-install.cjs \
 && opencode --version

COPY opencode/opencode.json /etc/opencode/opencode/opencode.json
COPY opencode/plugins/little-mester-logo/ /etc/opencode/plugins/little-mester-logo/
COPY opencode/plugins/host-metrics/ /etc/opencode/plugins/host-metrics/
COPY opencode/plugins/host-metrics-tui/ /etc/opencode/plugins/host-metrics-tui/
COPY opencode/plugins/little-mester-exit-confirm/ /etc/opencode/plugins/little-mester-exit-confirm/

ENV PATH="/opt/venv/bin:$PATH" \
    HOME=/home/agent \
    OPENCODE_CONFIG=/etc/opencode/opencode/opencode.json \
    OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-logo","/etc/opencode/plugins/host-metrics-tui","/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}'

RUN groupadd -g ${LAB_GID} agent && useradd -m -u ${LAB_UID} -g ${LAB_GID} agent

WORKDIR /workspace
CMD ["opencode"]
