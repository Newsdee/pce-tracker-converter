@echo off
REM Convert FamiTracker .ftm to Furnace .fur for PC Engine
REM Preserves volume and arpeggio envelopes from SEQUENCES block
python ..\..\convert_mod.py enl_track1.ftm enl_track1.fur
pause
