# A2A ↔ dsh 橋接參考手冊

本檔記錄把本機 DeepSeek Harness (dsh) 包成 A2A remote agent 的關鍵知識與疑難排解。
SKILL.md 只留流程；這裡放細節。踩過坑已實測驗證，勿重踩。

## 1. dsh 呼叫方式（已實測可用）

- 正確形態：`node <dsh>/lib/bin.js --profile headless "<task>"`（官方 headless 一次性運行器）。
  - 跑完即退、不佔 port；stdout 即 agent 最終回答；退出碼 0=完成。
- **不要走** Web UI (3080) 的 HTTP API；**不要用** dsh Python SDK（官方限 Linux/macOS，Windows 不支援）。
- **繞過 npx**：`npx @deepseek-ai/dsh` 在受限 shell 會卡住（去連 npm registry 做版本檢查）。直接用 node 跑 bin.js。
- 建議安裝新版 dsh（≥0.2.0，相容 credentials 的 `version: 1` 數字格式），以 `DSH_BIN_JS` 指向它的 `lib/bin.js`。
  - server.py 未指定 `DSH_BIN_JS` 時，會依序嘗試全域 `dsh` → `npx @deepseek-ai/dsh`。

## 2. dsh 的 API key / credentials

- key 不用外部提供：dsh 自讀 `~/.dsh/.credentials.yaml`（家目錄）。**不需要設 `DEEPSEEK_API_KEY`**。
- 版本格式坑（重要）：`.credentials.yaml` 的 `version` 必須是**數字 `1`**（新版 dsh 才接受）。
  - npx 快取的舊版 dsh（v0.1.0-rc.6）要求 `version` 是字串 → 讀不了桌面 App 寫的檔。
  - 結論：用與桌面 App 同代的新版 dsh（0.2.0-rc.2）即可，別改 credentials 檔。

## 3. Windows .cmd/.bat 啟動坑

- Windows 的 CreateProcess 不能直接執行 `.cmd/.bat`；Python `subprocess.run([...])` 預設不走 shell 會 `FileNotFoundError`。
- server.py 的 `_run_dsh()` 已處理：偵測 `.cmd/.bat/.ps1` 或 `npx` 時自動 `shell=True`（傳 list 時 Python 用 list2cmdline 加引號，防 `& | < >` 注入）。
- 自建測試 `.bat` 必須用 **CRLF** 換行，否則 cmd 吃掉每行第一字。

## 4. envelope 結構化格式

A2A message 的 `data` part 可帶這個 envelope（也相容「text part 是 JSON 且含 action」）：

```json
{
  "task_id": "uuid-123",
  "from": "wordbuddy",
  "to": "dsh",
  "action": "translate",
  "payload": { "text": "hello world", "target_lang": "zh-TW" },
  "reply_to": "wordbuddy.callback",
  "timeout_ms": 60000
}
```

- 內建 action 路由（server.py `ACTION_HANDLERS`）：
  - `translate`：payload `{text, target_lang}`
  - `code`：payload `{language, task}`
  - `chat` / `general`：payload `{text}`（或 prompt/task）
  - 未知 action → 通用提示（把 action+payload 描述給 dsh）
- `timeout_ms` → dsh 有效逾時 = `min(DSH_TIMEOUT, timeout_ms/1000)`。
- `reply_to` → 存進 task.metadata，client 回結構化結果時回填。

## 5. 協議方法（JSON-RPC 2.0 over HTTP，POST /a2a）

- `tasks/send`：非同步派任務，立刻回 `submitted`；dsh 背景執行。
- `tasks/get`：查任務狀態與 artifact（`{"id": task_id}`）。
- `tasks/sendSubscribe`：SSE 串流派任務，一邊推送 task 快照直到 `completed/failed/canceled`。
- Agent Card：`GET /.well-known/agent.json`；健康檢查：`GET /health`（跑 `dsh --version`，零成本）。

## 6. 任務狀態生命週期

`submitted → working → completed / failed / canceled`

## 7. 生產化特性

- **持久化**：任務狀態落地 `.a2a_tasks.json`（server.py 同目錄，原子寫入）；重啟後 `tasks/get` 仍查得到舊結果。
- **失敗重試**：`DSH_RETRIES`（預設 2）次，退避 `min(2^attempt,10)` 秒；結果附 `[重試第 N/M 次仍失敗]`。
- **SSE**：長任務用 `tasks/sendSubscribe` 或 client `--stream`，不必輪詢。

## 8. 環境變數一覽

| 變數 | 預設 | 說明 |
|---|---|---|
| `DSH_BIN_JS` | 自動找 | dsh CLI 進入點 lib/bin.js（最穩） |
| `DSH_BIN` | — | 兼容舊用法：`dsh`/`npx`/完整命令 |
| `DSH_WORKSPACE` | server.py 所在目錄 | dsh 工作目錄 |
| `DSH_ISOLATE` | `0` | `1`=每任務獨立子目錄 |
| `DSH_TIMEOUT` | `1800` | 單次任務秒數上限 |
| `DSH_RETRIES` | `2` | dsh 失敗重試次數 |

## 9. 常見故障排除

- **client 連不到 / 抓不到 Agent Card**：server 起了嗎？確認 `http://127.0.0.1:8080/health`。
- **`tasks/send` 60 秒逾時**：確認用非同步（新版 server 已預設非同步）；長任務改 `--stream`。
- **dsh 報 credentials version 錯誤**：用的是舊版 rc.6；改用較新的 dsh（≥0.2.0）bin.js。
- **dsh 回「網路搜尋工具 API 鑑權失敗」**：headless 未帶搜尋 key；要即時搜尋需另解 dsh 的 web search 鑑權。
- **dsh 回 PowerShell 宿主亂碼 / exit 0xFFFF0000**：dsh 桌面端宿主問題，非橋接問題；答案通常仍正確（手動追蹤）。

## 10. 一鍵手動跑（使用者）

1. 編輯 `scripts/start_server.bat` 的 `DSH_WORKSPACE`（要動的專案目錄）。
2. 雙擊 `start_server.bat` 啟動（埠 8080）。
3. 另開終端機：`python client.py "任務文字"` 或 `python client.py --envelope-file env.json`。
