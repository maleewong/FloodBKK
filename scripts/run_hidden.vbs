' Runs auto_update.bat (one folder up) without a window; used by the Windows scheduled task "FloodBKK auto update".
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
CreateObject("WScript.Shell").Run "cmd /c """ & root & "\auto_update.bat""", 0, True
