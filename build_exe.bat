@echo off
echo === Building WindowsAirPlay.exe (app with window) ===
py -m pip install --upgrade pip
py -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail
py -m PyInstaller --onefile --noconsole --name WindowsAirPlay --icon icon.ico --collect-all customtkinter --collect-all av --collect-all sounddevice --collect-all zeroconf --hidden-import pystray._win32 --add-data "airport_key.pem;." app.py
if errorlevel 1 goto :fail
echo.
echo === Building AirPlayReceiver.exe (console version) ===
py -m PyInstaller --onefile --name AirPlayReceiver --icon icon.ico --collect-all av --collect-all sounddevice --collect-all zeroconf --add-data "airport_key.pem;." airplay_audio.py
if errorlevel 1 goto :fail
echo.
echo DONE: dist\WindowsAirPlay.exe and dist\AirPlayReceiver.exe
pause
exit /b 0
:fail
echo.
echo BUILD FAILED - copy the text above and send it to me
pause
exit /b 1
