@echo off
setlocal
py -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo 설치에 실패했습니다. Python 3.10 이상이 설치되어 있는지 확인하세요.
  pause
  exit /b 1
)
echo.
echo 설치가 완료되었습니다.
pause
