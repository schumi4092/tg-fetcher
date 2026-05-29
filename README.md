# TG Fetcher Pro — Crypto 情報中心

Telegram 訊息擷取 + AI 總結 + 本地記憶系統，打造你的個人 Crypto 情報日記。

## 功能

- 🔐 **Telegram 帳號登入** — 擷取任何群組/頻道的歷史訊息
- 🤖 **AI 總結** — Claude 自動分析訊息重點、市場情緒、重要事件
- 🧠 **本地記憶** — SQLite 儲存每日摘要，資料永不遺失
- 📌 **事件標記** — AI 自動辨識重要事件，也可手動新增
- 📝 **個人筆記** — 隨時記錄你的想法、策略、觀察
- 📊 **每日報告** — AI 綜合當天所有資訊生成情報簡報
- 🔍 **記憶搜尋** — 搜尋歷史摘要、事件、筆記

## 快速開始

### 1. 安裝依賴

```bash
pip install -r requirements.txt
```

### 2. 設定 API Keys

```bash
# Telegram API（必要）— 從 https://my.telegram.org/apps 取得
export TG_API_ID="12345678"
export TG_API_HASH="abcdef1234567890"

# Claude API（AI 功能）— 從 https://console.anthropic.com 取得
export CLAUDE_API_KEY="sk-ant-api03-..."
```

建議放在專案根目錄的 `.env`，程式啟動時會自動讀取。

### 3. 啟動

```bash
cd tg-fetcher
python server.py
```

### 4. 使用

1. 瀏覽器開啟 http://127.0.0.1:5151
2. 在左側登入 Telegram（首次需要手機驗證碼）
3. 選擇聊天室 → 調整時間範圍 → 點「擷取訊息」
4. 點「🤖 AI 總結」讓 Claude 分析並存入記憶
5. 切換到「🧠 記憶」頁籤查看歷史記錄
6. 在「📝 筆記」頁籤隨時新增個人備註

## 檔案結構

```
tg-fetcher/
├── server.py           # Flask 後端（Telegram + Claude + SQLite）
├── static/
│   └── index.html      # 前端介面
├── tg_memory.db        # 本地記憶資料庫（自動建立）
├── tg_web_session.session  # Telegram 登入狀態（自動建立）
└── README.md
```

## 測試

```bash
python -m pytest
```

## API 費用估算

Claude API 按 token 計費：
- 每次總結約 1000-3000 tokens（約 $0.003-0.01）
- 每日報告約 2000-4000 tokens（約 $0.006-0.015）
- 事件提取約 500-1000 tokens（約 $0.002-0.003）
- **一天大量使用估計 < $0.10**

## 注意事項與安全

> ⚠️ **只在本機 localhost 執行，絕對不要暴露到公網。**
> 後端用 SameSite cookie + 自訂 header 擋跨站請求，但**這不是針對「能直接連到 port 的人」的驗證**：任何能存取 `/` 的人都會拿到 API token。一旦綁到 `0.0.0.0`、或經 ngrok／反向代理／port-forward 對外，等於把你的 Telegram 帳號與整個 `tg_memory.db` 交出去。要遠端使用請自行加上真正的登入驗證。

- 此工具僅供個人使用；使用者需自負 Telegram / GMGN / Twitter 等服務條款與當地法規（含他人訊息的隱私）之合規責任。
- 用個人帳號大量抓取／封存訊息可能違反 Telegram 服務條款，有帳號被限制的風險。
- `.env`（API keys）、`*.session`（= Telegram 帳號登入態）、`tg_memory.db`（你的摘要／筆記）都已被 `.gitignore` 排除——請勿提交或外流，尤其 `*.session` 等同帳號鑰匙。
- 授權：見 [LICENSE](LICENSE)（MIT）。
