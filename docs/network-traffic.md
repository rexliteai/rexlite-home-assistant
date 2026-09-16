# 網通設備流量

採集留在 HA 所在區域網路。REXLiTE 使用 HA 已有的實體或屬性，提供統一且只讀的 `rexlite/network_traffic` WebSocket 回應。MCP 後端將結果加入診斷的 `managedTraffic`，營運前端以 Mbps 顯示。這不是自動支援所有品牌型號的私有 API 驅動，也不安裝其他整合、不更動路由、防火牆或攝影機設定。

## 接入方式

| 品牌／系列 | 資料來源 | 設定方式與限制 |
| --- | --- | --- |
| TP-Link Deco | `tplink_deco` client tracker 速率屬性 | 既有後端 adapter 自動讀取；也可明確指定 HA 來源。需要 Deco 自訂整合與支援的本機 API。 |
| TP-Link 路由器／Omada | 已安裝整合所提供的速率 sensor／屬性，或 SNMP | 必須確認每個來源的實際單位；不能把 Wi-Fi 協商速度當成流量。HA 原生 Omada 不保證提供每個 client 的速率。 |
| VIGI | 支援 SNMP 的攝影機／NVR，或其交換器埠計數器 | 以型號／韌體與可用 MIB 為準；ONVIF／RTSP 本身不等於全機流量。交換器埠需使用 interface 範圍。 |
| Zyxel | SNMP IF-MIB 計數器 | 由原生 HA SNMP sensor 採集，不將 SNMP 密碼送到雲端。非管理型交換器不保證可用。 |
| UniFi／其他設備 | 已有 HA 速率或 SNMP 統計 | 既有 UniFi adapter 保留；其他來源可透過同一管理員動作設定。 |

裝置 `device`、介面／埠 `interface`、路由器總量 `router` 分別處理。只有 device 範圍可填入 HA device_id 並合併到原裝置卡片；另外兩種範圍為獨立卡片，不套用「每台裝置」的警示門檻。網路埠 RX/TX 顯示接收／傳送，不能直接認定為終端裝置下載／上傳。

## HA 管理員操作

更新本專案後，在 HA「開發者工具 → 動作」使用 `rexlite.configure_network_traffic`。畫面提供品牌、量測範圍、來源類型、單位、實體選擇器。來源必須先存在。相同 id 更新原設定；`rexlite.remove_network_traffic` 只移除對應，不刪除 sensor。

速率實體範例（依實際來源單位調整，不從名稱猜測）：

```yaml
action: rexlite.configure_network_traffic
data:
  id: gateway_wan
  name: TP-Link WAN
  provider: tp_link
  scope: router
  mode: rate
  unit: Mbit/s
  rx_entity: sensor.gateway_download
  tx_entity: sensor.gateway_upload
```

Deco 屬性範例：`scope: device`、指定真實 `device_id`、`mode: rate`、`unit: kB/s`，rx/tx entity 都選對應 client 的 `device_tracker`；rx_attribute=`down_kilobytes_per_s`、tx_attribute=`up_kilobytes_per_s`。device_id 不可使用 Deco 節點 ID 代替 client。

## SNMP 範例

使用 HA 原生 SNMP sensor。先核對機型支援、啟用唯讀 SNMP，以及確定介面 ifIndex。以下 OID 的最後 `.1` 僅為範例，必須換成實際埠索引；不是所有型號都實作 IF-MIB 高容量計數器。此檔僅為範例，不會自動寫入現場設定。

```yaml
sensor:
  - platform: snmp
    name: Network port 1 RX bytes
    unique_id: network_port1_rx
    host: !secret network_monitor_host
    version: "3"
    username: !secret network_snmp_user
    auth_key: !secret network_snmp_auth
    auth_protocol: hmac-sha
    priv_key: !secret network_snmp_priv
    priv_protocol: aes-cfb-128
    baseoid: 1.3.6.1.2.1.31.1.1.1.6.1 # ifHCInOctets
    unit_of_measurement: B
    scan_interval: 10
```

以相同連線設定另外建立以下三個 sensor。保留原始整數，不經浮點模板轉換：

| 名稱 | OID | 單位 |
| --- | --- | --- |
| Network port 1 TX bytes | `1.3.6.1.2.1.31.1.1.1.10.1`（ifHCOutOctets） | B |
| Network port 1 discontinuity | `1.3.6.1.2.1.31.1.1.1.19.1`（ifCounterDiscontinuityTime） | 不指定 |
| Network uptime | `1.3.6.1.2.1.1.3.0`（sysUpTime） | 不指定 |

依 HA 最終產生的 entity_id 設定：

```yaml
action: rexlite.configure_network_traffic
data:
  id: zyxel_port1
  name: Zyxel Port 1
  provider: zyxel
  scope: interface
  mode: counter
  unit: B
  rx_entity: sensor.network_port_1_rx_bytes
  tx_entity: sensor.network_port_1_tx_bytes
  discontinuity_entity: sensor.network_port_1_discontinuity
  uptime_entity: sensor.network_uptime
```

64-bit 位元組計數器以精確整數差值除以來源取樣時間，再乘 8 得到 bit/s。第一次取樣需要等待第二個有效樣本；結果是取樣區間平均，無法保證偵測兩次取樣間的瞬間尖峰。重置標記改變、uptime 倒退、計數器倒退、超過 120 秒資料缺口都重新建立基準；重複讀取不補零、不累積計時。只在診斷讀取時處理 HA 已有資料，不在雲端另行輪詢實體網路設備。

設定儲存於 HA `.storage/rexlite_network_traffic`，最多 128 個來源。讀取與設定使用 HA 管理員權限；保存失敗不套用新設定，無效儲存資料不阻止原有雲端通道啟動。輸出只包含名稱、品牌、範圍、deviceId、Mbps 所需速率與來源時間，沒有密碼、SNMP 憑證或任意原始屬性。

## 驗證範圍

純運算測試涵蓋 64-bit 精度、單位、重啟／重置、重複與過期樣本。`tests/network_traffic_runtime_check.py` 使用真正 HA service registry、Store、WebSocket dispatcher 驗證保存、失敗回滾與非管理員拒絕，已加入 HA 相容性 CI。這些測試不代表現場 Deco、VIGI 或 Zyxel 已完成連線；正式驗收需套用對應型號設定並比對設備管理介面的計數。

來源：[HA SNMP](https://www.home-assistant.io/integrations/snmp/)、[HA Omada](https://www.home-assistant.io/integrations/tplink_omada)、[Deco 整合](https://github.com/amosyuen/ha-tplink-deco)、[Zyxel SNMP](https://mysupport.zyxel.com/hc/en-us/articles/4411475992594-Nebula-SNMP-Configuration-Monitor-your-Device-via-SNMP)、[VIGI 使用手冊](https://www.tp-link.com/uk/document/24330/)、[IF-MIB RFC 2863](https://www.rfc-editor.org/rfc/rfc2863).
