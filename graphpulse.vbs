' GraphPulse launcher: starts the server silently (no console window) if it is
' not already listening, then opens the viewer in the default browser.
'
' Used by both the desktop shortcut and the Startup entry:
'   wscript.exe graphpulse.vbs          -> start server (if needed) + open browser
'   wscript.exe graphpulse.vbs /nobrowser -> start server only (Startup use)
Option Explicit

Dim PORT, URL, APPDIR, SHELL, FSO, args, openBrowser, i
PORT = 8123
URL = "http://127.0.0.1:" & PORT & "/"

Set FSO = CreateObject("Scripting.FileSystemObject")
Set SHELL = CreateObject("WScript.Shell")
APPDIR = FSO.GetParentFolderName(WScript.ScriptFullName)

openBrowser = True
For i = 0 To WScript.Arguments.Count - 1
  If LCase(WScript.Arguments(i)) = "/nobrowser" Then openBrowser = False
Next

' --- is the server already up? ---
Function ServerIsUp()
  Dim http
  ServerIsUp = False
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  http.setTimeouts 800, 800, 1500, 1500
  http.Open "GET", URL & "api/graphs", False
  http.Send
  If Err.Number = 0 And http.Status = 200 Then ServerIsUp = True
  On Error GoTo 0
End Function

' --- resolve a python that exists on this machine ---
' Order: an explicit override, uv-managed interpreters, the Hermes venv, then
' whatever pythonw is on PATH. Only stdlib is used, so any 3.10+ works.
Function PythonExe()
  Dim candidates, c
  candidates = Array( _
    SHELL.ExpandEnvironmentStrings("%GRAPHPULSE_PYTHONW%"), _
    SHELL.ExpandEnvironmentStrings("%APPDATA%\uv\python\cpython-3.12-windows-x86_64-none\pythonw.exe"), _
    SHELL.ExpandEnvironmentStrings("%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"), _
    SHELL.ExpandEnvironmentStrings("%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\pythonw.exe"), _
    SHELL.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
    SHELL.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe") )
  For Each c In candidates
    If Len(c) > 0 Then
      If FSO.FileExists(c) Then
        PythonExe = c
        Exit Function
      End If
    End If
  Next
  ' fall back to whatever pythonw is on PATH
  PythonExe = "pythonw.exe"
End Function

If Not ServerIsUp() Then
  Dim cmd
  cmd = """" & PythonExe() & """ """ & APPDIR & "\server.py"" --port " & PORT & _
        " --roots ""D:/Hermes;D:/AI"""
  ' 0 = hidden window, False = do not wait
  SHELL.Run cmd, 0, False

  ' wait (max ~20 s) for it to accept connections before opening the browser
  Dim tries
  tries = 0
  Do While tries < 40
    WScript.Sleep 500
    If ServerIsUp() Then Exit Do
    tries = tries + 1
  Loop
End If

If openBrowser Then SHELL.Run URL, 1, False
