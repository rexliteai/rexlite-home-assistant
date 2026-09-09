# Changelog

## 0.1.7

- Add administrator-only capability, verified ETS import, YAML deployment, and
  deployment-status WebSocket commands for one-upload KNX provisioning.
- Validate file fingerprints and actual KNX schemas, preserve manual YAML/UI
  entities, and transactionally activate a managed package with rollback.
- Verify effective configuration and registered entity states after reload;
  expose skipped mappings and unavailable state counts separately.
- Support automatic KNX deployment on every stable Core release from 2026.1.0:
  legacy address-format identities on 2026.1-7, stable native identities on
  2026.8, and custom identities for new deployments on 2026.9+.
- Preserve canonical identity metadata across Core upgrades and honor official
  registry migrations. Reject conflicting/duplicate identities and unsafe legacy
  address-format changes before changing YAML. Preserve nested manual RGB GAs.
- Support pre-May tuple KNX entity identifiers and avoid normalizing existing
  KNX schemas twice when preserving manual lights or climate entities.
- Report Core/schema incompatibility in capability preflight; the integration's
  minimum stays Core 2026.1.0.
- Restart Home Assistant after installing this release to register the new
  commands. Entry reload alone does not load newly installed integration code.

## 0.1.6

- Classify rejected service credentials separately from temporary network
  failures, stop permanent retry loops, and start Home Assistant reauthentication.
- Validate stored credentials before runtime setup while preserving Home
  Assistant's bounded setup retry for boot-time network unavailability.
- Distinguish a healthy preconfigured connection awaiting user activation from
  an active cloud service, without changing the stable service identity.
- Preserve the same authenticated agent identity across reboot and DHCP address
  changes, and re-publish the current LAN endpoint after connectivity returns.
- Redact the service identity, activation credential, and local Home Assistant
  address from diagnostics, and clean up runtime tasks during unload.

## 0.1.5

- Detect the current Home Assistant host LAN IPv4 address and report it with
  the stable tunnel agent ID.
- Reject loopback, public, link-local, and Docker bridge addresses so stale
  `127.0.0.1` and `172.17.0.1` values are never published as IPC endpoints.
- Re-publish tunnel metadata when the LAN IP changes, allowing the operation
  platform and mobile app to reconcile a moved IPC without re-pairing.

## 0.1.4

- Add compliant 256x256 and 512x512 local brand icons for supported Home
  Assistant versions.
- Consistently describe the customer-facing feature as REXLiTE AI Cloud
  Service in Traditional Chinese and English.
- Preserve existing configurations by keeping the internal service-setting
  schema unchanged.
- Add regression coverage for customer-facing cloud-service terminology.

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
