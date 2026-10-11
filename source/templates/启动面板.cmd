@echo off
chcp 65001 >nul
title Friendswine Mod
if not exist "%~dp0panel.ps1" goto missing
if not exist "%~dp0runtime\python.exe" goto missing
start "" powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0panel.ps1"
exit /b 0
:missing
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgACcAL1SoUodl9k4NToxbdGUM//eLzZGwZYxbdGXjiYtTi1MpfwVTDlSNUdWLAjAnADsAIABpAGYAIAAoAC0AbgBvAHQAIABbAEMAbwBuAHMAbwBsAGUAXQA6ADoASQBzAEkAbgBwAHUAdABSAGUAZABpAHIAZQBjAHQAZQBkACkAIAB7ACAAWwB2AG8AaQBkAF0AWwBDAG8AbgBzAG8AbABlAF0AOgA6AFIAZQBhAGQASwBlAHkAKAAkAHQAcgB1AGUAKQAgAH0A
exit /b 1
