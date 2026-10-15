; ============================================================================
; 视频工坊（VDL）Windows 安装程序 —— Inno Setup 6 脚本
; ============================================================================
; 由 desktop/build_win.sh 末尾调用（Git Bash 下路径经 MSYS 转换为 Windows 风格）：
;   iscc "/DAppVersion=1.0.68" "/DOutputDir=<repo>/dist" \
;        "/DOutputBaseFilename=VideoDownloader-Setup-1.0.68" \
;        "/DSourceDir=<repo>/dist/VideoDownloader" "/DIconFile=<repo>/desktop/icon.ico" \
;        desktop/installer.iss
;
; 产物：<repo>/dist/VideoDownloader-Setup-<AppVersion>.exe（单文件、LZMA2 压缩）
; 安装形态：PyInstaller onedir 整文件夹 → {app}\（Program Files\视频工坊）
;
; ⚠️ AppId 是升级识别锚点：一经发布【永不修改】。改它 = 老用户覆盖装会被当成
;    另一个软件，双份并存。
; ⚠️ 不要加 VersionInfo* 之外的自定义签名配置：当前无 EV 代码签名证书，
;    SmartScreen 会对新下载的安装包示警「Windows 保护了你的电脑」→ 用户点
;    「更多信息」→「仍要运行」即可，官网下载页已给出该说明。
; ============================================================================

; ---- 默认值（可被 build_win.sh 的 /D 覆盖；单独编译时用相对本文件路径）----
#define AppVersion "0.0.0"
#define OutputDir "..\dist"
#define OutputBaseFilename "VideoDownloader-Setup"
#define SourceDir "..\dist\VideoDownloader"
#define IconFile "..\desktop\icon.ico"

#define AppName "视频工坊"
#define AppExeName "VideoDownloader.exe"
#define AppPublisher "hanyuxz.top"

[Setup]
; 固定 AppId（升级锚点，勿改）
AppId={{A7F3E1C8-2D4B-4F6A-9E5C-1B8D7A3F6E2C}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableReadyPage=no
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 只发 64 位（PyInstaller 构建机为 win-x64；onnxruntime/curl_cffi 均只有 x64 wheel）
; ⚠️ 用 x64 而非 6.3+ 才有的 x64compatible，保证 CI 镜像里的 Inno 6.2 也能编译
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; 装 Program Files 需要 UAC 提权（标准做法，安装全程只弹一次）
PrivilegesRequired=admin
; 安装前自动关闭正在运行的实例（Inno 6.1+：检测占用 {app} 文件的进程并关闭，
; 否则 onedir 覆盖安装会因 exe 被占用而失败）
CloseApplications=yes
RestartApplications=no

[Languages]
; Inno 6 内置简体中文语言文件
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: checkedonce

[Files]
; onedir 整包（VideoDownloader.exe + _internal\ + bin\ffmpeg.exe + bin\deno.exe + bin\aria2c.exe + web\ + commentary\）
; ignoreversion = 覆盖安装时总是替换（不做版本/日期比较，保证拿到的是本次构建的文件）
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 安装完成后默认勾选「立即运行」（skipifsilent：静默安装时不弹）
Filename: "{app}\{#AppExeName}"; Description: "立即运行 {#AppName}"; \
    Flags: nowait postinstall skipifsilent

; 说明：不设 [UninstallDelete] —— 运行期数据（任务历史/配置/缓存）全部在用户目录
; （%USERPROFILE%\.video-downloader 等），不在 {app} 内；{app} 卸载即整目录删除，
; 无需额外清理规则（且 [UninstallDelete] 不支持通配符，写了也会编译告警）。
