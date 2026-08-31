' GraphPulse launcher: starts the server silently (no console window) if it is
' not already listening, then opens the viewer in the default browser.
'
' Used by both the desktop shortcut and the Startup entry:
'   wscript.exe graphpulse.vbs          -> start server (if needed) + open browser
'   wscript.exe graphpulse.vbs /nobrowser -> start server only (Startup use)
Option Explicit

Dim PORT, URL, APPDIR, SHELL, FSO, openBrowser, i
Dim logPath, pyExe, batPath, bat, bf, tries, msg, ts, tailTxt
Dim lines, ln
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
' Probes the TCP port directly. An HTTP probe through MSXML is unreliable here:
' it can return success from a proxy/cached handler even when nothing is
' listening on 127.0.0.1:PORT, which made the launcher think the server was
' already running and silently do nothing.
Function ServerIsUp()
  Dim exec, out
  ServerIsUp = False
  On Error Resume Next
  Set exec = SHELL.Exec("netstat -ano -p TCP")
  If Err.Number <> 0 Then
    On Error GoTo 0
    Exit Function
  End If
  out = exec.StdOut.ReadAll
  On Error GoTo 0
  lines = Split(out, vbCrLf)
  For Each ln In lines
    If InStr(ln, "127.0.0.1:" & PORT & " ") > 0 And InStr(ln, "LISTENING") > 0 Then
      ServerIsUp = True
      Exit For
    End If
  Next
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
  logPath = APPDIR & "\server.log"
  pyExe = PythonExe()

  ' Write a tiny launcher .bat instead of fighting cmd.exe quoting rules from
  ' VBScript (a mis-paired quote there fails silently: no process, no log).
  ' The bat redirects stdout+stderr, so a crash always leaves a trace.
  batPath = APPDIR & "\.launch-server.bat"
  bat = "@echo off" & vbCrLf & _
        "cd /d " & Chr(34) & APPDIR & Chr(34) & vbCrLf & _
        Chr(34) & pyExe & Chr(34) & " " & Chr(34) & APPDIR & "\server.py" & Chr(34) & _
        " --port " & PORT & " --roots " & Chr(34) & "D:/Hermes;D:/AI" & Chr(34) & _
        " > " & Chr(34) & logPath & Chr(34) & " 2>&1" & vbCrLf
  Set bf = FSO.CreateTextFile(batPath, True)
  bf.Write bat
  bf.Close

  ' 0 = hidden window, False = do not wait
  SHELL.Run Chr(34) & batPath & Chr(34), 0, False

  ' wait (max ~20 s) for it to accept connections before opening the browser
  tries = 0
  Do While tries < 40
    WScript.Sleep 500
    If ServerIsUp() Then Exit Do
    tries = tries + 1
  Loop

  If Not ServerIsUp() Then
    ' Surface the failure instead of exiting quietly.
    msg = "GraphPulse could not start. See " & logPath
    If FSO.FileExists(logPath) Then
      Set ts = FSO.OpenTextFile(logPath, 1)
      tailTxt = ts.ReadAll
      ts.Close
      If Len(tailTxt) > 600 Then tailTxt = Right(tailTxt, 600)
      msg = msg & vbCrLf & vbCrLf & tailTxt
    End If
    If openBrowser Then MsgBox msg, 16, "GraphPulse"
    WScript.Quit 1
  End If
End If

If openBrowser Then SHELL.Run URL, 1, False
