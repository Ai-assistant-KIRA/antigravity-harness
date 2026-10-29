@echo off
rem Harness CLI wrapper. Works from any working directory.
rem Uses the interpreter recorded at install time (bin\python.path), then the
rem py launcher, then python on PATH. HARNESS_PYTHON overrides all of them.
setlocal
set "HARNESS_CLI=%~dp0..\scripts\harness_cli.py"
if not defined HARNESS_PYTHON if exist "%~dp0python.path" set /p HARNESS_PYTHON=<"%~dp0python.path"
if not defined HARNESS_PYTHON goto :find_python
if not exist "%HARNESS_PYTHON%" goto :find_python
"%HARNESS_PYTHON%" -X utf8 "%HARNESS_CLI%" %*
exit /b %ERRORLEVEL%
:find_python
where py >nul 2>nul
if errorlevel 1 goto :use_python
py -3 -X utf8 "%HARNESS_CLI%" %*
exit /b %ERRORLEVEL%
:use_python
python -X utf8 "%HARNESS_CLI%" %*
exit /b %ERRORLEVEL%
