# Text MMO full-stack image (#71).
# One command: `docker compose up` runs server + dashboard + agents.
# Default image is lean (server + dashboard + ml env + botfarm, no torch).
# Torch profile builds with --build-arg INSTALL_TORCH=1 (CPU wheel).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Lean runtime deps first (better layer cache).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional PyTorch (CPU) for the torch-farm profile only.
# Kept as a build arg so the default `docker compose up` stays small.
ARG INSTALL_TORCH=0
RUN if [ "$INSTALL_TORCH" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu; \
    fi

# Runtime code: server (game + dashboard + GM stream), configs, world,
# ML env + botfarm + conductor, torch agents. Tests ship too so the
# image can run the offline suites in CI.
COPY server.py dashboard.html world.json server_config.json ./
COPY ml/ ./ml/
COPY torch_agents/ ./torch_agents/
COPY tests/ ./tests/

EXPOSE 8765 8766 8767

# Liveness probe for compose `depends_on: condition: service_healthy` (#56).
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8766/health').read()"

CMD ["python", "server.py"]
