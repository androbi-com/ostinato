; SPDX-License-Identifier: GPL-3.0-only
; Inno Setup 6 script: Ostinato-Setup-<version>.exe from the PyInstaller bundle in dist\Ostinato\.
; Built by tools\build_windows.ps1, which passes /DAppVersion=... /DRoot=<repository>.
; Installs for the current user (no administrator rights): %LOCALAPPDATA%\Programs\Ostinato.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Root
  #define Root ".."
#endif

[Setup]
AppId={{6F3C1B7E-2D4A-4E8B-9C51-0A7D3E8F42B6}
AppName=Ostinato
AppVersion={#AppVersion}
AppVerName=Ostinato {#AppVersion}
AppPublisher=androbi
AppPublisherURL=https://github.com/androbi-com/ostinato
AppSupportURL=https://github.com/androbi-com/ostinato/issues
AppUpdatesURL=https://github.com/androbi-com/ostinato/releases
AppComments=A sequencer you can talk to, standalone or with the M-VAVE FM-1 running SLOOP: Claude and your browser.
DefaultDirName={localappdata}\Programs\Ostinato
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#Root}\LICENSE
SetupIconFile={#Root}\build\ostinato.ico
UninstallDisplayIcon={app}\Ostinato.exe
UninstallDisplayName=Ostinato
OutputDir={#Root}\dist
OutputBaseFilename=Ostinato-Setup-{#AppVersion}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no

[Tasks]
Name: "claude"; Description: "Connect Ostinato to Claude Desktop (restart Claude Desktop afterwards)"
Name: "autostart"; Description: "Start Ostinato when I sign in to Windows (it waits in the tray, next to the clock)"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#Root}\dist\Ostinato\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Root}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Ostinato"; Filename: "{app}\Ostinato.exe"; Comment: "Ostinato: a sequencer with Claude and in the browser, standalone or with your FM-1"
Name: "{autodesktop}\Ostinato"; Filename: "{app}\Ostinato.exe"; Tasks: desktopicon
Name: "{userstartup}\Ostinato"; Filename: "{app}\Ostinato.exe"; Parameters: "--no-open"; Tasks: autostart

[Run]
Filename: "{app}\ostinato-cli.exe"; Parameters: "claude-config add --exe ""{app}\ostinato-cli.exe"""; \
  Tasks: claude; Flags: runhidden; StatusMsg: "Connecting Ostinato to Claude Desktop..."
Filename: "{app}\Ostinato.exe"; Description: "Start Ostinato now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\ostinato-cli.exe"; Parameters: "claude-config remove --exe ""{app}\ostinato-cli.exe"""; \
  Flags: runhidden; RunOnceId: "ClaudeConfig"

[Messages]
FinishedLabel=Ostinato is installed.%n%nStart it from the Start menu: it waits in the tray, next to the clock, and opens its page in your browser. Press "Use the virtual device" there to play on this computer, or plug in your FM-1 (with SLOOP) by USB.%n%nIf you connected it to Claude Desktop, restart Claude Desktop to see Ostinato's tools (after an update too: they stay disconnected until then).

[Code]
// Stop everything that runs from the install folder (the files are in use): the device server (which lets go of the
// device), the tray, then any other ostinato-cli.exe: the MCP shims, which Claude starts and keeps running (one each
// for the Chat tab and Code sessions). Those only by their path: another copy of Ostinato is left alone.
procedure StopPrograms;
var
  Cli, Quoted: String;
  Code: Integer;
begin
  Cli := ExpandConstant('{app}\ostinato-cli.exe');
  if FileExists(Cli) then
    Exec(Cli, 'stop --if-mine', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM Ostinato.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Quoted := Cli;
  StringChangeEx(Quoted, '''', '''''', True);  // (an apostrophe in the user's name)
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -Command "Get-Process ostinato-cli -ErrorAction SilentlyContinue | ' +
       'Where-Object { $_.Path -eq ''' + Quoted + ''' } | Stop-Process -Force -PassThru | Wait-Process -Timeout 10"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

// An update while Ostinato runs.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopPrograms;
  Result := '';
end;

// Uninstalling: the saved states and settings are the user's: keep them unless asked.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then  // (before [UninstallRun] and the files)
    StopPrograms;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{userappdata}\ostinato');
    if DirExists(Data) and not UninstallSilent then
      if MsgBox('Also delete your saved states and settings?' + #13#10 + Data + #13#10#13#10 +
                'Choose No to keep them (for example to install Ostinato again later).',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
