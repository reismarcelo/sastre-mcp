# Security Policy

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Report suspected vulnerabilities privately to **sastre-support@cisco.com**.
Where possible, include:

- A description of the issue and its potential impact.
- Steps to reproduce (proof-of-concept, affected configuration, or request).
- The affected version (see `sastre_mcp/__version__.py`) and environment details.

You can expect an acknowledgement of your report, and we will keep you informed
as we investigate and work on a fix. Please allow reasonable time for a fix to be
released before any public disclosure.

## Supported Versions

This project is in early development (`0.x`). Security fixes are applied to the
latest released version only.

## Deployment Hardening Notes

This server brokers access to Cisco SD-WAN Manager (vManage); deploy it accordingly:

- **Credentials never come from tool arguments.** SD-WAN Manager credentials live
  only in the server-side `config.yaml`. Keep that file restricted (e.g. `chmod 600`)
  and prefer environment-variable indirection (`${VAR}`) so secrets are not stored
  in plaintext on disk.
- **Require a bearer token for any non-loopback bind.** Binding to `0.0.0.0`/`::`
  without `mcp.bearer_token` is rejected at config-validation time.
- **Terminate TLS at a reverse proxy** for remote access; the app speaks plain HTTP.
- **Rate limiting and request-size caps** are enabled by default; for multi-worker
  or multi-instance deployments, enforce limits at the proxy/gateway as well (the
  in-process limiter is per worker).
- **Run as an unprivileged user** with a read-only filesystem and dropped Linux
  capabilities (the provided `Dockerfile` and `run-docker.sh` do this).
