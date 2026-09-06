@echo off
setlocal
title Sutton House - Create Trial Users
cd /d "%~dp0"

echo ========================================
echo   SUTTON HOUSE - CREATE TRIAL USERS
echo ========================================
echo.

python create_user_batch.py "caraway62cogent@icloud.com" "observer" "Atlantic"
python create_user_batch.py "phippsgarry@aol.com" "observer" "Saturn"
python create_user_batch.py "gary.macdonald1@ntlworld.com" "observer" "Africa"

echo.
echo ========================================
echo User creation complete.
echo Check above for CREATED / SKIPPED / ERROR.
echo ========================================
echo.
echo IMPORTANT: Delete this BAT file after successful creation.
pause
endlocal
