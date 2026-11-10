# The image every agent run starts from (agents/container.py). The agent CLI and the
# host's gh, uv, ruff, and node are mounted read-only at /opt/am/bin when a run starts,
# so the image needs only the system tools a review reads and builds with, and the
# libraries the host's node links (libatomic1: official Node builds need it).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git curl wget jq ripgrep fd-find tree less file unzip xz-utils \
      make patch python3 python3-venv python3-pip python-is-python3 nodejs npm libatomic1 \
 && rm -rf /var/lib/apt/lists/*
