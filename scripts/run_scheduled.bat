@echo off
rem Windows Task Scheduler entrypoint for the maintenance pipeline.
rem
rem Task Scheduler does not set a working directory, so the repository root is
rem resolved from this script's own location (%~dp0) rather than from the
rem caller.  The wrapper only runs the pipeline; decisions and recipe dispatch
rem stay behind the decision CLI and a human, never in an unattended job.
rem
rem Usage:  run_scheduled.bat <input_file_or_downloads_dir> <output_root> [log_path]

setlocal

set "REPO_ROOT=%~dp0.."
set "INPUT_PATH=%~1"
set "OUTPUT_ROOT=%~2"
set "LOG_PATH=%~3"

if "%INPUT_PATH%"=="" (
    echo run_scheduled: input path is required 1>&2
    exit /b 2
)
if "%OUTPUT_ROOT%"=="" (
    echo run_scheduled: output root is required 1>&2
    exit /b 2
)
if "%LOG_PATH%"=="" set "LOG_PATH=%OUTPUT_ROOT%\scheduled.log"

if not exist "%INPUT_PATH%" (
    echo run_scheduled: input path not found 1>&2
    exit /b 2
)
if not exist "%REPO_ROOT%\run_pipeline.py" (
    echo run_scheduled: repository root does not contain run_pipeline.py 1>&2
    exit /b 2
)
if not exist "%OUTPUT_ROOT%" mkdir "%OUTPUT_ROOT%"

rem A scheduled run is unattended: keep both streams in the log so a failure is
rem inspectable afterwards, and hand the pipeline's exit code back to the
rem scheduler instead of masking it.
echo [%DATE% %TIME%] input "%INPUT_PATH%" >> "%LOG_PATH%"
if exist "%INPUT_PATH%\." (
    if not exist "%REPO_ROOT%\inbox_runner.py" (
        echo run_scheduled: repository root does not contain inbox_runner.py 1>&2
        exit /b 2
    )
    python "%REPO_ROOT%\inbox_runner.py" --downloads "%INPUT_PATH%" --inbox "%OUTPUT_ROOT%\inbox" --output-root "%OUTPUT_ROOT%" --repository-root "%REPO_ROOT%" >> "%LOG_PATH%" 2>&1
) else (
    python "%REPO_ROOT%\run_pipeline.py" "%INPUT_PATH%" --output-root "%OUTPUT_ROOT%" >> "%LOG_PATH%" 2>&1
)
set "PIPELINE_EXIT=%ERRORLEVEL%"
echo [%DATE% %TIME%] exit=%PIPELINE_EXIT% >> "%LOG_PATH%"

endlocal & exit /b %PIPELINE_EXIT%
