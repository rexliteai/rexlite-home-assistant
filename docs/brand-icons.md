# REXLiTE 圖示修復

REXLiTE 原本的金色房屋圖示位於 `custom_components/rexlite/brand/`。
`0.1.11` 的 HACS 原始碼封裝與 `rexlite.zip` 都包含這些檔案；不要換圖或
重裝 KNX 來處理圖示問題。

## Home Assistant 整合頁

Home Assistant 2026.3 起透過 `/api/brands/integration/rexlite/` 讀取整合
內的圖示，深色及高解析度請求會由 Core 選擇可用圖檔。升級整合並完成
必要的 Home Assistant 重啟後，完整重新整理瀏覽器頁面。

2026-09-10 的實機檢查：重新載入頁面後，`0.1.11` 已正確顯示原本的
金色房屋圖示，並讀取本機 `dark_icon@2x.png` API。沒有更換品牌圖檔。

## HACS 2.0.5 商店列表

HACS 2.0.5 的獨立前端仍使用舊品牌 CDN，無法讀取整合內的品牌圖示。
這與 Home Assistant 整合頁使用的 API 不同；HACS 列表顯示
`icon not available` 不代表 REXLiTE 的安裝套件缺圖。

- [Home Assistant 官方品牌 API 說明](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
- [HACS 上游問題](https://github.com/hacs/integration/issues/5171)
- [尚未合併的上游修正](https://github.com/hacs/frontend/pull/937)

### 明確執行的主機維護修復

`scripts/repair_hacs_rexlite_icon.py` 可在安裝 HACS 2.0.5 的主機上執行。
它不是 REXLiTE 整合的啟動程序，不會在 HACS 更新 REXLiTE 時自動執行。
需要這份儲存庫、Python 3.10 以上，以及 Home Assistant 設定目錄的寫入權限。

先確認 `/config` 是目標主機真正的設定目錄；Container 安裝請改成掛載來源
的實際路徑。只檢查、不修改：

```sh
python3 scripts/repair_hacs_rexlite_icon.py --config /config
```

套用：

```sh
python3 scripts/repair_hacs_rexlite_icon.py --config /config --apply
```

修復只將 REXLiTE 的 icon 回傳改為嵌入同一個原始 PNG，其他品牌 URL
保持原樣。圖示不再依賴外部 CDN，也不新增網路端點、token 或背景工作。
程式核對 HACS 2.0.5 正式封裝的 14 個前端 JavaScript SHA-256，同步更新
gzip，並備份至設定目錄中的 `.rexlite-hacs-icon-backup-2.0.5/`。
若版本、檔案或原始 icon 不符，會在修改前停止；重複套用不會重複修改。

完成後完整重新整理 HACS 頁面，例如 macOS Chrome 的 Command + Shift + R。
不需要重啟 Home Assistant。還原原始前端：

```sh
python3 scripts/repair_hacs_rexlite_icon.py --config /config --restore
```

這是 HACS 2.0.5 的主機端相容性修復，HACS 後續更新可能覆蓋它。不要把
此修復強行套用到其他 HACS 版本；上游正式修正後應使用官方版本。

### 驗證紀錄

2026-09-10 已在官方 HACS 2.0.5 發佈 ZIP 的隔離副本驗證：14 個
JavaScript 檔案語法、每個檔案 64 種品牌／類型／主題 URL 組合、gzip
一致性、重複套用、逐位元組還原、寫入失敗回復，以及拒絕被其他工具
修改的檔案。REXLiTE 原有 156 項本機測試亦通過（1 項正式 HA schema
測試需在完整 HA 環境執行）。這些結果不代表已套用至使用者現場主機。
