@echo off
REM Convert Satellite One S3M to Furnace .fur for PC Engine
REM 8 channels -> drop ch3 (sparse accents) and ch8 (4th harmony) to fit 6
cd /d "%~dp0"
python ..\..\convert_mod.py SATELL.S3M --drop_channels=3,8
pause
