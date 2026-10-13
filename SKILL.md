---
name: a2a-dsh-dispatch
description: 透過 A2A 協議（JSON-RPC 2.0 over HTTP）把本機 DeepSeek Harness (dsh) 包成 remote agent，並由 WorkBuddy 派發任務、收回 artifact 結果。當使用者說「派任務給 dsh」「A2A 派發」「deepseek harness」「dsh headless」「用 A2A 傳給 dsh」「envelope 派任務」，或需要把翻譯/編碼等任務轉發給本機 dsh 並取回結果時使用。內含已實測的 server.py/client.py、envelope 結構化格式、非同步+SSE+持久化+重試，以及 dsh 呼叫疑難排解。
agent_created: true
---

# A2A ↔ DeepSeek Harness (dsh) 派發橋接

## Overview

把本機 dsh 包成 A2A remote agent，讓 WorkBuddy（或任何 A2A client）用標準協議派任務給 dsh 並收回結果。
dsh 本身不原生支援 A2A，故加一層薄橋接：`server.py` 收 A2A 請求 → 呼叫本機 dsh headless → 把 stdout 包成 artifact 回傳。
腳本已實測可跑（翻譯、quicksort 編碼、SSE 串流、持久化、重試皆驗證通過）。

## 關鍵前提（先讀，避免白工）

1. **dsh 在「本機/host」，不在沙箱**。我的 Bash 沙箱連不到使用者桌面的 dsh、也沒有 credentials。派發務必在 **host** 執行（Bash 工具加 `dangerouslyDisableSandbox: true`）。
2. **API key 不用外部提供**：dsh 自讀 `~/.dsh/.credentials.yaml`。不要設 `DEEPSEEK_API_KEY`。
3. **繞過 npx**：`npx @deepseek-ai/dsh` 在受限 shell 會卡住。server.py 可用 node 直接跑新版 dsh 的 `lib/bin.js`（用 `DSH_BIN_JS` 指定路徑最穩）。
4. **server 與 client 必須在同一支 Bash 呼叫裡跑**：背景啟動的 server 會在該次工具呼叫結束時被殺。用 `server.py & ... client.py ... kill` 的寫法。
5. **client 輸出防丟**：用 `python -u client.py ... > .log 2>&1` 再 `cat .log`，避免被 SIGTERM 時緩衝輸出全丟。

## 快速開始：派一個任務給 dsh

以下 `<skill>` = 本 skill 目錄（`~/.workbuddy/skills/a2a-dsh-dispatch`）。腳本在 `<skill>/scripts/`。

在 host（`dangerouslyDisableSandbox: true`）執行：

```bash
PYC="python"
cd "<skill>/scripts"
"$PYC" server.py > .server.log 2>&1 &
SRV=$!
sleep 3
"$PYC" -u client.py "你的任務文字" http://127.0.0.1:8080 > .client.log 2>&1
kill $SRV 2>/dev/null
cat .client.log
```

- `client.py` 預設非同步：派發→輪詢到 `completed`→印 artifact。
- 長任務加 `--stream`（SSE 串流，即時看進度）：`client.py --stream "任務"`。
- 用完刪掉 `.server.log`/`.client.log` 與 `<skill>/scripts/.a2a_tasks.json`（測試資料不留）。

## 健康檢查（零成本、不連模型）

server 起來後開 `http://127.0.0.1:8080/health`：看 `dsh_reachable`、`dsh_version`、`credentials_ok`。
抓 Agent Card：`GET /.well-known/agent.json`。

## 結構化 envelope 派任務

要帶 `action`/`payload`/`reply_to`/`timeout_ms` 時，寫一份 JSON 餵給 `--envelope-file`：

```json
{ "task_id":"uuid-1","from":"wordbuddy","to":"dsh","action":"translate",
  "payload":{"text":"good night","target_lang":"zh-TW"},
  "reply_to":"wordbuddy.callback","timeout_ms":60000 }
```

```bash
"$PYC" -u client.py --envelope-file env.json http://127.0.0.1:8080    # 加 --stream 走 SSE
```

內建 action：`translate`、`code`、`chat`/`general`；未知 action 走通用提示。完整格式見 `references/dsh-notes.md` 第 4 節。

## 生產化特性（已內建）

- 持久化：任務狀態落地 `<skill>/scripts/.a2a_tasks.json`，server 重啟後 `tasks/get` 仍查得到。
- 失敗重試：`DSH_RETRIES`（預設 2）次、退避重試。
- SSE：`tasks/sendSubscribe` / client `--stream`。

## Resources

### scripts/
- `server.py` — A2A 服務端（橋接 dsh）。非同步派發、envelope 路由、SSE、持久化、重試、/health。
- `client.py` — A2A 客戶端。`--stream`（SSE）、`--envelope`/`--envelope-file`（結構化派發+結構化回覆）。
- `agent_card.json` — A2A v1.0 Agent Card（server.py 同目錄載入）。
- `start_server.bat` — 使用者一鍵啟動（位置獨立，用 `%~dp0`）；供手動跑，非 WorkBuddy 派發用。

### references/
- `dsh-notes.md` — dsh 呼叫方式、credentials 版本坑、Windows .cmd 坑、envelope 格式、協議方法、環境變數、故障排除。派發遇到問題時讀它。
