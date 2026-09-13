@echo off
setlocal
if "%~1"=="" (
  echo HWPX 파일을 convert.bat 위로 끌어다 놓으세요.
  pause
  exit /b 1
)
py "%~dp0converter.py" "%~1"
if errorlevel 1 (
  echo.
  echo 변환에 실패했습니다.
  pause
  exit /b 1
)
echo.
echo 변환이 완료되었습니다.
pause
