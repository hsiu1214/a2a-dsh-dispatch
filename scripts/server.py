#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A2A 服務端：把 DeepSeek Harness (dsh) 包裝成 A2A 可尋址的 remote agent。

協議：JSON-RPC 2.0 over HTTP  (POST /a2a)
Agent Card：GET /.well-known/agent.json
依賴：Python 標準函式庫 (http.server, json, subprocess, uuid, threading)

啟動（dsh 的 key 已存在 ~/.dsh/.credentials.yaml，無需再設環境變數）：
    python server.py
然後另開終端機跑 client.py 派任務。

dsh 呼叫方式：用官方 headless 一次性運行器（由 node 執行 dsh 的 bin.js）
    node <dsh>/lib/bin.js --profile headless "<task>"
它跑完即退、不佔用 port，stdout 即 agent 最終回答，退出碼 0=完成。
（Python SDK 不支援 Windows，故用 headless CLI + subprocess；這也是官方推薦的 CI/腳本做法。）

可用環境變數：
    DSH_BIN_JS    指向 dsh 的 CLI 進入點 lib/bin.js（最穩，可由 node 直接跑，繞過會卡住的 npx）。
                  不設則依序嘗試：全域 dsh → npx @deepseek-ai/dsh。
    DSH_BIN       兼容舊用法：可設 "dsh" / "npx" / 或完整命令（如 "C:/path/dsh.exe"）。
    DSH_WORKSPACE dsh 的工作目錄（預設 server.py 所在目錄）；dsh 會在此目錄內自主執行
    DSH_ISOLATE   設 "1" 時，每個任務用 DSH_WORKSPACE/<task_id>/ 獨立子目錄，避免並行互相污染
    DSH_TIMEOUT   單次任務秒數上限，預設 1800
    DSH_RETRIES   dsh 失敗時的自動重試次數，預設 2（搭配簡易退避）

健康檢查（不連模型、零成本）：
    GET http://127.0.0.1:8080/health  → 回報 dsh 是否可執行、版本、credentials 是否存在

協議方法：
    tasks/send          非同步派任務：立刻回 submitted/working，dsh 背景執行
    tasks/get           查任務狀態與 artifact
    tasks/sendSubscribe SSE 串流派任務：一邊送出一邊推送狀態更新直到 completed/failed

生產化特性：
    - 任務狀態落地到 .a2a_tasks.json（重啟可恢復，用 tasks/get 仍查得到舊結果）
    - dsh 失敗自動重試（DSH_RETRIES 次）
    - 支援 SSE 串流（client 用 --stream 觸發），長任務不必一直輪詢
