# Changelog

## 0.1.1

- Fix dedicated-host Home Assistant pages remaining in a loading state.
- Close completed HTTP streams using RFC-compatible `HEAD`, `Content-Length`, and chunked response framing while preserving WebSocket upgrades.
- Prevent completed remote page requests from accumulating as active tunnel streams.

## 0.1.0

- Add HACS installation support for the REXLiTE Home Assistant integration.
- Support Home Assistant OS, Supervised, Container, and Core installations.
- Add connection status, automatic recovery, and user-manageable settings.
- Add Traditional Chinese and English localization.
- Add automated validation and release checks.
