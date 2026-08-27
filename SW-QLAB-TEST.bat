@echo off
title SW QLAB - 接続テスト
cd /d "%‾dp0"
set SCRIPT=%‾dp0sw_qlab_monitor.py

echo QLab が動いている Mac の IP を入れてください。
echo 例: 192.168.0.30   （ポート違いなら 192.168.0.30:53000）
echo.
set /p TARGET=IP[:ポート] = 

where py >nul 2>&1 && goto PY
where python >nul 2>&1 && goto PYTHON
echo Python が見つかりません。
pause
goto END

:PY
py "%SCRIPT%" --try %TARGET%
goto END

:PYTHON
python "%SCRIPT%" --try %TARGET%

:END
echo.
pause
