# Sastre-MCP

Streamable HTTP [MCP](https://modelcontextprotocol.io) server that exposes [Sastre](https://github.com/CiscoDevNet/sastre) commands (the `cisco-sdwan` Python package).

## Requirements

- Python 3.14+
- Reachable Cisco SD-WAN Manager (vManage)
- A `config.yaml` file, use [`config.example.yaml`](config.example.yaml) as a template

## Install

```bash
cd /path/to/sastre-mcp
python3 -m pip install -e .
```

## Run

```bash
cp config.example.yaml config.yaml
# Edit config.yaml: sdwan_managers (name, address, credentials), mcp bearer_token, etc.

python3 -m sastre_mcp
# or: sastre-mcp
```

The server loads **`config.yaml`** from the current working directory.

MCP endpoint (default): `http://127.0.0.1:8765/mcp` (use HTTPS behind a reverse proxy in production).

### Configuration file

| Section | Purpose |
|---------|---------|
| `sdwan_managers` | List of SD-WAN Managers. Each entry needs a unique `name` plus host, port, auth (`user`/`password` or `apikey`), optional `tenant`, `timeout` (1–3600 s) |
| `default_manager` | Optional; name of the manager used when a tool call omits `manager`. Defaults to the first entry |
| `mcp` | HTTP bind `host`/`port`, optional `bearer_token`, `stateless_http`, `cors_origins` (list; empty disables CORS), `disable_rate_limit` (testing) |
| `limits` | Input and DoS caps (regex length, body size, rate limit window, etc.); optional — defaults match the previous hard-coded values |

**Bind rule:** If `mcp.host` is `0.0.0.0` or `::`, `mcp.bearer_token` is **required** (enforced when the file is validated).

**Auth rule:** For each manager, if `apikey` is not set (or empty), `user` and `password` are required.

**Manager rule:** Each `sdwan_managers` entry must have a unique `name`; if set, `default_manager` must match one of them.

Sensitive fields should use strong values and restrictive file permissions on `config.yaml`. Restrict it to the service account, e.g. `chmod 600 config.yaml`. Do not commit `config.yaml` (it is listed in `.gitignore`).

#### Environment-variable indirection for secrets

**Any string value** in the config may reference an environment variable, which is expanded when the file is loaded:

- `${VAR}` — replaced with the value of `VAR`; **startup fails** with a clear error if `VAR` is unset (so a missing secret never silently becomes empty).
- `${VAR:-default}` — replaced with `VAR` if set, otherwise the literal `default`.
- `$${` — escape for a literal `${` when a value genuinely needs that sequence.

This works for `password`, `apikey`, `bearer_token`, or any other field. Example:

```yaml
sdwan_managers:
  - name: "primary"
    address: "10.0.0.1"
    user: "admin"
    password: "${PRIMARY_VMANAGE_PASSWORD}"
mcp:
  bearer_token: "${MCP_BEARER_TOKEN}"
```

Then supply the secrets from your environment or a secrets manager (systemd `LoadCredential`, Docker/Kubernetes secrets, Vault agent, etc.) at runtime:

```bash
export PRIMARY_VMANAGE_PASSWORD='...'
export MCP_BEARER_TOKEN='...'
python3 -m sastre_mcp
```

### Logging

The server logs to the console and to a rotating file. The configuration is static and defined in code (`sastre_mcp/__main__.py`); it is not customizable via environment variables, so logging behaves identically across deployments:

| Setting | Value | Purpose |
|---------|-------|---------|
| Root / file level | `DEBUG` | Root and file-handler level |
| Console level | `WARNING` | Console-handler level |
| Log file | `logs/sastre-mcp.log` | Rotating log file path (3 backups, 200 KB each) |

If the log file's directory cannot be created or the file is not writable (e.g. a `--read-only` container or a non-writable working directory), file logging is skipped and the server logs to the console only instead of failing to start.

## Cursor

Add an MCP server entry of type **HTTP** (streamable) pointing at your base URL, e.g. `http://127.0.0.1:8765/mcp`, and configure the header `Authorization: Bearer <mcp.bearer_token>` when a token is set in config.

## Tools

- `show_devices` — device inventory
- `show_realtime` — realtime operational (`command` list, e.g. `["omp","adv-routes"]`)
- `show_state` — bulk state
- `show_statistics` — statistics (optional `days` / `hours`)
- `show_alarms` / `show_events` — manager alarms and events
- `list_sdwan_managers` — list configured managers and the default
- `list_show_operational_commands` — catalog help for command tokens

Optional `output_format`: `text` (default) or `json` per tool.

**Selecting a manager:** Every show tool accepts an optional `manager` argument naming one of the configured `sdwan_managers`. Omit it to use `default_manager`. Call `list_sdwan_managers` to see the available names.

## Security notes

- Terminate TLS at a reverse proxy for remote access.
- Do not expose `0.0.0.0` without `mcp.bearer_token`.
- SD-WAN Manager credentials never appear in tool arguments; they live only in `config.yaml` on the server. Tool callers can select a manager only by its configured `name`.

See [`SECURITY.md`](SECURITY.md) for deployment hardening guidance and how to report vulnerabilities.

### Rate limiting

A per-client-IP fixed-window limit is enabled by default (`limits.rate_limit_window_secs` / `limits.rate_limit_max_requests`; disable with `mcp.disable_rate_limit`).

- **Behind a reverse proxy:** by default the limiter keys on the direct peer address, so all clients arriving through a proxy share one bucket. Set `limits.rate_limit_trusted_proxies` to the proxy IP(s) or CIDR(s) you control; for requests from those addresses the client IP is then taken from the `X-Forwarded-For` header (the closest entry that is not itself a trusted proxy). `X-Forwarded-For` is only honored when the request actually arrives from a trusted proxy, because the header is otherwise spoofable.
- **Multi-worker / multi-instance:** counters live in process memory, so the limit is enforced independently per worker process. With `N` workers the effective global limit is roughly `N * rate_limit_max_requests` per window. For accurate limiting across scaled deployments, enforce limits at the reverse proxy / API gateway or use a shared store (e.g. Redis). Quiet IP buckets are pruned automatically, so memory does not grow unboundedly.

### vManage session pooling

Logged-in `cisco-sdwan` (`Rest`) sessions are pooled and reused per manager instead of logging in and out on every tool call. The pool is thread-safe (show tasks run in worker threads) and a given session is never used by two requests at once.

- **Concurrency bound:** `limits.max_sessions_per_manager` (default `4`) caps how many sessions/connections may be open against a single SD-WAN Manager simultaneously, acting as a semaphore. When all sessions are busy, a request waits up to `limits.session_acquire_timeout_secs` (default `30`) for one to free up; otherwise the caller gets a "busy, retry shortly" message.
- **Idle eviction:** an idle session unused for longer than `limits.session_max_idle_secs` (default `600`) is closed and re-created on next use, avoiding stale server-side sessions. Set to `0` to disable idle eviction. A reused session that fails mid-request is discarded and retried once with a fresh login.
- **Multi-worker / multi-instance:** as with rate limiting, the pool is per process, so each worker maintains its own sessions (up to `N * max_sessions_per_manager` total across `N` workers).

## Docker

A [`Dockerfile`](Dockerfile) builds a minimal image that runs as a non-root user.

```bash
docker build -t sastre-mcp .

# Mount config.yaml read-only and supply secrets via the environment.
# Set mcp.host to 0.0.0.0 in config.yaml so the server is reachable from outside
# the container (which requires mcp.bearer_token to be set).
docker run --rm -p 8765:8765 \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -e MCP_BEARER_TOKEN='...' \
  --read-only --security-opt no-new-privileges --cap-drop ALL \
  sastre-mcp
```

Do not bake `config.yaml` or secrets into the image; provide them at runtime. Terminate TLS at a reverse proxy in production.

The image's `HEALTHCHECK` probes `SASTRE_MCP_HEALTHCHECK_PORT` (default `8765`). When you run the server on a non-default `mcp.port`, set this env var to match so the health probe targets the right port, e.g. `-e SASTRE_MCP_HEALTHCHECK_PORT=9000`.

For local use, the [`run-docker.sh`](run-docker.sh) helper runs the container with the current directory bind-mounted into `/app` (where the server looks for `config.yaml`):

```bash
./run-docker.sh

# Read-only mount, custom port/tag, with a bearer token from the environment:
MOUNT_RO=1 HOST_PORT=9000 IMAGE=sastre-mcp:dev MCP_BEARER_TOKEN='...' ./run-docker.sh
```

It forwards `MCP_BEARER_TOKEN` into the container only when set and applies `--security-opt no-new-privileges` and `--cap-drop ALL`. The container runs as a  non-root user (uid 10001), so the mounted `config.yaml` must be readable by that uid.

## Dev

```bash
python3 -m pip install -e ".[dev]"
pytest
```
