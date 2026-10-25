' run_hidden.vbs -- start a PowerShell script with NO console window.
'
' Why this exists: a scheduled task whose action is
'     powershell -File some_script.ps1
' makes Windows allocate a CONSOLE for that process. Two things that look like
' they fix it do NOT:
'
'   1. The Task Scheduler "Hidden" checkbox (Settings.Hidden) is a UI-only flag:
'      it hides the task from the Task Scheduler list, and has no effect on the
'      window a started program paints.
'   2. `-WindowStyle Hidden` operates on a window that has ALREADY been created,
'      so on its own it can still leave a brief flash.
'
' wscript.exe is a GUI-subsystem binary (PE subsystem 2), so it allocates no console of its
' own. The child PowerShell below DOES still get a console, but window style 0 makes Windows
' create it hidden (SW_HIDE in STARTUPINFO) rather than hiding it after the fact, so nothing
' is ever painted. A task that runs on a timer (a2a-agy-transport repeats for
' restart-on-failure, and the common case is the launcher's fast "already running" no-op)
' therefore shows no window. Before this shim that no-op path allocated a visible console on
' every tick, which is the flashing window users see.
'
' Usage from a task action:
'   wscript.exe //B "...\scripts\run_hidden.vbs" "...\scripts\run_x.ps1" [args...]
'
' The child is waited for (third arg Run = True) and its exit code is returned,
' so Task Scheduler's "Last Result" keeps meaning what it meant before: while the
' transport runs the task stays Running and IgnoreNew skips repetitions, and a
' transport that dies hands its status back through this shim.

Option Explicit

Dim argList, shell, scriptPath, command, i, exitCode

Set argList = WScript.Arguments
If argList.Count < 1 Then
    WScript.Echo "usage: wscript.exe //B run_hidden.vbs <script.ps1> [args...]"
    WScript.Quit 2
End If

scriptPath = argList(0)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " _
          & QuoteArg(scriptPath)

For i = 1 To argList.Count - 1
    command = command & " " & QuoteArg(argList(i))
Next

Set shell = CreateObject("WScript.Shell")
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

' Quote an argument using the canonical CreateProcess (CRT) rules.
'
' WHY CRT RULES AND NOT "just wrap it in quotes": the command line built here is handed to
' WScript.Shell.Run, which passes it straight to CreateProcess -- it does no parsing of its
' own. The process that parses it is powershell.exe, a console-subsystem binary that uses the
' standard CRT command-line parser. That parser treats a backslash before a quote as an escape,
' so naive quoting is NOT sufficient:
'
'     -Path "C:\dir with space\"      arrives as  C:\dir with space"   <- path corrupted
'     -Path "C:\dir with space\\"     arrives as  C:\dir with space\   <- correct
'
' A backslash run must therefore be doubled when it precedes a quote (or the closing quote),
' and an embedded quote escaped with 2n+1 backslashes. A plain argument is passed through
' unquoted, so the live task actions stay byte-identical to what they name.
Function QuoteArg(value)
    Dim quotable, i, ch, out, backslashes
    quotable = (value = "") _
        Or (InStr(value, " ") > 0) _
        Or (InStr(value, Chr(9)) > 0) _
        Or (InStr(value, Chr(34)) > 0)
    If Not quotable Then
        QuoteArg = value
        Exit Function
    End If

    out = Chr(34)
    backslashes = 0
    For i = 1 To Len(value)
        ch = Mid(value, i, 1)
        If ch = "\" Then
            ' Defer runs of backslashes: they double only if a quote follows.
            backslashes = backslashes + 1
        ElseIf ch = Chr(34) Then
            ' 2n backslashes, then 1 to escape the quote itself.
            out = out & String(backslashes * 2 + 1, "\") & Chr(34)
            backslashes = 0
        Else
            If backslashes > 0 Then
                out = out & String(backslashes, "\")
                backslashes = 0
            End If
            out = out & ch
        End If
    Next
    ' A run at the end of the value precedes the closing quote, so it doubles.
    If backslashes > 0 Then out = out & String(backslashes * 2, "\")

    QuoteArg = out & Chr(34)
End Function
