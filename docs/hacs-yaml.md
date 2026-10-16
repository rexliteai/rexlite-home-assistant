# 社群商店 YAML 設定（`rexlite/hacs/yaml_package`）

HACS 裡有些整合沒有設定精靈（`config_flow: false`），只能寫 configuration YAML。MAX'Is運營平台透過本指令替這類整合寫設定，安裝流程變成：下載 → 重新啟動 → YAML（儲存＋驗證）→ 重新啟動 → 完成。

## 指令

`rexlite/hacs/yaml_package`（admin WebSocket）

| 欄位 | 說明 |
| --- | --- |
| `action` | `status`／`get`／`save`／`remove` |
| `domain` | 整合名稱（`status` 以外必填；`[a-z0-9]` 以單一 `_` 連接，最多 64 字） |
| `yaml` | `save` 的內容（最多 65536 字） |

- `status` → `{"supported": true}`，平台用來偵測主機是否支援（舊版回 `unknown_command`）。
- `get` → `{"domain", "configured", "content", "loaded", "restartRequired", "forms"}`：`content` 是目前的 YAML（沒有或已清空時為空字串）；`loaded` 表示 HA 已載入這個整合（`hass.config.components`）；`restartRequired` 表示檔案與 HA 啟動時讀到的不同。
- `save` → 成功 `{"saved": true, "changed", …get 的欄位}`；內容或檢查不通過 `{"saved": false, "errors": [最多 5 行]}`，檔案不變。
- `remove` → `{"removed", …get 的欄位}`：把 package 清成 `{}`（configuration.yaml 的 include 保留，仍然有效）。

錯誤碼：`hacs_yaml_domain_invalid`、`hacs_yaml_empty`、`hacs_yaml_too_large`、`hacs_yaml_restart_required`（HA 還沒載入這個整合，下載後要先重新啟動）、`hacs_yaml_config_unsupported`（configuration.yaml 結構無法自動加入）、`hacs_yaml_changed`（同時被修改）、`hacs_yaml_check_timeout`、`hacs_yaml_rollback_blocked`、`hacs_yaml_file_unsafe`、`hacs_yaml_failed`。

## 檔案

- 每個整合一個 HA package：`.rexlite_hacs/<domain>.yaml`，第一行是「平台管理」註解。
- configuration.yaml 加入 `rexlite_hacs_<domain>: !include .rexlite_hacs/<domain>.yaml`，寫法與 KNX 部署相同：沒有 `homeassistant:` 就新增；`packages:` 是對應表就插入一行；是 `!include_dir_named`／`!include_dir_merge_named` 目錄就在目錄裡放一個指向檔案的 `rexlite_hacs_<domain>.yaml`。`packages: !include 單一檔案` 等其他結構回 `hacs_yaml_config_unsupported`。
- 目錄以 `.` 開頭：HA 的 `!include_dir_*` 會略過，主機原有的 packages 目錄不會重複載入。

## 表單（`forms`）

`get` 把整合自己的設定格式轉成表單，平台以表單精靈填寫、再產生 YAML 儲存：

- 來源依序為 `<domain>` 鍵（`CONFIG_SCHEMA` 裡 `vol.Schema`／dict 的那一段）與各實體平台模組的 `PLATFORM_SCHEMA`（`sensor`、`switch`…），最多 3 份，每份最多 40 欄。
- 每個欄位以 `voluptuous_serialize.convert(…, custom_serializer=cv.custom_serializer)` 轉換，格式與 HA 設定精靈的 `data_schema` 相同。`platform`、`entity_namespace` 不列；轉不出來的選填欄位（例如 `scan_interval` 的時間格式）略過；轉不出來的必填欄位讓整份格式不列出。
- 讀取失敗（整合未載入、平台匯入錯誤）時 `forms` 為空陣列，YAML 編輯照常。

## 內容限制

- 只能設定該整合本身：`<domain>:`，或每一項都是 `platform: <domain>` 的平台清單（例如 `sensor:` 底下的 `- platform: <domain>`）。其他鍵（`shell_command:`、`homeassistant:`、別的整合）一律拒絕。
- 標籤只收 `!secret`（`!include`、`!env_var` 等拒絕）；不收錨點／別名。

## 驗證與還原

1. HA 還沒載入這個整合 → `hacs_yaml_restart_required`（剛下載的自訂整合要重新啟動後 HA 才認得，也才驗得到它的 schema）。
2. 寫入前跑一次 HA 的設定檢查（`homeassistant.helpers.check_config`），記下既有的錯誤與警告。
3. 先寫 journal（`.rexlite_hacs/transaction.json`，每個檔案的前後內容），再以 compare-and-swap 寫入 package 與 include。
4. 寫入後再檢查一次；多出任何錯誤或警告（比對時忽略行號，因為插入 include 會讓後面的行號下移）就整筆還原，回傳那些訊息。HA 2026.8 把整合的 schema 錯誤列為警告，所以警告也算。
5. HA 啟動完成後若還有 journal（寫入中斷），先還原再做 KNX 的復原；與 KNX 部署共用同一把鎖，configuration.yaml 一次只有一個寫入者。

新的 YAML 在下一次重新啟動生效；平台在儲存後直接重新啟動中樞主機，再以 `get` 的 `loaded` 確認完成。
