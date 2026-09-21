@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'AliExpress Monitor Daily Collection' -Confirm:$false -ErrorAction SilentlyContinue"
echo Daily browser collector task removed. The dedicated Chrome profile was kept.
pause
endlocal
