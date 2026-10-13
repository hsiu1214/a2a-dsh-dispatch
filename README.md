# a2a-dsh-dispatch

用 **A2A 協議**（Google Agent2Agent，JSON-RPC 2.0 over HTTP）把本機的 **DeepSeek Harness (`dsh`)** 包成一個可被尋址的 remote agent，讓任何 A2A client 都能「派任務給 dsh、收回結果」。

`dsh` 本身不是 A2A server；這個專案加一層薄橋接：`server.py` 收 A2A 請求 → 呼叫本機 dsh headless → 把 dsh 的輸出包成 A2A artifact 回傳。

```
 [A2A client]   --HTTP JSON-RPC-->   [server.py]   --headless-->   [dsh]  --> DeepSeek API
  client.py 或                          (本橋接)                     (本機)      結果包成
  任意 A2A agent                                                               artifact 回傳
```

## 功能

- **A2A 標準三件套**：Agent Card（`/.well-known/agent.json`）、`tasks/send`、`tasks/get`。
- **非同步派發**：`tasks/send` 立刻回 `submitted`，dsh 在背景執行，長任務不會卡住 HTTP。
- **SSE 串流**：`tasks/sendSubscribe` 一邊推送任務狀態直到完成（client 用 `--stream`）。
- **結構化 envelope**：支援 `from/to/action/payload/reply_to/timeout_ms` 訊息格式，內建 `translate`/`code`/`chat`/`general` 動作路由。
- **生產化**：任務狀態落地 `.a2a_tasks.json`（重啟可恢復）、失敗自動重試、逾時控制。
- **零相依**：純 Python 標準庫，無需 pip 安裝任何套件。

## 需求

- Python 3.8+（僅用標準庫）
- Node.js（用來跑 dsh 的 `lib/bin.js`）
- 本機已安裝 **DeepSeek Harness (`dsh`)** 並完成登入（key 由 dsh 自讀 `~/.dsh/.credentials.yaml`，**不需要**另外設 `DEEPSEEK_API_KEY`）

## 安裝（作為 WorkBuddy / CodeBuddy skill）

把整個資料夾放到 skill 目錄：

```bash
# WorkBuddy
cp -r a2a-dsh-dispatch ~/.workbuddy/skills/
# CodeBuddy
cp -r a2a-dsh-dispatch ~/.codebuddy/skills/
```

之後對 agent 說「派任務給 dsh：…」即會觸發。

## 使用

### 啟動服務端

```bash
cd scripts
python server.py          # 監聽 0.0.0.0:8080
```

Windows 也可編輯 `scripts/start_server.bat` 後雙擊執行。

健康檢查（零成本、不連模型）：

```bash
curl http://127.0.0.1:8080/health
# → dsh_reachable / dsh_version / credentials_ok
```

### 派任務（另一個終端機）

```bash
# 純文字任務（非同步輪詢）
python client.py "把 hello world 翻成繁體中文"

# SSE 串流（長任務即時看進度）
python client.py --stream "寫一支讀取 CSV 的 Python 腳本"

# 結構化 envelope
python client.py --envelope-file env.json
# 或
python client.py --stream --envelope-file env.json
```

### envelope 格式

```json
{
  "task_id": "uuid-1",
  "from": "my-agent",
  "to": "dsh",
  "action": "translate",
  "payload": { "text": "good night", "target_lang": "zh-TW" },
  "reply_to": "my-agent.callback",
  "timeout_ms": 60000
}
```

內建 `action`：`translate`（`text`+`target_lang`）、`code`（`language`+`task`）、`chat`/`general`（`text`）；未知 action 走通用提示。

### 讓其他電腦的 agent 使用

`server.py` 綁定 `0.0.0.0`，同一區網的其他 agent 可把 client 指向 `http://<這台電腦IP>:8080` 直接呼叫（A2A 標準 client 亦可）。詳見 `references/dsh-notes.md` 與下方安全警告。

## 設定（環境變數）

| 變數 | 預設 | 說明 |
|---|---|---|
| `DSH_BIN_JS` | — | dsh CLI 進入點 `lib/bin.js`（最穩，node 直接跑，避開 npx 延遲） |
| `DSH_BIN` | — | 相容舊用法：`dsh` / `npx` / 完整命令 |
| `DSH_WORKSPACE` | server.py 所在目錄 | dsh 的工作目錄 |
| `DSH_ISOLATE` | `0` | `1`=每任務用獨立子目錄 |
| `DSH_TIMEOUT` | `1800` | 單次任務秒數上限 |
| `DSH_RETRIES` | `2` | dsh 失敗重試次數 |

## 檔案

```
a2a-dsh-dispatch/
├── SKILL.md                 # skill 觸發條件與操作流程
├── scripts/
│   ├── server.py            # A2A 服務端（橋接 dsh）
│   ├── client.py            # A2A 客戶端（--stream / --envelope-file）
│   ├── agent_card.json      # A2A v1.0 Agent Card
│   └── start_server.bat     # Windows 一鍵啟動
└── references/
    └── dsh-notes.md         # dsh 呼叫細節、疑難排解、envelope 格式
```

## 安全警告（重要）

- `server.py` **沒有任何認證**。一旦綁定 `0.0.0.0` 並對外暴露，任何連得上的人都能派任務、**耗用你的 DeepSeek API 額度**。
- 對外（區網以外）使用前，請自行加上 **bearer token 認證**，並建議透過 **HTTPS 反向代理**（如 Caddy/nginx）暴露，避免 token 明文傳輸。
- 本repo 不含任何金鑰；dsh 的憑證由 dsh 自管於 `~/.dsh/.credentials.yaml`。

## 授權

[MIT](LICENSE)
