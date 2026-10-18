@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title 打包成 exe
echo ============================================
echo   把这个程序打包成单文件 exe
echo ============================================
echo.
where python >nul 2>nul
if errorlevel 1 goto nopython
echo   [1/3] 检查并安装 PyInstaller ...
python -m pip install --upgrade --disable-pip-version-check pyinstaller
if errorlevel 1 goto pipfail
echo.
echo   [2/3] 正在打包，大约需要一两分钟，请勿关闭窗口 ...
python -m PyInstaller --noconfirm --clean --noconsole --onefile --icon icon.ico --add-data "icon.ico;." --name "手机看视频" phone_video_app.pyw
if errorlevel 1 goto buildfail
echo.
echo   [3/3] 检查产物 ...
if not exist "dist\手机看视频.exe" goto noexe
echo.
echo   完成！exe 在： %~dp0..\dist\手机看视频.exe
echo   这个文件可以单独发给别人，对方不需要安装 Python。
echo.
echo   提示：如果对方电脑的杀毒软件报毒，是 PyInstaller 单文件包的常见误报，
echo         加白名单即可；也可以改用 --onedir 方式打包。
echo.
if exist "%~dp0..\dist" start "" "%~dp0..\dist"
pause
exit /b
:nopython
echo   [!] 没有找到 Python。
echo       请先安装 Python 3.10 或更高版本，安装时勾选 Add Python to PATH。
echo       https://www.python.org/downloads/
pause
exit /b
:pipfail
echo   [!] PyInstaller 安装失败。
echo       请检查网络；公司网络可能需要先配置 pip 镜像源。
pause
exit /b
:buildfail
echo   [!] 打包失败，请把上面的错误信息发给我。
pause
exit /b
:noexe
echo   [!] 打包命令执行完了，但没有找到 dist\手机看视频.exe。
echo       请把上面的输出发给我。
pause
exit /b
