@echo off
rem One operational entrypoint. It resolves the repository from this file,
rem so the caller's working directory is irrelevant.
setlocal
set "REPO_ROOT=%~dp0.."
where python >nul 2>&1
if errorlevel 1 (
    echo start: Python was not found in PATH 1>&2
    exit /b 2
)
python "%REPO_ROOT%\start.py" --repository-root "%REPO_ROOT%" %*
set "START_EXIT=%ERRORLEVEL%"
endlocal & exit /b %START_EXIT%
