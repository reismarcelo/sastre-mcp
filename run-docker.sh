#!/usr/bin/env bash
#
# Run the sastre-mcp Docker container with the current host directory
# bind-mounted into /app (where the server looks for config.yaml).
#
# Usage:
#   ./run-docker.sh [extra docker run args...] [-- extra container args...]
#
# Environment overrides:
#   IMAGE             Image name/tag to run            (default: sastre-mcp)
#   HOST_PORT         Host port to publish             (default: 8765)
#   CONTAINER_PORT    Container port to map to         (default: 8765)
#   MOUNT_RO          "1" to mount the directory read-only (default: 0)
#   MCP_BEARER_TOKEN  Passed through to the container if set
#
# The container's HEALTHCHECK is pointed at CONTAINER_PORT automatically; set
# CONTAINER_PORT to match mcp.port in config.yaml when using a non-default port.
#
set -euo pipefail

IMAGE="${IMAGE:-sastre-mcp}"
HOST_PORT="${HOST_PORT:-8765}"
CONTAINER_PORT="${CONTAINER_PORT:-8765}"
MOUNT_RO="${MOUNT_RO:-0}"

# Resolve the current directory to an absolute path for the bind mount source.
HOST_DIR="$(pwd -P)"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH" >&2
  exit 1
fi

mount_spec="${HOST_DIR}:/app"
if [[ "${MOUNT_RO}" == "1" ]]; then
  mount_spec="${mount_spec}:ro"
fi

# Build the docker run argument list.
args=(
  run --rm -it
  -p "${HOST_PORT}:${CONTAINER_PORT}"
  -v "${mount_spec}"
  --security-opt no-new-privileges
  --cap-drop ALL
  # Keep the in-container HEALTHCHECK aligned with the port the server listens on.
  -e "SASTRE_MCP_HEALTHCHECK_PORT=${CONTAINER_PORT}"
)

# Forward the bearer token only when it is set; never bake secrets into images.
if [[ -n "${MCP_BEARER_TOKEN:-}" ]]; then
  args+=(-e "MCP_BEARER_TOKEN=${MCP_BEARER_TOKEN}")
fi

# Any extra args provided on the command line are appended verbatim. This lets
# callers add their own `docker run` flags and/or container arguments.
args+=("$@")

args+=("${IMAGE}")

exec docker "${args[@]}"
