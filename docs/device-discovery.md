# Pending device discovery

`rexlite/discovery/scan` is an admin-only WebSocket command on the HA host. It
accepts no user-supplied destination, emits SSDP searches and bounded mDNS queries,
then returns `flows`, `candidates`, `warnings` and `scannedAt`. Concurrent requests
share one result for 15 seconds. Temporary listeners are always released.

Native HA discovery flows keep their original identities and pairing steps.
Deco candidates come from fresh TP-Link SSDP responses; their local management
address is proposed in the normal user config flow. Already configured Deco host
addresses are excluded, including disabled entries, to avoid duplicate setup.
A missing Deco custom integration is reported as requiring installation, not
silently installed. Discovery never sends a password or changes device settings.

The operations pending-device view scans on entry and every 30 seconds while
visible; its search button runs immediately. It requires the matching backend
`POST /ha/discovery-scan?hubId=...` and frontend release. HA 0.1.18 alone does not
add the button to older operations clients. Unsupported older hosts still expose
native pending flows and explicitly report that active scanning is unavailable.

Power alone is not network discoverability. Unprovisioned Wi-Fi devices need
network setup; Zigbee and Matter need their appropriate pairing mode. Multicast
blocked by a VLAN, client isolation or firewall cannot be discovered from this
host. The scanner does not alter network security or open pairing windows.

HA 2026.6.0 and later published releases are covered by the compatibility matrix.
Use REXLiTE 0.1.19 or newer and restart HA after updating. Callback registration
has an independent deadline because HA replays cached SSDP descriptions; stale
router records must not prevent native discovery probes. The operations API uses
25s (HA command), 30s (gateway) and 35s (browser) budgets around the bounded scan.
