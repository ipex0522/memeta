@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul && goto :run_with_launcher
where python >nul 2>nul && goto :run_with_python

echo.
echo Python 3 が見つかりません。
echo Python 3.10 以降をインストールし、PATH に追加してから再実行してください。
echo https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:run_with_launcher
py -3 "%~dp0main.py"
goto :check_result

:run_with_python
python "%~dp0main.py"

:check_result
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Memeta はエラーで終了しました。上のメッセージを確認してください。
echo.
pause
exit /b 1
