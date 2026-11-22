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
const
  GENERIC_READ_WRITE = $C0000000;
  OPEN_EXISTING = 3;
  ERROR_FILE_NOT_FOUND = 2;
  ERROR_PATH_NOT_FOUND = 3;
  SYNCHRONIZE_ACCESS = $00100000;
  PROCESS_QUERY_LIMITED_INFORMATION = $1000;
  WM_CLOSE = $0010;
  CLOSE_WAIT_STEPS = 300;      { 100 ms each: 30 s }

function CreateFileW(Path: String; Access, Share: DWORD; SecurityAttributes: UINT_PTR;
  Disposition, Flags: DWORD; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall setuponly';
function CloseNativeHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall setuponly';
function GetWindowThreadProcessId(Window: HWND; var ProcessId: DWORD): DWORD;
  external 'GetWindowThreadProcessId@user32.dll stdcall setuponly';
function OpenAppProcess(Access: DWORD; Inherit: BOOL; ProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall setuponly';
function QueryFullProcessImageNameW(AppHandle: THandle; Flags: DWORD;
  Image: String; var Characters: DWORD): BOOL;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall setuponly';

var
  WasRunning: Boolean;

function AppWasRunning(): Boolean;
begin
  Result := WasRunning;
end;

{ Opening an image for reading alone succeeds while it runs: ask for write
  access too, never write (OPEN_EXISTING cannot create or truncate). }
function CanReplaceInstalledFile(Path: String): Boolean;
var
  FileHandle: THandle;
  ErrorCode: DWORD;
begin
  FileHandle := CreateFileW(Path, GENERIC_READ_WRITE, 0, 0, OPEN_EXISTING, 0, 0);
  if FileHandle <> THandle(-1) then begin
    CloseNativeHandle(FileHandle);
    Result := True;
  end else begin
    ErrorCode := DLLGetLastError;
    Result := (ErrorCode = ERROR_FILE_NOT_FOUND) or (ErrorCode = ERROR_PATH_NOT_FOUND);
  end;
end;

{ A window called Lanterna is closed only when it belongs to the Lanterna.exe
  installed here (a folder of that name in Explorer has the same title). }
function IsInstalledApp(Window: HWND): Boolean;
var
  ProcessId, Characters: DWORD;
  Candidate: THandle;
  Image: String;
begin
  Result := False;
  ProcessId := 0;
  if (Window = 0) or (GetWindowThreadProcessId(Window, ProcessId) = 0) then Exit;
  Candidate := OpenAppProcess(SYNCHRONIZE_ACCESS or PROCESS_QUERY_LIMITED_INFORMATION, False, ProcessId);
  if Candidate = 0 then Exit;
  try
    Characters := 32768;
    SetLength(Image, Characters);
    if not QueryFullProcessImageNameW(Candidate, 0, Image, Characters) then Exit;
    SetLength(Image, Characters);
    Result := CompareText(ExpandFileName(Image), ExpandFileName(ExpandConstant('{app}\Lanterna.exe'))) = 0;
  finally
    CloseNativeHandle(Candidate);
  end;
end;

{ Was the app running when Setup started? Then it is asked to close, as its
  own X does (what is on the TV stops), and Setup waits for its files: the
  session bus GLib started for it (gdbus.exe, in its folder) quits about three
  seconds after the app, and the Restart Manager cannot close it, so without
  the wait an update over a running app was refused (exit code 5). Whatever
  is still open after 30 s is left to the Restart Manager, as before. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Window: HWND;
  Step: Integer;
begin
  Result := '';
  Window := FindWindowByWindowName('Lanterna');
  WasRunning := CheckForMutexes('') or (Window <> 0);
  if not IsInstalledApp(Window) then Exit;
  PostMessage(Window, WM_CLOSE, 0, 0);
  for Step := 1 to CLOSE_WAIT_STEPS do begin
    if CanReplaceInstalledFile(ExpandConstant('{app}\Lanterna.exe')) and
       CanReplaceInstalledFile(ExpandConstant('{app}\_internal\gdbus.exe')) then begin
      Log(Format('Lanterna and its session bus closed after %d ms', [Step * 100]));
      Exit;
    end;
    Sleep(100);
  end;
  Log('Lanterna or its session bus still open after 30 s: left to the Restart Manager');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    { the name and icon of its toasts, written by the app itself }
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\AppUserModelId\io.github.daniel_g_carrasco.lanterna');
  end;
end;
