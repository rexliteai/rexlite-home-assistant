# Changelog

## 0.1.3

- Rename the integration and setup experience to REXLiTE AI.
- Simplify activation by using the managed REXLiTE gateway and local Home
  Assistant defaults.
- Add local brand assets for Home Assistant 2026.3 and newer.
- Strengthen product-copy, JSON, brand-asset, and release validation checks.

## 0.1.2

- Recreate the authenticated tunnel session whenever a user enables or disables remote administration.
- Close all requests and streams from the previous access mode before reconnecting.
- Preserve health-only monitoring while remote administration is disabled.

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
