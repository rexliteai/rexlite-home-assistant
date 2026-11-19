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
