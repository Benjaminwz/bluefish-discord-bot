; 藍色大肥魚 Windows 安裝程式（Inno Setup 6）
; 不要直接編譯這個檔案：執行 python installer\build.py v1.2.3，它會先下載 uv.exe、做好精靈圖片再呼叫 ISCC。
; 裝的時候不用系統管理員權限，預設裝在 %LOCALAPPDATA%\Programs\藍色大肥魚。
; 本魚專用的 Python 由 bootstrap.cmd 用 uv 下載到安裝資料夾裡，不會動到電腦裡其他的 Python。

#ifndef AppVersion
  #define AppVersion "dev"
#endif
#define AppName "藍色大肥魚"
#define Root "..\.."
#define Shortcut "藍色大肥魚控制面板"

[Setup]
#ifdef TESTBUILD
AppId={{5B7F3D0C-TEST-4E1A-9B2C-000000000000}
#else
AppId={{6C3E2F7A-8B41-4D59-A0E2-3F7B1C9D5E84}
#endif
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Benjaminwz
AppPublisherURL=https://github.com/Benjaminwz/bluefish-discord-bot
AppSupportURL=https://github.com/Benjaminwz/bluefish-discord-bot/issues
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; RedirectionGuard 會一路傳給安裝時跑的程式，害 uv 建不了 Python 的資料夾連結（錯誤 448）。
; 這個安裝程式不用管理員權限，本來就沒有它要防的提權風險，所以關掉。
RedirectionGuard=no
OutputDir=..\dist
OutputBaseFilename=BlueFish-Setup-Windows-{#AppVersion}
SetupIconFile={#Root}\bluefish.ico
UninstallDisplayIcon={app}\bluefish.ico
UninstallDisplayName={#AppName} Discord 機器人
WizardStyle=modern
WizardImageFile=build\wizard.bmp,build\wizard_2x.bmp
WizardSmallImageFile=build\wizard_small.bmp,build\wizard_small_2x.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "cht"; MessagesFile: "ChineseTraditional.isl"

[Messages]
WelcomeLabel2=這會在你的電腦上安裝 [name/ver]：繁體中文的 Discord 機器人，附控制面板。%n%n安裝時會自動下載本魚專用的 Python 和套件（約 50～100 MB），請保持網路連線。裝好後會打開設定視窗，幫你選 AI、貼上 Discord 機器人的 Token。
FinishedLabel=本魚裝好了！%n%n接下來的設定視窗會一步步帶你完成設定，之後從開始功能表的「{#Shortcut}」打開本魚。

[Tasks]
Name: "desktopicon"; Description: "在桌面建立「{#Shortcut}」"; GroupDescription: "捷徑："

[Files]
Source: "{#Root}\deepseek_discord_bot.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\panel.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\knowledge.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\setup_wizard.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\bluefish.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\cover.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\avatar.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "bootstrap.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "uv\uv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
; 設定檔只在第一次安裝時放，更新版本時不會蓋掉你的設定；解除安裝時也保留（最後會問要不要刪）
Source: "..\default_config.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall

#ifndef TESTBUILD
[Icons]
Name: "{userprograms}\{#Shortcut}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\panel.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\bluefish.ico"; Comment: "藍色大肥魚 Discord 機器人的控制面板"; AppUserModelID: "BlueFish.ControlPanel"
Name: "{userdesktop}\{#Shortcut}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\panel.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\bluefish.ico"; Comment: "藍色大肥魚 Discord 機器人的控制面板"; AppUserModelID: "BlueFish.ControlPanel"; Tasks: desktopicon
#endif

[Run]
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\setup_wizard.py"" --if-needed"; WorkingDir: "{app}"; Description: "繼續設定本魚（選 AI、貼上 Token）"; Flags: postinstall nowait skipifsilent; Check: PythonReady

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\music_cache"
Type: files; Name: "{app}\ffmpeg.exe"
Type: files; Name: "{app}\deno.exe"
Type: files; Name: "{app}\install-log.txt"
Type: files; Name: "{app}\bot.pid"
#ifndef TESTBUILD
Type: files; Name: "{userstartup}\{#Shortcut}.lnk"
#endif

[Code]
var
  PythonOK: Boolean;

{ 關掉這個資料夾裡正在跑的本魚和控制面板（只找 python，不會動到別的程式），更新或解除安裝時檔案才不會被占用 }
procedure StopBluefish;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like ''python*'' -and $_.CommandLine -and $_.CommandLine.Contains(''' +
    ExpandConstant('{app}') + ''') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopBluefish;
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := '正在下載並設定本魚專用的 Python（第一次要等 1～3 分鐘）…';
    WizardForm.ProgressGauge.Style := npbstMarquee;
    PythonOK := Exec(ExpandConstant('{cmd}'), '/c ""' + ExpandConstant('{app}\bootstrap.cmd') + '""',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
    WizardForm.ProgressGauge.Style := npbstNormal;
    if not PythonOK then
      MsgBox('準備 Python 失敗了（代碼 ' + IntToStr(ResultCode) + '）。' + #13#10 +
        '請確認電腦有連上網路，再執行一次安裝程式。' + #13#10#13#10 +
        '詳細紀錄在：' + ExpandConstant('{app}\install-log.txt'), mbError, MB_OK);
  end;
end;

function PythonReady: Boolean;
begin
  Result := PythonOK;
end;

{ 路徑超過 260 字的檔案（裝在很深的資料夾時，Python 裡會有）一般方法刪不掉，用 PowerShell 的長路徑寫法補刪 }
procedure RemoveLongPaths;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -Command "foreach ($d in ''python'', ''.venv'') { $p = ''\\?\' + ExpandConstant('{app}') +
    '\'' + $d; if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue } }"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopBluefish;
  if CurUninstallStep = usPostUninstall then
    RemoveLongPaths;
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}')) then
    if MsgBox('要一起刪除本魚的設定和資料嗎？' + #13#10 +
      '（Discord Token、AI 金鑰、等級紀錄、知識庫…）' + #13#10#13#10 +
      '之後想重新安裝繼續用的話，請按「否」。', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{app}'), True, True, True);
end;
