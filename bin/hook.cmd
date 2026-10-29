@echo off
rem Harness hook launcher. Resolves the plugin root from this script so it
rem works from any working directory.
rem
rem Hooks run on every tool call, so the launcher avoids extra processes: it
rem uses the interpreter recorded at install time (bin\python.path) and only
rem falls back to a PATH search (`where`, then the py launcher) when that
rem interpreter is gone. HARNESS_PYTHON overrides both.
setlocal
set "HARNESS_ROOT=%~dp0.."
set "HARNESS_RUNNER=%HARNESS_ROOT%\scripts\hook_runner.py"
if not defined HARNESS_PYTHON if exist "%~dp0python.path" set /p HARNESS_PYTHON=<"%~dp0python.path"
if not defined HARNESS_PYTHON goto :find_python
if not exist "%HARNESS_PYTHON%" goto :find_python
"%HARNESS_PYTHON%" -X utf8 "%HARNESS_RUNNER%" %*
exit /b %ERRORLEVEL%
:find_python
where py >nul 2>nul
if errorlevel 1 goto :use_python
py -3 -X utf8 "%HARNESS_RUNNER%" %*
exit /b %ERRORLEVEL%
:use_python
python -X utf8 "%HARNESS_RUNNER%" %*
exit /b %ERRORLEVEL%
