# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - 2026-06-09

### Added
- `show_template_values` tool — per-device variable values from device-template attachments (`sdwan show-template values`).
- `show_template_references` tool — how device templates reference feature templates (`sdwan show-template references`); JSON output forces `filled_rows`.
- `list_configuration` tool — configuration items stored on the manager, selected by one or more catalog `tags` (`sdwan list configuration`); empty `tags` are rejected.
- `list_certificates` tool — WAN edge device certificate information (`sdwan list certificate`).
- `list_configuration_tags` tool — catalog help listing the valid `tags` values for `list_configuration`.

## [0.1.0] - 2026-06-05
- Initial release
