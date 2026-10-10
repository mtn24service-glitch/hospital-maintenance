@echo off
chcp 65001 >nul
title مستشفى الأمير محمد بن ناصر - نظام بلاغات الصيانة
echo ============================================================
echo   نظام إدارة بلاغات الصيانة والتشغيل
echo   مستشفى الأمير محمد بن ناصر - جازان
echo ============================================================
cd /d "%~dp0"
python -m venv .venv 2>nul
call .venv\Scripts\activate.bat
pip install -r requirements.txt -q
python -m app.seed
echo.
echo بدء التشغيل في المتصفح قريباً...
echo اللوحة الرئيسية:      http://127.0.0.1:8000/dashboard
echo تقديم بلاغ (عام):     http://127.0.0.1:8000/report
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
pause