; Mud Music Manager - Windows installer (v0.20.0, Checklist item 60d)
;
; Built by packaging\build.ps1 with Inno Setup 6 (https://jrsoftware.org):
;   ISCC.exe /DAppVersion=0.20.0 /DSourceDir=<dist\Mud Music Manager> /DOutputDir=<release> installer.iss
;
; Installs for the current user only (no administrator prompt) into
; %LOCALAPPDATA%\Programs\Mud Music Manager, with a Start menu shortcut, an
; optional desktop shortcut and an uninstaller (Settings -> Apps). The user's
; data is NOT here: it lives in %APPDATA%\Mud Music Manager (app_paths.py), so
; installing a newer version over this one, or uninstalling, never touches
; playlists, settings or music.
;
; AppId must never change: it is how a newer installer recognises an
; installed copy and updates it in place.

#ifndef AppVersion
  #error AppVersion must be given: /DAppVersion=x.y.z
#endif
#ifndef SourceDir
  #error SourceDir must be given: /DSourceDir=<the built program folder>
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

#define AppName "Mud Music Manager"
#define AppExe "Mud Music Manager.exe"
#define AppUserModelID "MichaelXuereb.MudMusicManager"

[Setup]
AppId={{4974306D-5BCE-4E0F-A115-EED0580EF47D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Michael Xuereb
AppCopyright=Copyright (c) 2026 Michael Xuereb. GNU GPL v3 or later.
AppPublisherURL=https://github.com/Michael-Shwerby/Mud-Music-Manager
AppSupportURL=https://github.com/Michael-Shwerby/Mud-Music-Manager/issues
AppUpdatesURL=https://github.com/Michael-Shwerby/Mud-Music-Manager/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} installer
; Per-user: {autopf} is %LOCALAPPDATA%\Programs when not running as administrator
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DisableDirPage=auto
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Closes a running Mud Music Manager before replacing its files (updates)
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
SetupIconFile={#SourceDir}\_internal\assets\app_icon.ico
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
OutputDir={#OutputDir}
OutputBaseFilename=MudMusicManager-Setup-{#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An update replaces the whole program; files a newer version no longer
; ships must not linger beside it. (Only the program folder: never user data.)
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\tools"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "portable.txt"

[Icons]
; The taskbar ID matches the one the app gives itself (app_info.WINDOWS_APP_ID),
; so a pinned shortcut and the running window are one taskbar button.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "{#AppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; v0.21.0 (one-click update, app_update.launch_installer()): the app starts this
; installer with /SILENT /relaunch=1 and closes itself; reopen it when done.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: RelaunchAfterUpdate

[UninstallDelete]
; Leftovers the program itself may create in its folder (never the user's data folder)
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
// True only for an update started by the app (Update now): silent, with /relaunch=1.
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;
