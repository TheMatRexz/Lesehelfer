@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Lesehelfer

rem Komplettpaket: eingebautes Python im Ordner "python" - dann ist alles schon dabei
if exist "%~dp0python\pythonw.exe" (
  start "" "%~dp0python\pythonw.exe" "%~dp0programm\lesehelfer.py"
  exit /b
)

rem Python 3.14 bevorzugen (dafuer gibt es die lokale Texterkennung RapidOCR)
call :python_suchen
if not "%PYVER%"=="314" (
  echo Fuer die bessere Texterkennung wird Python 3.14 installiert ...
  echo Das dauert beim ersten Mal ein paar Minuten. Bitte warten.
  winget install -e --id Python.Python.3.14 --scope user --silent --accept-package-agreements --accept-source-agreements
  call :python_suchen
)
if not defined PY (
  echo.
  echo Python konnte nicht automatisch installiert werden.
  echo Bitte Python von python.org installieren.
  pause
  exit /b 1
)

echo Pruefe benoetigte Pakete (beim ersten Mal dauert das etwas) ...
"%PY%" -m pip install --quiet --disable-pip-version-check mss pillow pywin32 keyboard winocr edge-tts pyspellchecker customtkinter
if errorlevel 1 (
  echo.
  echo Beim Installieren der Pakete ist etwas schiefgegangen. Internet verbunden?
  pause
  exit /b 1
)
rem lokale Texterkennung (optional - ohne sie wird die Windows-Texterkennung benutzt)
"%PY%" -m pip install --quiet --disable-pip-version-check rapidocr
rem onnxruntime-directml: rechnet auf dem Prozessor UND (wenn moeglich) auf der Grafikkarte
"%PY%" -m pip show onnxruntime-directml >nul 2>&1
if errorlevel 1 (
  "%PY%" -m pip uninstall -y onnxruntime >nul 2>&1
  "%PY%" -m pip install --quiet --disable-pip-version-check onnxruntime-directml
  if errorlevel 1 "%PY%" -m pip install --quiet --disable-pip-version-check onnxruntime
)
rem Offline-Uebersetzung Englisch -> Deutsch (optional)
"%PY%" -m pip install --quiet --disable-pip-version-check ctranslate2 sentencepiece
rem Vorlesen mit der Maus (Reiter Desktop, optional)
"%PY%" -m pip install --quiet --disable-pip-version-check uiautomation
rem VR: Controller-Tasten ueber SteamVR (Reiter VR, optional)
"%PY%" -m pip install --quiet --disable-pip-version-check openvr

rem Oberflaeche ohne schwarzes Fenster starten
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%~dp0programm\lesehelfer.py"
exit /b

:python_suchen
set "PY="
set "PYVER="
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  set "PYVER=314"
  exit /b
)
if exist "%ProgramFiles%\Python314\python.exe" (
  set "PY=%ProgramFiles%\Python314\python.exe"
  set "PYVER=314"
  exit /b
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if defined PY exit /b
for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if defined PY exit /b
for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PY set "PY=%%P"
exit /b
