@echo off
rem 「起動しない」ときの原因確認用。コンソール付きで起動してエラーを表示する。
cd /d "%~dp0"
call "%~dp0_python.bat"
echo 使用するPython: %PY%
"%PY%" -V
echo.
"%PY%" "%~dp0パイプライン管理.pyw"
echo.
echo ---- 終了コード: %ERRORLEVEL% ----
echo 上に出ているメッセージをそのまま貼り付けてください。
pause
