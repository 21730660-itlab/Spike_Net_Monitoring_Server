@echo off
REM Allow other PCs/phones on the network to open the dashboard (run as administrator).
netsh advfirewall firewall add rule name="Spike Net Monitor" dir=in action=allow protocol=TCP localport=8000
REM Let the "Scan network" page hear MikroTik (MNDP) and Ubiquiti discovery answers.
netsh advfirewall firewall add rule name="Spike Net discovery" dir=in action=allow protocol=UDP localport=5678,10001
