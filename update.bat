@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call _findpy.bat || exit /b 1
echo กำลังดึงข้อมูลล่าสุดจากสำนักการระบายน้ำ...
%PY% scripts\update.py %*
if errorlevel 1 (pause & exit /b 1)
start "" "bkk_drainage.html"
timeout /t 5 >nul
