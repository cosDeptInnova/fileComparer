@echo off
setlocal
cd /d %~dp0\..
if "%COMPARE_WINDOWS_WORKER_MODE%"=="" set COMPARE_WINDOWS_WORKER_MODE=production
if "%COMPARE_QUEUE_NAME%"=="" set COMPARE_QUEUE_NAME=compare
set "PYTHONPATH=%CD%;%PYTHONPATH%"
echo [comp_docs_worker] Windows detected. DO NOT use 'rq worker' here; launching python -m app.worker --queue %COMPARE_QUEUE_NAME%
python -m app.worker --queue %COMPARE_QUEUE_NAME% %*