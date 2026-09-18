@echo off
echo Instalando ADB y scrcpy...
winget install --id Google.PlatformTools -e --accept-source-agreements --accept-package-agreements
winget install --id Genymobile.scrcpy -e --accept-source-agreements --accept-package-agreements
echo.
echo Listo. Cierra esta ventana y abre "Abrir control.bat".
pause
