@echo off
setlocal
python -X utf8 "%~dp0..\scripts\harness_cli.py" %*
exit /b %ERRORLEVEL%
