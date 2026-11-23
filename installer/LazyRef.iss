[Setup]
AppId={{3A431192-873E-48C8-B6B5-060DB4639141}
AppName=LazyRef
AppVersion={#AppVersion}
AppPublisher=LazyRef
DefaultDirName={localappdata}\Programs\LazyRef
DefaultGroupName=LazyRef
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
MinVersion=10.0.19045
SetupArchitecture=x64
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
SetupIconFile={#Payload}\LazyRef.ico
UninstallDisplayIcon={app}\LazyRef.ico
OutputDir={#OutputDir}
OutputBaseFilename=LazyRef-{#AppVersion}-Windows-x64-Setup
VersionInfoVersion={#AppVersion}

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LazyRef"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-B -X utf8 ""{app}\app.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\LazyRef.ico"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD_PARTY_NOTICES.txt"

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-B -X utf8 ""{app}\app.pyw"""; WorkingDir: "{app}"; Description: "Launch LazyRef"; Flags: nowait postinstall skipifsilent
