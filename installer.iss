; 중복 영상 찾기 설치 파일 (Inno Setup)
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{778AA817-28E5-47F1-91F1-33A8A6227C1C}
AppName=중복 영상 찾기
AppVersion={#MyAppVersion}
AppVerName=중복 영상 찾기 {#MyAppVersion}
AppPublisher=kakario777
DefaultDirName={autopf}\VideoDupFinder
DisableProgramGroupPage=yes
DisableDirPage=yes
UninstallDisplayIcon={app}\VideoDupFinder.exe
UninstallDisplayName=중복 영상 찾기
OutputDir=installer_out
OutputBaseFilename=VideoDupFinder_Setup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
WizardStyle=modern
SetupIconFile=icon.ico
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany=kakario777
VersionInfoDescription=중복 영상 찾기 설치
VersionInfoProductName=VideoDupFinder

[Languages]
#if FileExists(CompilerPath + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Files]
Source: "dist\VideoDupFinder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\중복 영상 찾기"; Filename: "{app}\VideoDupFinder.exe"
Name: "{autodesktop}\중복 영상 찾기"; Filename: "{app}\VideoDupFinder.exe"; Check: IsFirstInstall

[Run]
Filename: "{app}\VideoDupFinder.exe"; Description: "중복 영상 찾기 실행"; Flags: nowait postinstall skipifsilent
Filename: "{app}\VideoDupFinder.exe"; Parameters: "--updated"; Flags: nowait runasoriginaluser; Check: ShouldRestart

[Code]
const
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{778AA817-28E5-47F1-91F1-33A8A6227C1C}_is1';
var
  FirstInstall: Boolean;

{ 처음 설치일 때만 바탕화면 바로가기를 만듦 (업데이트 때는 건드리지 않음) }
function InitializeSetup(): Boolean;
begin
  FirstInstall := not (RegKeyExists(HKLM, UninstKey) or RegKeyExists(HKCU, UninstKey));
  Result := True;
end;

function IsFirstInstall(): Boolean;
begin
  Result := FirstInstall;
end;

function ShouldRestart(): Boolean;
begin
  Result := ExpandConstant('{param:restart|0}') = '1';
end;
