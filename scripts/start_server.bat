@echo off
REM ============================================================
REM  啟動 A2A 服務端（把本機 dsh 包成 A2A remote agent）
REM  位置獨立：用 %~dp0（本 bat 所在目錄）定位 server.py
REM  用法：雙擊本檔，或於 cmd 執行。前景執行，Ctrl+C 停止（埠 8080）。
REM  dsh 的 API key 由 dsh 自讀 ~/.dsh/.credentials.yaml，無需在本檔設定。
REM ============================================================
setlocal

REM 1) dsh 的工作目錄（要動的程式碼/專案目錄）。
REM    預設用目前目錄；改成你要讓 dsh 進去做事的專案路徑。
set "DSH_WORKSPACE=%CD%"

REM 2) 指向本機 dsh 的 CLI 進入點 lib/bin.js（由 node 執行，避開會卡住的 npx）。
REM    留空則 server 會依序嘗試全域 dsh → npx @deepseek-ai/dsh。
REM    例：set "DSH_BIN_JS=C:/path/to/@deepseek-ai/dsh/lib/bin.js"
set "DSH_BIN_JS="

REM 3) 失敗重試次數（預設 2）
set "DSH_RETRIES=2"

REM 4) 每個任務用獨立子目錄，避免並行互相污染
set "DSH_ISOLATE=1"

REM 啟動服務端。若 python 不在 PATH，改成你環境的 python 完整路徑。
python "%~dp0server.py"

endlocal
