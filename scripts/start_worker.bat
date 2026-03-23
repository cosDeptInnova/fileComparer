@echo off
setlocal
cd /d %~dp0\..
if "%COMPARE_QUEUE_NAME%"=="" set COMPARE_QUEUE_NAME=compare
set "PYTHONPATH=%CD%;%PYTHONPATH%"
echo [comp_docs_worker] launching Celery worker via python -m app.worker --queue %COMPARE_QUEUE_NAME%
python -m app.worker --queue %COMPARE_QUEUE_NAME% %*
