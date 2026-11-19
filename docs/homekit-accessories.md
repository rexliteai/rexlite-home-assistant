# HomeKit 設備（`rexlite/homekit/accessories`）

管理員 WebSocket 指令，唯讀，給 MAX'Is 運營平台「主機部署 › HomeKit」列出主機看得到的 HomeKit 配件。配對仍走 HA 內建的 HomeKit 裝置（`homekit_controller`）設定流程：平台用回傳的 `flowId` 開啟流程、輸入 8 碼配對碼。

```json
{
  "accessories": [{
    "id": "1a:2b:3c:4d:5e:6f", "name": "Eve Energy 50FF", "model": "Eve Energy 20EBO8301",
    "category": 7, "transport": "ip", "host": "192.168.68.80", "port": 51826,
    "paired": false, "status": "ready", "nativeDomain": "", "flowId": "01JD…", "entryId": "",
    "lastSeen": "2026-11-19T14:00:00+00:00"
  }],
  "matter": [{
    "id": "8f3b2e1a2d4c6e7f", "name": "WiZ Downlight", "vendorId": 4107, "productId": 9,
    "deviceType": 269, "commissioningMode": 1, "discriminator": 3840, "transport": "ip",
    "host": "192.168.68.85", "port": 5540, "lastSeen": "2026-11-19T15:40:00+00:00"
  }],
  "bluetooth": {"adapters": 1},
  "thread": {"routers": 2, "preferredNetwork": true, "routes": 1},
  "scannedAt": "2026-11-19T14:00:01+00:00"
}
```

- `transport`：`ip`（`_hap._tcp`，要能 TCP 連線）、`thread`（`_hap._udp`）、`ble`（Apple HAP 廣播，5 分鐘內）。
- `status`：`ready` → `paired_elsewhere`（HAP 狀態旗標 bit 0 已清除，HA 會以 `already_paired` 略過）→ `native`（HA 的 HomeKit 型號表把它交給原廠整合，且不另外給 HomeKit 裝置）→ `added`（已有 `homekit_controller` 設定項目）→ `ignored`（HA 的 `HOMEKIT_IGNORE`）。同一台以 Wi-Fi、Thread、藍芽的順序只列一次；這台 HA 自己的 HomeKit Bridge 不列出。
- `category`：HAP 類別代碼（5 燈、7 插座、8 開關、9 溫控、10 感應器、14 窗簾、17 攝影機、21 冷氣…）。
- `thread.routes`：`/proc/net/ipv6_route` 裡經邊界路由器（`fe80::` 下一跳）通往 `fd…/64` 的路由數。0 而 `routers` 大於 0，表示主機沒有接受 IPv6 路由公告的路由選項（`accept_ra_rt_info_max_plen`），Thread 配件連不到。

不送任何資料給設備；Wi-Fi 配件只做一次 TCP 連線後立即關閉。

## 等待配對的 Matter 設備（`matter`，0.1.59）

「Works with Apple Home」也包含 Matter 設備，HomeKit 裝置（HAP）配不了；這裡列出配對視窗開著的 Matter 設備，平台以 Matter 加入（輸入設備的 Matter 配對碼）完成。

- 網路：`_matterc._udp`，`CM` 為 1（開機或重置後）或 2（其他控制中心開啟分享）才列；`id` 為 DNS-SD 實例名稱（16 位十六進位），`DN`／`VP`／`DT`／`D` 對應 `name`／`vendorId`+`productId`／`deviceType`／`discriminator`（沒有為 -1）。
- 藍芽：服務資料 UUID `0xFFF6`、opcode 0，位元組 1–2 低 12 位元為鑑別碼，3–4 廠商、5–6 產品（little-endian）；5 分鐘內的廣播才列，`id` 為藍芽位址。這種設備還沒連上網路，主機要開啟 Matter 藍芽配對才能直接配對。
- 同一台（廠商、產品、鑑別碼相同）網路與藍芽都看到時只列網路那筆。

## 診斷（`diagnostics`，0.1.60）

`network.adapters`（HA 網路介面，zeroconf 依此決定 IPv4／IPv6）、`network.hostIpv6`（`disabled`、`addresses[{interface, scope, address}]`）、`zeroconf.sockets[{ipv6, address}]`、`zeroconf.records`、`zeroconf.addressRecords{ipv4, ipv6}`、`zeroconf.serviceTypes`、`zeroconf.counts{服務類型: 實例數}`、`zeroconf.commissionable[{instance, txt, addresses}]`、`bluetooth{scanners, connectableScanners, advertisements, fresh, matter, homekit, names}`。看不到設備時：AAAA 為 0 或 socket 沒有 IPv6 → 主機收不到 IPv6 的 mDNS；`_matterc._udp` 為 0 而 Apple TV／Cast 有數字 → 設備沒在網路上廣播配對，或廣播被擋。
