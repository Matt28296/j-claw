@echo off
chcp 65001 >nul
REM -- bot_build.bat "<project description>" --
REM Launch a J-Claw build DETACHED and return immediately. A chat/bot turn that
REM triggers a build is bounded by the agent's ~120s turn timeout, but a real
REM build takes 10-20 min. Running it in its own independent console (via START)
REM lets the build outlive the bot turn -- the bot replies "started" instantly and
REM the run is watched through Mission Control instead of the chat turn.
if "%~1"=="" (
  echo ERROR: no project description provided.
  echo Usage: bot_build.bat "what to build"
  exit /b 1
)
start "J-Claw Build" /min cmd /c ""%~dp0run.bat" --yes "%~1""
echo J-Claw build STARTED in the background:
echo   %~1
echo.
echo Watch Mission Control (updates live):
echo   http://localhost:8765/dashboard/index.html
echo   http://100.116.101.29:8765/dashboard/index.html   ^(phone / Tailscale^)
exit /b 0
