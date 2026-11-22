; Inno Setup script for Lanterna.
; Compiled by the CI with:  ISCC.exe /DAppVersion=X.Y.Z build-aux\windows\lanterna.iss
; Expects the PyInstaller output in dist\Lanterna (repository root).
;
; The settings (%APPDATA%\Lanterna) and the app's own files
; (%LOCALAPPDATA%\Lanterna) stay when the app is uninstalled: they are the
; user's, and a reinstall goes on from them.

#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{630ED13A-93FA-4F8C-987A-339BEAD62BE8}
AppName=Lanterna
AppVersion={#AppVersion}
AppPublisher=Daniel Grasso
AppPublisherURL=https://github.com/daniel-g-carrasco/lanterna
AppSupportURL=https://github.com/daniel-g-carrasco/lanterna/issues
AppUpdatesURL=https://github.com/daniel-g-carrasco/lanterna/releases
DefaultDirName={autopf}\Lanterna
DefaultGroupName=Lanterna
DisableProgramGroupPage=yes
; per-user install by default (no UAC); the dialog lets the user elevate,
; and the command line (/CURRENTUSER, as the app's own update passes it)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
OutputDir=..\..\dist
OutputBaseFilename=lanterna-setup-v{#AppVersion}-windows-x64
SetupIconFile=lanterna.ico
UninstallDisplayIcon={app}\Lanterna.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; an update closes the running app (CloseApplications asks the Restart
; Manager; the app saves its state when its window closes)
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
italian.AppDescription=Mostra foto, presentazioni, video e film sulla TV con Chromecast
english.AppDescription=Show photos, slides, videos and films on your TV with Chromecast

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[InstallDelete]
; the version before's libraries and modules (PyInstaller's _internal) go
; first: one the new version no longer ships must not stay there and be
; loaded. The app has quit by then (CloseApplications); its settings and
; data are not in {app}, the uninstaller is not in _internal
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\Lanterna\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; the AppUserModelID the app sets for itself: its toasts, its taskbar button
Name: "{group}\Lanterna"; Filename: "{app}\Lanterna.exe"; Comment: "{cm:AppDescription}"; AppUserModelID: "io.github.daniel_g_carrasco.lanterna"
Name: "{autodesktop}\Lanterna"; Filename: "{app}\Lanterna.exe"; Tasks: desktopicon; AppUserModelID: "io.github.daniel_g_carrasco.lanterna"

[Run]
Filename: "{app}\Lanterna.exe"; Description: "{cm:LaunchProgram,Lanterna}"; Flags: nowait postinstall skipifsilent; Check: not AppWasRunning
; an update (from the app's own dialog, /SILENT) starts again the app it closed
Filename: "{app}\Lanterna.exe"; Flags: nowait runasoriginaluser; Check: AppWasRunning

[Code]
var
  WasRunning: Boolean;

function AppWasRunning(): Boolean;
begin
  Result := WasRunning;
end;

{ Was the app running when Setup started? The Restart Manager closes it
  (CloseApplications); this only remembers whether to start it again. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  WasRunning := CheckForMutexes('') or (FindWindowByWindowName('Lanterna') <> 0);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    { the name and icon of its toasts, written by the app itself }
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\AppUserModelId\io.github.daniel_g_carrasco.lanterna');
  end;
end;
