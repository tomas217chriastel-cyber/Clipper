@echo off
REM Needs Python 3.11 or 3.12 (python.org) ONLY for building. The finished app needs nothing.
py -3 build.py %*
pause
