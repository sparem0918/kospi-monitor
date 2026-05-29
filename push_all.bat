@echo off
cd /d "%~dp0"
title KOSPI Monitor - Push All Files

python push_all_files.py

if errorlevel 1 (
    pause
)
