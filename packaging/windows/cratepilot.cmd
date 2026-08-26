@echo off
set "CRATEPILOT_ROOT=%~dp0.."
set "PATH=%~dp0;%CRATEPILOT_ROOT%\native;%PATH%"
"%CRATEPILOT_ROOT%\python\python.exe" -m cratepilot %*