"""
import json
import os
import sys
import time
import shutil
import http.server
import socketserver
import subprocess
import threading
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8080

# ---- dsh 相關設定（可用環境變數覆寫）----
# 優先用 DSH_BIN_JS 指向 dsh 的 CLI 進入點 (lib/bin.js)，由 node 執行。
# 這能繞過會卡住的 npx，並相容桌面 App 的 credentials 格式（version: 1 數字）。
# dsh 會自動讀取使用者家目錄下的 ~/.dsh/.credentials.yaml，不需要另設 DEEPSEEK_API_KEY。
# 若連 DSH_BIN_JS 都沒設，會依序嘗試全域 dsh → npx @deepseek-ai/dsh。
DSH_BIN_JS = os.environ.get("DSH_BIN_JS")


def _detect_node():
    n = shutil.which("node")
    if n:
        return n
    return "node"


_raw_bin = os.environ.get("DSH_BIN")
if DSH_BIN_JS:
    NODE = _detect_node()
    DSH_CMD = [NODE, DSH_BIN_JS]
elif _raw_bin == "npx":
    DSH_CMD = ["npx", "@deepseek-ai/dsh"]
elif _raw_bin == "dsh":
    DSH_CMD = ["dsh"]
elif _raw_bin:
    DSH_CMD = _raw_bin.split()
elif shutil.which("dsh") is not None:
    DSH_CMD = ["dsh"]
else:
    # 未指定 DSH_BIN_JS 時退回 npx。
    # 建議設 DSH_BIN_JS 指向本機 dsh 的 lib/bin.js（可由 node 直接跑，避開 npx 的 registry 連線延遲）。
    DSH_CMD = ["npx", "@deepseek-ai/dsh"]

DSH_BIN_NAME = os.path.basename(DSH_CMD[-1]) or DSH_CMD[0]
DSH_WORKSPACE = os.environ.get("DSH_WORKSPACE", BASE_DIR)
DSH_ISOLATE = os.environ.get("DSH_ISOLATE", "0") == "1"
DSH_TIMEOUT = int(os.environ.get("DSH_TIMEOUT", "1800"))
DSH_RETRIES = int(os.environ.get("DSH_RETRIES", "2"))


def _credentials_file():
    """dsh 存放 key 的檔案位置（家目錄 ~/.dsh/.credentials.yaml）。"""
    for var in ("USERPROFILE", "HOME"):
        base = os.environ.get(var)
        if base:
            p = os.path.join(base, ".dsh", ".credentials.yaml")
            if os.path.exists(p):
                return p
    return None


# ---- 結構化 envelope 支援（from/to/action/payload/reply_to/timeout_ms）----
# A2A 訊息可用 data part 帶這個 envelope；也相容「text part 是 JSON 且含 action」的寫法。
# 解析後把 action + payload 組成一句給 dsh 的 prompt，並帶入 reply_to / timeout_ms。

def _try_parse_json(s):
    s = (s or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def extract_envelope(message):
    """從 A2A message 取出 envelope（dict）。優先 data part，退化解析 text part。"""
    for p in (message or {}).get("parts", []):
        if p.get("type") == "data" and isinstance(p.get("data"), dict):
            if "action" in p["data"] or "payload" in p["data"]:
                return p["data"]
    raw = "".join(
        p.get("text", "") for p in (message or {}).get("parts", [])
        if p.get("type") == "text"
    )
    env = _try_parse_json(raw)
    if isinstance(env, dict) and ("action" in env or "payload" in env):
        return env
    return None


def _translate_prompt(payload):
    text = payload.get("text", "")
    lang = payload.get("target_lang") or payload.get("lang") or "繁體中文 (zh-TW)"
    return f"請把 '{text}' 翻譯成{lang}，只輸出翻譯結果，不要多餘說明或引號"


def _code_prompt(payload):
    lang = payload.get("language") or "Python"
    spec = payload.get("task") or payload.get("text") or json.dumps(payload, ensure_ascii=False)
    return f"請用{lang}完成以下任務，直接給出完整可執行的程式碼與簡短說明：\n{spec}"


def _general_prompt(payload):
    return payload.get("text") or payload.get("prompt") or payload.get("task") \
        or json.dumps(payload, ensure_ascii=False)


ACTION_HANDLERS = {
    "translate": _translate_prompt,
    "code": _code_prompt,
    "chat": _general_prompt,
    "general": _general_prompt,
}


def build_from_envelope(env):
    """把 envelope 轉成 (prompt, reply_to, timeout_ms)。"""
    action = str(env.get("action") or "general").strip().lower()
    payload = env.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"text": str(payload)}
    reply_to = env.get("reply_to")
    timeout_ms = env.get("timeout_ms")
    handler = ACTION_HANDLERS.get(action)
    if handler:
        prompt = handler(payload)
    else:
        prompt = (
            f"動作：{action}\n參數：{json.dumps(payload, ensure_ascii=False)}\n"
            "請依上述內容執行，並回傳結果。"
        )
    return prompt, reply_to, timeout_ms


def _run_dsh(cmd, **kwargs):
    """呼叫 dsh 指令，處理 Windows 上 npx/dsh 是 .cmd 批次檔的坑。

    Windows 的 CreateProcess 不能直接執行 .cmd/.bat，必須走 shell (cmd /c)。
    傳入 list 時 Python 會用 list2cmdline 正確加引號，
    因此任務文字中的 & | < > 等不會被當成指令分隔（注入防護）。
    """
    use_shell = False
    if sys.platform.startswith("win"):
        resolved = shutil.which(cmd[0]) or cmd[0]
        ext = os.path.splitext(resolved)[1].lower()
        if ext in (".cmd", ".bat", ".ps1") or cmd[0] == "npx":
            use_shell = True
    return subprocess.run(cmd, shell=use_shell, **kwargs)

# 從 agent_card.json 載入 Agent Card（單一真相來源）
with open(os.path.join(BASE_DIR, "agent_card.json"), "r", encoding="utf-8") as f:
    AGENT_CARD = json.load(f)

# 任務存放：記憶體鏡像 + 落地持久化（JSON 檔，重啟可恢復）
TASKS_FILE = os.path.join(BASE_DIR, ".a2a_tasks.json")
TASKS = {}
TASKS_LOCK = threading.Lock()


def _load_tasks():
    """啟動時從硬碟載入既有任務（重啟後 tasks/get 仍查得到舊結果）。"""
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _persist_tasks_locked():
    """把 TASKS 原子寫入硬碟（呼叫端須已持有 TASKS_LOCK）。"""
    tmp = TASKS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(TASKS, f, ensure_ascii=False)
        os.replace(tmp, TASKS_FILE)
    except OSError:
        pass


TASKS = _load_tasks()


def delegate_to_deepseekharness(task_text: str, task_id: str, timeout_ms=None):
    """呼叫 dsh headless 執行一次性任務，回傳 (success: bool, output: str)。

    推薦方式：dsh --profile headless "<task>"
      - 跑完即退、不佔用 port，最適合被外部排程 / bridge 呼叫
      - stdout 即 agent 最終回答文字
      - 退出碼 0=完成，非 0=失敗
    dsh 會自讀 ~/.dsh/.credentials.yaml，不需要另設 DEEPSEEK_API_KEY。
    timeout_ms：呼叫端要求的截止時間（毫秒）；有效逾時 = min(DSH_TIMEOUT, timeout_ms/1000)。
    """
    effective_timeout = DSH_TIMEOUT
    if timeout_ms:
        try:
            effective_timeout = min(DSH_TIMEOUT, max(1, int(timeout_ms) // 1000))
        except (ValueError, TypeError):
            pass
    cmd = DSH_CMD + ["--profile", "headless", task_text]

    if DSH_ISOLATE:
        workdir = os.path.join(DSH_WORKSPACE, task_id)
    else:
        workdir = DSH_WORKSPACE
    os.makedirs(workdir, exist_ok=True)

    try:
        proc = _run_dsh(
            cmd, capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=effective_timeout, cwd=workdir,
        )
    except FileNotFoundError:
        return False, f"(找不到 {DSH_BIN_NAME} 命令，請確認 dsh 已安裝且在 PATH 中)"
    except subprocess.TimeoutExpired:
        return False, f"(dsh 執行超時 >{effective_timeout}s)"

    out = proc.stdout or "(dsh 無輸出)"
    if proc.returncode != 0:
        out += f"\n[dsh 退出碼 {proc.returncode}]\n{proc.stderr}"
        return False, out
    return True, out


def dsh_healthcheck():
    """安全健康檢查：只跑 dsh --version（不連模型、零成本），確認可執行檔找得到。
    並回報 dsh 的 credentials 檔（~/.dsh/.credentials.yaml）是否存在——
    dsh 用這份檔連模型，不需要另設 DEEPSEEK_API_KEY 環境變數。
    """
    cmd = DSH_CMD + ["--version"]
    try:
        proc = _run_dsh(cmd, capture_output=True,
                        encoding="utf-8", errors="replace", timeout=60)
        version_out = (proc.stdout or proc.stderr or "").strip().replace("\n", " ")
        reachable = proc.returncode == 0
    except FileNotFoundError:
        version_out = f"找不到 {DSH_BIN_NAME}"
        reachable = False
    except subprocess.TimeoutExpired:
        version_out = "版本查詢超時"
        reachable = False
    cred = _credentials_file()
    return {
        "dsh_cmd": " ".join(DSH_CMD),
        "dsh_reachable": reachable,
        "dsh_version": version_out[:200] if reachable else version_out,
        "credentials_file": cred or "(未找到 ~/.dsh/.credentials.yaml)",
        "credentials_ok": cred is not None,
        "workspace": DSH_WORKSPACE,
        "isolate": DSH_ISOLATE,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 關掉雜訊

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/.well-known/agent.json":
            self._send_json(200, AGENT_CARD)
        elif self.path.rstrip("/") == "/health":
            self._send_json(200, dsh_healthcheck())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/a2a":
            self._send_json(200, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32601, "message": "method not found"}
            })
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(200, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "parse error"}
            })
            return
        # SSE 串流方法在一般 JSON 回應前攔截
        if req.get("method") == "tasks/sendSubscribe":
            params = req.get("params", {}) or {}
            self.handle_send_subscribe(params, req.get("id"))
            return
        self.handle_rpc(req)

    def handle_rpc(self, req):
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params", {}) or {}
        if method == "tasks/send":
            self.handle_tasks_send(params, rid)
        elif method == "tasks/get":
            self.handle_tasks_get(params, rid)
        else:
            self._send_json(200, {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"unsupported method: {method}"}
            })

    def _prepare_task(self, params):
        """從 params 建立任務、持久化、啟動背景 dsh 執行。回傳 task dict。"""
        task_in = params.get("task", {})
        task_id = task_in.get("id") or f"task-{uuid.uuid4().hex[:8]}"
        message = task_in.get("message", {})

        envelope = extract_envelope(message)
        if envelope is not None:
            text, reply_to, timeout_ms = build_from_envelope(envelope)
        else:
            text = "".join(
                p.get("text", "")
                for p in message.get("parts", [])
                if p.get("type") == "text"
            )
            reply_to, timeout_ms = None, None

        # 非同步：先建立任務並立刻回傳（狀態 submitted），dsh 在背景執行。
        # 這樣 client 不會因為長任務（搜尋/編碼）導致 HTTP 逾時。
        with TASKS_LOCK:
            task = {
                "id": task_id,
                "status": {"state": "submitted"},
                "artifacts": [],
                "history": [message],
            }
            if reply_to:
                task["metadata"] = {"reply_to": reply_to, "envelope": envelope}
            TASKS[task_id] = task
            _persist_tasks_locked()
        threading.Thread(
            target=self._run_delegate,
            args=(task_id, text, reply_to, timeout_ms),
            daemon=True,
        ).start()
        return task

    def handle_tasks_send(self, params, rid):
        task = self._prepare_task(params)
        self._send_json(200, {"jsonrpc": "2.0", "id": rid, "result": {"task": task}})

    def handle_send_subscribe(self, params, rid):
        """tasks/sendSubscribe：建立任務後以 SSE 串流推送狀態更新，直到任務結束。"""
        task = self._prepare_task(params)
        task_id = task["id"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_state = None
        try:
            while True:
                with TASKS_LOCK:
                    cur = TASKS.get(task_id)
                    snapshot = json.loads(json.dumps(cur)) if cur else None
                if snapshot is not None:
                    state = snapshot.get("status", {}).get("state")
                    if state != last_state:
                        self._sse_send({"jsonrpc": "2.0", "id": rid,
                                        "result": {"task": snapshot}})
                        last_state = state
                    if state in ("completed", "failed", "canceled"):
                        break
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client 中途斷線，結束串流

    def _sse_send(self, payload):
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _run_delegate(self, task_id, text, reply_to=None, timeout_ms=None):
        """背景執行 dsh（含失敗重試），並把結果寫回任務狀態。"""
        with TASKS_LOCK:
            if task_id not in TASKS:
                return
            TASKS[task_id]["status"] = {"state": "working"}
            _persist_tasks_locked()

        ok, result = delegate_to_deepseekharness(text, task_id, timeout_ms=timeout_ms)
        attempt = 0
        while not ok and attempt < DSH_RETRIES:
            attempt += 1
            time.sleep(min(2 ** attempt, 10))  # 簡易退避：2s, 4s, ... 上限 10s
            ok, result = delegate_to_deepseekharness(text, task_id, timeout_ms=timeout_ms)
            if not ok:
                result += f"\n[重試第 {attempt}/{DSH_RETRIES} 次仍失敗]"

        state = "completed" if ok else "failed"
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            if task is None:
                return
            task["status"] = {"state": state}
            task["artifacts"] = [
                {
                    "artifactId": f"art-{uuid.uuid4().hex[:8]}",
                    "name": "dsh_result",
                    "parts": [{"type": "text", "text": result}],
                }
            ]
            _persist_tasks_locked()

    def handle_tasks_get(self, params, rid):
        task_id = params.get("id")
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        if not task:
            self._send_json(200, {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32001, "message": "task not found"}
            })
            return
        self._send_json(200, {"jsonrpc": "2.0", "id": rid, "result": {"task": task}})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"A2A server (dsh bridge) 監聽於 http://127.0.0.1:{PORT}")
        print(f"Agent Card:            http://127.0.0.1:{PORT}/.well-known/agent.json")
        print("Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")


if __name__ == "__main__":
    main()
