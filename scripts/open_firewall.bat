@echo off
REM Allow other PCs/phones on the network to open the dashboard (run as administrator).
netsh advfirewall firewall add rule name="Spike Net Monitor" dir=in action=allow protocol=TCP localport=8000
