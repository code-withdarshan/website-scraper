@echo off
echo Killing all Python / Streamlit / Flask processes...
taskkill /F /IM python.exe 2>nul
taskkill /F /IM py.exe 2>nul
taskkill /F /IM streamlit.exe 2>nul
echo Done.
timeout /t 2 >nul
