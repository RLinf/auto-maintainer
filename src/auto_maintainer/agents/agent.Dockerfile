# The image every agent run starts from (agents/container.py). The agent CLI and the
# host's gh, uv, and ruff are mounted read-only at /opt/am/bin when a run starts, so
# the image needs only the system tools a review reads and builds with.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git curl wget jq ripgrep fd-find tree less file unzip xz-utils \
      make patch python3 python3-venv python3-pip python-is-python3 nodejs npm \
 && rm -rf /var/lib/apt/lists/*
