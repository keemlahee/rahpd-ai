@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    echo [1/4] Python 3.12 설치 중...
    winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
) else (
    echo [1/4] Python 확인됨
)
if not exist "%PY%" (
    echo Python 설치에 실패했습니다. https://www.python.org 에서 3.12를 설치한 뒤 다시 실행해 주세요.
    pause
    exit /b 1
)

set "HAS_FFMPEG="
where ffmpeg >nul 2>nul && set "HAS_FFMPEG=1"
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" set "HAS_FFMPEG=1"
if defined HAS_FFMPEG (
    echo [2/4] ffmpeg 확인됨
) else (
    echo [2/4] ffmpeg 설치 중...
    winget install --id Gyan.FFmpeg -e --silent --accept-package-agreements --accept-source-agreements
)

if not exist "%WINDIR%\System32\msvcp140.dll" (
    echo [3/4] Visual C++ 런타임 설치 중... 관리자 권한 창이 뜨면 '예'를 눌러 주세요.
    winget install --id Microsoft.VCRedist.2015+.x64 -e --silent --accept-package-agreements --accept-source-agreements
) else (
    echo [3/4] Visual C++ 런타임 확인됨
)

echo [4/4] 파이썬 패키지 설치 중... 몇 분 걸립니다.
if not exist ".venv\Scripts\python.exe" "%PY%" -m venv .venv
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo 패키지 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행해 주세요.
    pause
    exit /b 1
)
echo.
echo 설치가 끝났습니다. start.bat을 실행하세요.
pause
