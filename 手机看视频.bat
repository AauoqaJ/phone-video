@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 goto trypython
start "" pythonw "%~dp0phone_video_app.pyw"
exit /b
:trypython
where python >nul 2>nul
if errorlevel 1 goto nopython
start "" python "%~dp0phone_video_app.pyw"
exit /b
:nopython
echo.
echo   没有找到 Python，需要先安装 Python 3.10 或更高版本。
echo   下载地址： https://www.python.org/downloads/
echo   安装时请勾选 "Add Python to PATH"。
echo.
pause
