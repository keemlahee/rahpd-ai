@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 먼저 install.bat을 실행해 주세요.
    pause
    exit /b 1
)

echo 유튜브 자동 편집을 시작합니다. 이 창을 닫으면 프로그램이 꺼집니다.
start "" "http://127.0.0.1:8766"
".venv\Scripts\python.exe" -m app
pause
