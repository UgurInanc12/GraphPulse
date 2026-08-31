' Installs GraphPulse autostart + desktop shortcut for the current user.
'   cscript //nologo install-autostart.vbs          -> install
'   cscript //nologo install-autostart.vbs /uninstall -> remove both
Option Explicit

Dim SHELL, FSO, APPDIR, desktop, startup, lnkDesktop, lnkStartup, uninstall, i
Set SHELL = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
APPDIR = FSO.GetParentFolderName(WScript.ScriptFullName)

uninstall = False
For i = 0 To WScript.Arguments.Count - 1
  If LCase(WScript.Arguments(i)) = "/uninstall" Then uninstall = True
Next

desktop = SHELL.SpecialFolders("Desktop")
startup = SHELL.SpecialFolders("Startup")
lnkDesktop = desktop & "\GraphPulse.lnk"
lnkStartup = startup & "\GraphPulse.lnk"

If uninstall Then
  If FSO.FileExists(lnkDesktop) Then FSO.DeleteFile lnkDesktop
  If FSO.FileExists(lnkStartup) Then FSO.DeleteFile lnkStartup
  WScript.Echo "removed: " & lnkDesktop
  WScript.Echo "removed: " & lnkStartup
  WScript.Quit 0
End If

Dim sc
' Desktop shortcut: start the server if needed, then open the viewer.
Set sc = SHELL.CreateShortcut(lnkDesktop)
sc.TargetPath = "wscript.exe"
sc.Arguments = """" & APPDIR & "\graphpulse.vbs"""
sc.WorkingDirectory = APPDIR
sc.Description = "GraphPulse - live 3D knowledge graph viewer (http://127.0.0.1:8123/)"
sc.IconLocation = "%SystemRoot%\System32\SHELL32.dll,13"
sc.WindowStyle = 7   ' minimized; the VBS itself is windowless
sc.Save

' Startup entry: server only, no browser window on every boot.
Set sc = SHELL.CreateShortcut(lnkStartup)
sc.TargetPath = "wscript.exe"
sc.Arguments = """" & APPDIR & "\graphpulse.vbs"" /nobrowser"
sc.WorkingDirectory = APPDIR
sc.Description = "GraphPulse server (autostart, no window)"
sc.IconLocation = "%SystemRoot%\System32\SHELL32.dll,13"
sc.WindowStyle = 7
sc.Save

WScript.Echo "desktop shortcut: " & lnkDesktop
WScript.Echo "startup entry   : " & lnkStartup
