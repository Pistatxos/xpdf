; Instalador de xpdf para Windows (Inno Setup 6).
; Lo invoca build.py con: /DAppVersion=x.y.z /DSourceDir=... /DOutputDir=... /DRootDir=...
;
; - Instalación por usuario, sin permisos de administrador (%LOCALAPPDATA%\Programs\xpdf).
; - Accesos directos en el menú Inicio y, si se elige, en el escritorio.
; - «Abrir con xpdf» para PDF, sin cambiar el lector predeterminado.
; - Actualizaciones: xpdf lanza este instalador con /VERYSILENT /CLOSEAPPLICATIONS
;   /RELAUNCH=1; se cierra la app abierta y se vuelve a abrir al terminar.

#ifndef AppVersion
  #error Falta /DAppVersion
#endif

[Setup]
; AppId identifica la instalación para actualizar y desinstalar: no cambiarlo nunca.
AppId={{6F1B2C3D-8E4A-4F5B-9C7D-0A1B2C3D4E5F}
AppName=xpdf
AppVersion={#AppVersion}
AppVerName=xpdf {#AppVersion}
AppPublisher=xpdf
AppComments=Editor de PDF de escritorio, minimalista
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\xpdf
DefaultGroupName=xpdf
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=xpdf-{#AppVersion}-windows-x64-setup
SetupIconFile={#RootDir}\assets\icons\app.ico
UninstallDisplayIcon={app}\xpdf.exe
UninstallDisplayName=xpdf
LicenseFile={#RootDir}\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ChangesAssociations=yes

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[InstallDelete]
; Las versiones nuevas sustituyen por completo la carpeta de la anterior.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\xpdf"; Filename: "{app}\xpdf.exe"
Name: "{autodesktop}\xpdf"; Filename: "{app}\xpdf.exe"; Tasks: desktopicon

[Registry]
; «Abrir con» para PDF, sin tocar la aplicación predeterminada (HKCU: sin administrador).
Root: HKCU; Subkey: "Software\Classes\xpdf.pdf"; ValueType: string; ValueName: ""; ValueData: "Documento PDF"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\xpdf.pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\xpdf.exe,0"
Root: HKCU; Subkey: "Software\Classes\xpdf.pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\xpdf.exe"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "xpdf.pdf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\Applications\xpdf.exe\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""; Flags: uninsdeletekey

[Run]
; Instalación normal: casilla «Abrir xpdf» al terminar.
Filename: "{app}\xpdf.exe"; Description: "Abrir xpdf"; Flags: nowait postinstall skipifsilent
; Actualización desde la propia app: reabrirla al terminar.
Filename: "{app}\xpdf.exe"; Flags: nowait skipifnotsilent; Check: ShouldRelaunch

[Code]
function ShouldRelaunch(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
