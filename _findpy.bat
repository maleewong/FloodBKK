@echo off
rem sets PY to a working Python: Anaconda/Miniconda first, then the py launcher, then python on PATH
rem (the Windows "python" shortcut that opens the Microsoft Store does not count)
set PY=
for %%D in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\Anaconda3" "%USERPROFILE%\miniconda3" "%LOCALAPPDATA%\anaconda3" "%LOCALAPPDATA%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\Anaconda3" "%ProgramData%\miniconda3" "C:\anaconda3" "C:\Anaconda3" "D:\anaconda3" "D:\Anaconda3") do (
  if not defined PY if exist "%%~D\python.exe" set PY="%%~D\python.exe"
)
if defined PY (
  rem Anaconda needs its Library\bin on PATH for numpy/scipy DLLs
  for %%P in (%PY%) do set "PATH=%%~dpPLibrary\bin;%%~dpPScripts;%%~dpP;%PATH%"
  goto :ok
)
py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY python -c "import sys" >nul 2>nul && set PY=python
if defined PY goto :ok
echo.
echo ยังหา Python ไม่เจอ
echo - ถ้าลง Anaconda ไว้ที่อื่น: เปิด "Anaconda Prompt" แล้วพิมพ์  where python  แล้วส่งผลให้ Claude ดู
echo - หรือติดตั้งจาก https://www.python.org/downloads/ (ติ๊ก "Add python.exe to PATH")
echo.
pause
exit /b 1
:ok
%PY% -c "import sys; print('ใช้ Python', sys.version.split()[0], sys.executable)"
exit /b 0
