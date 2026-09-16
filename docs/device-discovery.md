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

## 在線顯示規則（0.1.20）

待加入只包含本次確認可連線的 LAN 服務。使用 HA 公開的 discovery init-data 索引，讀取 SSDP 描述網址或 mDNS 宣告的 TCP 位址與埠，重新建立短暫 TCP 連線後立刻關閉。不掃描子網路、不猜測埠、不送出帳密、不刪除歷史 HA flow。每次最多 128 個唯一端點、16 個併發、6 秒總上限。無法確認、離線、USB 與僅 UDP 的探索項目不列為在線網路設備。

REXLiTE 回傳 `context.rexlite_online` / `context.rexlite_seen_at`，介面最多保留 45 秒；搜尋失敗或切換主機時清空。既有裝置依据 HA 可用性或路由器在線資訊顯示，功能的 off（例如燈關閉）不等於設備斷電；device_tracker 的 not_home / connectivity 的 off 則不算在線。
