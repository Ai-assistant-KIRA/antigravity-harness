@echo off
rem Harness² hook launcher. Resolves the plugin root from this script so it
rem works from any working directory, then prefers the Windows py launcher.
setlocal
set "HARNESS_ROOT=%~dp0.."
set "HARNESS_RUNNER=%HARNESS_ROOT%\scripts\hook_runner.py"

where py >nul 2>nul
if errorlevel 1 goto :use_python

py -3 -X utf8 "%HARNESS_RUNNER%" %*
exit /b %ERRORLEVEL%

:use_python
python -X utf8 "%HARNESS_RUNNER%" %*
exit /b %ERRORLEVEL%
