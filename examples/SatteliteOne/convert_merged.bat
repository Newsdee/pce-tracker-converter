@echo off
REM Convert Satellite One S3M with channel merging (8 -> 6)
REM Analysis shows best plan: merge ch5->ch3, merge ch7->ch6
REM  ch5 (arpeggios 48%%) folds into ch3 (sparse 12%%) = 89%% preserved
REM  ch7 (harmony 57%%) folds into ch6 (counter-melody 48%%) = 49%% preserved
REM  Total: 93%% of all notes kept (vs 88%% with pure drop)
cd /d "%~dp0"
python ..\..\convert_mod.py SATELL.S3M SATELL_merged.fur --merge_channels=5:3,7:6
pause
