@echo off
REM Convert Little Swedish Girl XM to Furnace .fur for PC Engine
REM 9 channels -> drop 5,6 (1-based) to fit 7ch, then limit to 6
REM Channel 4 (1-based) -> swap to PCE noise slot (ch5)
cd /d "%~dp0"
python ..\..\convert_mod.py rez-little_swedish_girl.xm --noise_channel=4 --noise_insts=7:2
pause
