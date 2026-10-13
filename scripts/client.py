#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A2A 客戶端：向 DeepSeek Harness Bridge 派發任務並取回結果。

用法：
    python client.py "重構 src/utils.py 的錯誤處理"
    python client.py "寫一支讀取 CSV 的 Python 腳本" http://127.0.0.1:8080
    python client.py --envelope-file env.json http://127.0.0.1:8080
    python client.py --stream "你的任務"                # 用 SSE 串流（tasks/sendSubscribe）
    python client.py --stream --envelope-file env.json  # envelope + SSE 串流

流程：抓 Agent Card → 派任務（tasks/send 或 SSE tasks/sendSubscribe）→ 收結果
依賴：Python 標準函式庫 (urllib, json, uuid, time, sys)
"""
import json
import sys
import time
import uuid
import urllib.request
import urllib.error

DEFAULT_SERVER = "http://127.0.0.1:8080"


def _post(endpoint, method, params, rid=None):
    payload = {
        "jsonrpc": "2.0",
        "id": rid or f"req-{uuid.uuid4().hex[:8]}",
        "method": method,
        "params": params,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_agent_card(base):
    with urllib.request.urlopen(base + "/.well-known/agent.json", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def endpoint_from_card(card):
    ifaces = card.get("supportedInterfaces") or []
    if ifaces:
        return ifaces[0]["url"]
    return card.get("url")  # 相容 v0.3


def send_task(endpoint, text):
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    params = {
        "task": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            },
        }
    }
    return task_id, _post(endpoint, "tasks/send", params)


def send_envelope(endpoint, envelope):
    """用結構化 envelope 派任務（放進 A2A message 的 data part）。

    envelope 例：
      {"task_id":"uuid-123","from":"wordbuddy","to":"dsh","action":"translate",
       "payload":{"text":"hello world","target_lang":"zh-TW"},
       "reply_to":"wordbuddy.callback","timeout_ms":60000}
    """
    task_id = envelope.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"
    params = {
        "task": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "data", "data": envelope}],
            },
        }
    }
    return task_id, _post(endpoint, "tasks/send", params)


def get_task(endpoint, task_id):
    return _post(endpoint, "tasks/get", {"id": task_id})


def wait_task(endpoint, task_id, attempts=360, interval=5):
    """輪詢 tasks/get 直到 completed/failed/canceled。

    由於 server 的 tasks/send 是非同步（立刻回 working），dsh 在背景跑，
    這裡負責等到任務結束。預設最多 360 次 × 5 秒 = 30 分鐘上限。
    """
    last_state = None
    for i in range(attempts):
        try:
            res = get_task(endpoint, task_id)
        except urllib.error.URLError:
            time.sleep(interval)
            continue
        if "result" in res:
            task = res["result"]["task"]
            state = task["status"]["state"]
            if state != last_state:
                print(f"  狀態：{state}（已等待 {i*interval}s）")
                last_state = state
            if state in ("completed", "failed", "canceled"):
                return task
        time.sleep(interval)
    return None


def _artifact_text(task):
    """把 task 的所有 text artifact 串成一段文字。"""
    chunks = []
    for art in task.get("artifacts", []):
        for part in art.get("parts", []):
            if part.get("type") == "text":
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _text_params(text):
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    return task_id, {"task": {"id": task_id, "message": {
        "role": "user", "parts": [{"type": "text", "text": text}]}}}


def _envelope_params(envelope):
    task_id = envelope.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"
    return task_id, {"task": {"id": task_id, "message": {
        "role": "user", "parts": [{"type": "data", "data": envelope}]}}}


def subscribe_task(endpoint, params):
    """tasks/sendSubscribe SSE 串流：yield 每個 task 快照事件（dict）。"""
    payload = {
        "jsonrpc": "2.0",
        "id": f"req-{uuid.uuid4().hex[:8]}",
        "method": "tasks/sendSubscribe",
        "params": params,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    resp = urllib.request.urlopen(req, timeout=None)
    data_buf = []
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data_buf:
                    payload_str = "\n".join(data_buf)
                    data_buf = []
                    try:
                        yield json.loads(payload_str)
                    except json.JSONDecodeError:
                        pass
            elif line.startswith("data:"):
                data_buf.append(line[5:].lstrip(" "))
    finally:
        resp.close()


def run_subscribe(endpoint, params):
    """以 SSE 串流跑任務，即時印狀態，回傳最終 task（失敗/斷線回 None）。"""
    last_state = None
    final = None
    for evt in subscribe_task(endpoint, params):
        if "error" in evt and "result" not in evt:
            print("串流錯誤:", evt["error"])
            return None
        task = (evt.get("result") or {}).get("task")
        if not task:
            continue
        state = task.get("status", {}).get("state")
        if state != last_state:
            print(f"  狀態：{state}")
            last_state = state
        final = task
        if state in ("completed", "failed", "canceled"):
            break
    return final


def main():
    args = sys.argv[1:]
    envelope = None
    envelope_file = None
    prompt = None
    base = DEFAULT_SERVER
    stream = False

    i = 0
    positionals = []
    while i < len(args):
        a = args[i]
        if a == "--envelope" and i + 1 < len(args):
            envelope = args[i + 1]; i += 2; continue
        if a.startswith("--envelope="):
            envelope = a.split("=", 1)[1]; i += 1; continue
        if a == "--envelope-file" and i + 1 < len(args):
            envelope_file = args[i + 1]; i += 2; continue
        if a.startswith("--envelope-file="):
            envelope_file = a.split("=", 1)[1]; i += 1; continue
        if a == "--stream":
            stream = True; i += 1; continue
        positionals.append(a); i += 1

    for p in positionals:
        if p.startswith("http://") or p.startswith("https://"):
            base = p
        else:
            prompt = p

    try:
        card = get_agent_card(base)
    except Exception as e:
        print(f"[錯誤] 抓不到 Agent Card（server 啟動了嗎？）：{e}")
        return

    print(f"Agent : {card['name']} v{card['version']}")
    print(f"Skills: {[s['id'] for s in card.get('skills', [])]}")
    endpoint = endpoint_from_card(card)
    print(f"端點  : {endpoint}  模式：{'SSE 串流' if stream else '輪詢'}")
    print("-" * 50)

    # 結構化 envelope 模式
    if envelope_file:
        with open(envelope_file, "r", encoding="utf-8") as f:
            envelope = f.read()
    if envelope is not None:
        try:
            env = json.loads(envelope)
        except json.JSONDecodeError as e:
            print("envelope JSON 解析失敗:", e)
            return
        started = time.time()
        if stream:
            task_id, params = _envelope_params(env)
            print(f"已訂閱 envelope 任務 {task_id}（action={env.get('action')}），SSE 串流中…")
            task = run_subscribe(endpoint, params)
        else:
            task_id, res = send_envelope(endpoint, env)
            if "error" in res:
                print("任務派發失敗:", res["error"])
                return
            print(f"已派發 envelope 任務 {task_id}（action={env.get('action')}），dsh 背景執行中…")
            task = wait_task(endpoint, task_id)
        if not task:
            print("未取得結果（逾時或串流中斷）。")
            return
        elapsed_ms = int((time.time() - started) * 1000)
        state = task["status"]["state"]
        reply = {
            "task_id": task_id,
            "from": "dsh",
            "to": env.get("from", "wordbuddy"),
            "action": env.get("action"),
            "reply_to": env.get("reply_to"),
            "status": state,
            "elapsed_ms": elapsed_ms,
            "result": _artifact_text(task),
        }
        print(f"狀態：{state}\n")
        print(json.dumps(reply, ensure_ascii=False, indent=2))
        return

    # 一般純文字模式
    prompt = prompt or "寫一支讀取 CSV 的 Python 腳本"
    started = time.time()
    if stream:
        task_id, params = _text_params(prompt)
        print(f"已訂閱任務 {task_id}，SSE 串流中…")
        task = run_subscribe(endpoint, params)
    else:
        task_id, res = send_task(endpoint, prompt)
        if "error" in res:
            print("任務派發失敗:", res["error"])
            return
        print(f"已派發任務 {task_id}，dsh 在背景執行中…")
        task = wait_task(endpoint, task_id)
    if not task:
        print("未取得結果（逾時或串流中斷）。")
        return

    print(f"狀態：{task['status']['state']}（{int((time.time()-started)*1000)} ms）\n")
    for art in task.get("artifacts", []):
        for part in art.get("parts", []):
            if part.get("type") == "text":
                print("=== dsh 回傳結果 ===\n")
                print(part["text"])


if __name__ == "__main__":
    main()
