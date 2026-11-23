Option Explicit
Dim shell, fs, folder, python, entry
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
python = folder & "\.venv\Scripts\python.exe"
entry = folder & "\app.py"
If fs.FileExists(python) Then
    If Not fs.FileExists(entry) Then
        MsgBox "The file app.py is missing. Restore the LazyRef application files.", 48, "LazyRef"
        WScript.Quit 1
    End If
    On Error Resume Next
    ' The tiny launcher is hidden; it starts the actual GUI with CREATE_NO_WINDOW.
    shell.Run Chr(34) & python & Chr(34) & " -X utf8 " & Chr(34) & entry & Chr(34), 0, False
    If Err.Number <> 0 Then
        MsgBox "LazyRef could not start: " & Err.Description, 48, "LazyRef"
        WScript.Quit 1
    End If
    On Error GoTo 0
Else
    MsgBox "Please run setup.bat first.", 48, "LazyRef"
End If

