@echo off
REM Start the desktop translator without opening an extra Python console window.
"%~dp0.venv\Scripts\pythonw.exe" "%~dp0pdf_translate_gui.py"
