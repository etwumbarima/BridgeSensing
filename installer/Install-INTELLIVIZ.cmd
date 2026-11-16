@echo off
powershell.exe -NoProfile -File "%~dp0Install-INTELLIVIZ.ps1"
if errorlevel 1 pause
