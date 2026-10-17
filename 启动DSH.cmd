@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -I bin\dsh_start.py --open
if errorlevel 1 pause
