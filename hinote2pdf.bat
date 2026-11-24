@echo off
rem Drag .hinote files or folders onto this file, or double-click it to pick files.
rem A small window then asks: with or without background, lossless or HQ lossy.
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m hinote2pdf --ask %*
rem keep the window open after a drag-and-drop run (the file picker shows its own message)
if not "%~1"=="" pause
