# REXLiTE AI for Home Assistant

<p align="center">
  <img src="assets/logo.png" width="250" height="100" alt="REXLiTE logo">
</p>

REXLiTE AI 為居家與場域提供安全、穩定的雲端服務。完成啟用後，使用者可依需求隨時開啟或關閉遠端服務，並持續掌握服務狀態。

## 安裝

安裝前請先完成 [HACS](https://www.hacs.xyz/docs/use/download/download/) 設定。

[![在 HACS 開啟 REXLiTE repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rexliteai&repository=rexlite-home-assistant&category=integration)

若按鈕無法自動開啟：

1. 開啟 **HACS**。
2. 右上角選單選擇 **自訂儲存庫（Custom repositories）**。
3. 輸入 `https://github.com/rexliteai/rexlite-home-assistant`。
4. 類別選擇 **Integration**，按下 **新增**。
5. 開啟 **REXLiTE AI** 並按 **下載**。
6. 重新啟動 Home Assistant。
7. 前往 **設定 > 裝置與服務 > 新增整合**，搜尋 **REXLiTE AI**。
8. 依畫面指示完成設定。

最低支援 Home Assistant `2026.1.0`。若 HACS 已下載但「新增整合」仍找不到 REXLiTE AI，請先重新啟動 Home Assistant，再強制重新整理瀏覽器快取。

## 支援環境

本整合支援下列 Home Assistant 安裝方式：

- Home Assistant OS
- Home Assistant Supervised
- Home Assistant Container
- Home Assistant Core

若畫面顯示目前的 Home Assistant 不支援重新導向，請返回 Home Assistant，並依上方「若按鈕無法自動開啟」步驟，從 HACS 手動加入本整合。

## 功能

- 提供 REXLiTE AI 雲端服務與即時服務狀態。
- 服務暫時中斷時會自動恢復，減少人工處理。
- 遠端服務可由使用者隨時開啟或關閉。
- 關閉遠端服務後，仍保留基本服務狀態。
- 支援繁體中文與英文介面。

如需協助，請聯絡 REXLiTE AI 客戶服務。為保障帳號與服務安全，請勿在公開區域張貼啟用資料、服務位置或系統紀錄。
