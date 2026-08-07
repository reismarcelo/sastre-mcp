# Changelog

All notable changes to this project are documented in this file.

## [0.6.0] - 2026-08-07

Migrated to MCP SDK v2.

### Added
- `mcp.allowed_hosts` — `Host` header values accepted by the transport's DNS-rebinding protection. Loopback binds and binds to a specific address contribute their own patterns automatically; `["*"]` disables the check for deployments where an upstream proxy validates `Host`.

### Changed
- Host/Origin validation is now always enabled. The MCP SDK only turns it on for loopback binds, which left it off for remote deployments; it is now configured explicitly for every bind address.
- `mcp.cors_origins` now also feeds the transport's allowed origins. Previously a configured origin passed the CORS preflight and was then rejected by the transport with `403 Invalid Origin header`.
- The MCP server identifies itself as `sastre-mcp` rather than `sastre-show`, and now reports its version to clients. Clients that key off the server name may need updating.

### Fixed
- Unusable configuration now exits non-zero from both entry points. A config referencing an unset environment variable exited `0`, which supervisors and container restart policies read as a clean shutdown; the `sastre-mcp` console script also raised a raw traceback where `python -m sastre_mcp` printed a clean message. `load_config` now reports every failure as a `RuntimeError` naming the config file, and the entry point logs it and exits `1`.

### Breaking
- `mcp.allowed_hosts` is **required** when `mcp.host` is `0.0.0.0` or `::`, since a wildcard bind does not identify the hostnames clients use. Existing wildcard-bind configs fail validation at startup until the list is added.

## [0.2.0] - 2026-06-09

### Added
- `show_template_values` tool — per-device variable values from device-template attachments (`sdwan show-template values`).
- `show_template_references` tool — how device templates reference feature templates (`sdwan show-template references`); JSON output forces `filled_rows`.
- `list_configuration` tool — configuration items stored on the manager, selected by one or more catalog `tags` (`sdwan list configuration`); empty `tags` are rejected.
- `list_certificates` tool — WAN edge device certificate information (`sdwan list certificate`).
- `list_configuration_tags` tool — catalog help listing the valid `tags` values for `list_configuration`.

## [0.1.0] - 2026-06-05
- Initial release
