@echo off
REM Convert Tinytune.mod to Furnace .fur for PC Engine
REM Sample 5 (ST-01:Drumsharp) is forced to noise channel
python ..\..\convert_mod.py Tinytune.mod Tinytune.fur --noise_insts=5
pause
