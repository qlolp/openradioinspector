#define AppVersion GetEnv("ORI_PACKAGE_VERSION")
[Setup]
AppId={{5B5236A1-2B9D-4777-9360-A82E9D88C75F}
AppName=OpenRadioInspector
AppVersion={#AppVersion}
AppPublisher=OpenRadioInspector contributors
DefaultDirName={localappdata}\Programs\OpenRadioInspector
DefaultGroupName=OpenRadioInspector
PrivilegesRequired=lowest
OutputDir=..\..\build\windows\packages
OutputBaseFilename=OpenRadioInspector-{#AppVersion}-windows-x64-unsigned
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\OpenRadioInspector.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
[Files]
Source: "..\..\build\windows\dist\OpenRadioInspector\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{group}\OpenRadioInspector"; Filename: "{app}\OpenRadioInspector.exe"
[Run]
Filename: "{app}\OpenRadioInspector.exe"; Description: "Запустить OpenRadioInspector"; Flags: nowait postinstall skipifsilent
; No UninstallDelete rule: user archives under LOCALAPPDATA are preserved.
