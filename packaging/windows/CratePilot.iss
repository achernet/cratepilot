#define AppName "CratePilot"
#define AppVersion GetEnv("CRATEPILOT_VERSION")
#define PayloadDir GetEnv("CRATEPILOT_PAYLOAD")
#define OutputDir GetEnv("CRATEPILOT_OUTPUT")

[Setup]
AppId={{8C1C14D3-69EB-4E63-9C4A-B89D28A0C6A1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Alex Chernetz
AppPublisherURL=https://cratepilot.chernetz.com
AppSupportURL=https://github.com/achernet/cratepilot/issues
DefaultDirName={localappdata}\Programs\CratePilot
DefaultGroupName=CratePilot
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=CratePilot-Setup-x64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Alex Chernetz
VersionInfoDescription=CratePilot Windows installer
VersionInfoProductName=CratePilot
ChangesEnvironment=yes

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CratePilot"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\CratePilot.ps1"""; WorkingDir: "{app}"
Name: "{group}\CratePilot dependency check"; Filename: "{app}\bin\cratepilot.cmd"; Parameters: "doctor"; WorkingDir: "{app}"
Name: "{group}\Uninstall CratePilot"; Filename: "{uninstallexe}"
Name: "{autodesktop}\CratePilot"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\CratePilot.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Run]
Filename: "{app}\bin\cratepilot.cmd"; Parameters: "doctor"; Description: "Verify the bundled audio toolchain"; Flags: postinstall skipifsilent runasoriginaluser
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\CratePilot.ps1"""; Description: "Launch CratePilot"; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
const
  EnvironmentKey = 'Environment';

function BinPath(Param: String): String;
begin
  Result := ExpandConstant('{app}\bin');
end;

function HasPathEntry(PathValue, Entry: String): Boolean;
begin
  Result := Pos(';' + Lowercase(Entry) + ';', ';' + Lowercase(PathValue) + ';') > 0;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PathValue, Entry: String;
begin
  if CurStep = ssPostInstall then begin
    Entry := BinPath('');
    RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', PathValue);
    if not HasPathEntry(PathValue, Entry) then begin
      if (PathValue <> '') and (PathValue[Length(PathValue)] <> ';') then PathValue := PathValue + ';';
      RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', PathValue + Entry);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  PathValue, Entry: String;
begin
  if CurUninstallStep = usUninstall then begin
    Entry := BinPath('');
    if RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', PathValue) then begin
      StringChangeEx(PathValue, ';' + Entry, '', True);
      StringChangeEx(PathValue, Entry + ';', '', True);
      if CompareText(PathValue, Entry) = 0 then PathValue := '';
      RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', PathValue);
    end;
  end;
end;
