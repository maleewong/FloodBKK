@echo off
chcp 65001 >nul
cd /d "%~dp0"
call _findpy.bat || exit /b 1
echo ติดตั้งแพ็กเกจที่ยังขาด สำหรับแผนระบาย 30 วันและหน้าน้ำท่วมขัง (ครั้งเดียว)...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (echo ติดตั้งไม่สำเร็จ ส่งข้อความในหน้าต่างนี้ให้ Claude ดูได้ & pause & exit /b 1)
echo.
echo เสร็จแล้ว ใช้ start.bat หรือ update.bat ได้เลย
pause
