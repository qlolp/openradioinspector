!macro ORI_CHECK_STOPPED
  ${If} ${FileExists} "$INSTDIR\runtime\ori-supervisor.exe"
    nsExec::ExecToStack '"$INSTDIR\runtime\ori-supervisor.exe" windows-node check-stopped'
    Pop $0
    Pop $1
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "Stop the local OpenRadioInspector node before changing its installation." /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro ORI_CHECK_STOPPED
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro ORI_CHECK_STOPPED
  ${If} $UpdateMode <> 1
  ${AndIf} ${FileExists} "$INSTDIR\runtime\ori-supervisor.exe"
    nsExec::ExecToStack '"$INSTDIR\runtime\ori-supervisor.exe" windows-node prepare-rollback'
    Pop $0
    Pop $1
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "Could not prepare the current archive for the next installation. Reconnect its volume, check free space, and retry." /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
    nsExec::ExecToStack '"$INSTDIR\runtime\ori-supervisor.exe" windows-node unregister'
    Pop $0
    Pop $1
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "Could not remove this installation's logon task." /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
  ${EndIf}
!macroend
