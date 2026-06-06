# syntax=docker/dockerfile:1

# Pinned base image. For stronger supply-chain guarantees, pin by digest, e.g.:
#   FROM python:3.14-slim@sha256:<digest> AS build
FROM python:3.14-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

# Build a wheel and install it (plus deps) into an isolated prefix that the
# final stage copies. Keeps build tooling out of the runtime image.
COPY . .
RUN python -m pip install --upgrade pip && \
    python -m pip install --prefix=/install .

# ---- Runtime ----
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create an unprivileged user to run the service.
RUN useradd --system --create-home --uid 10001 sastre

# Copy the installed package and console script from the build stage.
COPY --from=build /install /usr/local

WORKDIR /app
USER sastre

# The MCP server binds to mcp.host/mcp.port from config.yaml (default port 8765).
# To accept connections from outside the container, set mcp.host to 0.0.0.0,
# which in turn requires mcp.bearer_token to be set.
EXPOSE 8765

# Port the HEALTHCHECK probes. Keep this in sync with mcp.port in config.yaml;
# when running on a non-default port pass e.g. -e SASTRE_MCP_HEALTHCHECK_PORT=9000.
ENV SASTRE_MCP_HEALTHCHECK_PORT=8765

# Provide config.yaml at runtime (do NOT bake secrets into the image), e.g.:
#   docker run --rm -p 8765:8765 \
#     -v "$PWD/config.yaml:/app/config.yaml:ro" \
#     -e MCP_BEARER_TOKEN=... \
#     --read-only --security-opt no-new-privileges --cap-drop ALL \
#     sastre-mcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket,sys; port=int(os.environ.get('SASTRE_MCP_HEALTHCHECK_PORT','8765')); s=socket.socket(); s.settimeout(3); sys.exit(0 if s.connect_ex(('127.0.0.1',port))==0 else 1)"

ENTRYPOINT ["sastre-mcp"]
