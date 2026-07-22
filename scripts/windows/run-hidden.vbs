' run-hidden.vbs - windowless launcher for Task Scheduler actions.
'
' WHY THIS EXISTS (2026-07-21): a scheduled task registered with the default
' LogonType=Interactive runs on the user's desktop, so every firing flashes a console
' window and steals focus. The offline-watcher repeats every 2 MINUTES, which made the
' laptop unusable; it had been silenced by hand, which fixed the symptom on one box and
' left the registration itself still wrong.
'
' Two ways to make a task windowless:
'   1. LogonType S4U ("run whether the user is logged on or not") - runs in a
'      non-interactive session, so no window can exist. Rejected here: these tasks call
'      wsl.exe, and WSL is unreliable outside an interactive user session.
'   2. Launch through a windowless host - this file. wscript.exe has no console of its
'      own, and WshShell.Run with intWindowStyle=0 starts the child hidden. The task
'      stays in the user's session (so WSL works) and nothing is ever drawn.
'
' -WindowStyle Hidden on pwsh is NOT equivalent: the host console is created and then
' hidden, which still flashes. The window must never be created in the first place.
'
' Usage (from a scheduled-task action):
'   Execute:   wscript.exe
'   Arguments: //nologo "<...>\run-hidden.vbs" "<exe>" <args...>
'
' Waits for the child and propagates its exit code, so ExecutionTimeLimit and
' LastTaskResult keep working - a fire-and-forget shim would report success instantly
' and hide every failure.

Option Explicit

Dim shell, i, arg, commandLine, exitCode

If WScript.Arguments.Count = 0 Then
    WScript.Quit 2   ' nothing to run - surfaces as a task failure rather than a silent success
End If

commandLine = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    ' wscript strips the quotes it parsed; restore them wherever the value contains a
    ' space, or a path like C:\Program Files\... would split into two tokens.
    If InStr(arg, " ") > 0 And Left(arg, 1) <> """" Then
        arg = """" & arg & """"
    End If
    If Len(commandLine) > 0 Then commandLine = commandLine & " "
    commandLine = commandLine & arg
Next

Set shell = CreateObject("WScript.Shell")
' 0 = hidden window, True = wait so the exit code is real
exitCode = shell.Run(commandLine, 0, True)
WScript.Quit exitCode
