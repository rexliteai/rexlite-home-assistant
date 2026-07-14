# REXLiTE for Home Assistant

<p align="center">
  <img src="assets/logo.png" width="250" height="100" alt="REXLiTE logo">
</p>

REXLiTE 官方 Home Assistant 整合，可透過 HACS 安裝與更新。

## 安裝

安裝前請先完成 [HACS](https://www.hacs.xyz/docs/use/download/download/) 設定。

[![在 HACS 開啟 REXLiTE repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rexliteai&repository=rexlite-home-assistant&category=integration)

若按鈕無法自動開啟：

1. 開啟 **HACS**。
2. 右上角選單選擇 **自訂儲存庫（Custom repositories）**。
3. 輸入 `https://github.com/rexliteai/rexlite-home-assistant`。
4. 類別選擇 **Integration**，按下 **新增**。
5. 開啟 **REXLiTE** 並按 **下載**。
6. 重新啟動 Home Assistant。
7. 前往 **設定 > 裝置與服務 > 新增整合**，搜尋 **REXLiTE**。
8. 依畫面指示完成設定。

最低支援 Home Assistant `2026.1.0`。若 HACS 已下載但「新增整合」仍找不到 REXLiTE，請先重新啟動 Home Assistant，再強制重新整理瀏覽器快取。

## 支援環境

本整合支援下列 Home Assistant 安裝方式：

- Home Assistant OS
- Home Assistant Supervised
- Home Assistant Container
- Home Assistant Core

若畫面顯示目前的 Home Assistant 不支援重新導向，請返回 Home Assistant，並依上方「若按鈕無法自動開啟」步驟，從 HACS 手動加入本整合。

## 功能

- 提供 REXLiTE 服務連線與狀態顯示。
- 支援連線中斷後自動恢復。
- 提供可由使用者管理的功能設定。
- 支援繁體中文與英文介面。

如需協助，請聯絡 REXLiTE 支援服務。安全性問題請依 [SECURITY.md](SECURITY.md) 私下通報，請勿在公開 Issue 張貼帳號、設定內容、系統網址或日誌。
