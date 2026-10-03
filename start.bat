@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call _findpy.bat || exit /b 1
rem เปิดเว็บแอปที่ http://127.0.0.1:8765  ปิดหน้าต่างนี้เมื่อเลิกใช้   (อัปเดตเองทุก 60 นาที: start.bat --auto 60)
%PY% scripts\serve.py %*
pause
