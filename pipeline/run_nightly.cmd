@echo off
REM Nightly job-search run, for Windows Task Scheduler.
REM
REM Everything it does lives in src\run_nightly.py -- the logging, the step
REM order, the verdict line and the log pruning. This file only finds Python
REM and hands over, so there is one copy of the logic rather than one per OS.
REM
REM Run it by hand any time to test:  run_nightly.cmd
REM
REM Keep this file pure ASCII. cmd.exe reads it in the OEM codepage, so one
REM accented character in a comment breaks the line it sits on, and the error
REM it reports names something else entirely.
REM
REM A scheduled task does not always get your interactive PATH, so if the run
REM fails with "python was not found", set JOB_PIPELINE_PYTHON to the full path
REM of python.exe -- either here or as an environment variable.

setlocal

if not defined JOB_PIPELINE_PYTHON set "JOB_PIPELINE_PYTHON=python"

cd /d "%~dp0src"
"%JOB_PIPELINE_PYTHON%" run_nightly.py %*
exit /b %ERRORLEVEL%
