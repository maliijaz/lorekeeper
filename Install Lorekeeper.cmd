@echo off
rem One-click install from this folder (also imports the library in .\data if there is one).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
