# Instalador de xpdf para Windows. Uso (en PowerShell):
#
#   irm https://raw.githubusercontent.com/Pistatxos/xpdf/main/scripts/install.ps1 | iex
#
# Descarga la última versión desde GitHub Releases, comprueba su SHA-256 contra
# SHA256SUMS.txt e instala para el usuario actual (sin administrador), con acceso
# directo en el escritorio y en el menú Inicio. Volver a ejecutarlo reinstala o
# actualiza. Opcional: $env:XPDF_VERSION = "0.3.0" para una versión concreta.
#
# Todo va dentro de un bloque y los errores se lanzan con «throw»: con «| iex»,
# un «exit» cerraría la ventana de PowerShell del usuario.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest es mucho más rápido así
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $Repo = if ($env:XPDF_REPO) { $env:XPDF_REPO } else { 'Pistatxos/xpdf' }
    $Headers = @{ 'User-Agent' = 'xpdf-installer' }

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'xpdf necesita Windows de 64 bits.'
    }

    if ($env:XPDF_VERSION) {
        $Tag = 'v' + $env:XPDF_VERSION.TrimStart('v')
    } else {
        Write-Host 'Buscando la última versión de xpdf...'
        $Tag = (Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $Headers).tag_name
    }
    $Version = $Tag.TrimStart('v')
    $Name = "xpdf-$Version-windows-x64-setup.exe"
    $Base = "https://github.com/$Repo/releases/download/$Tag"

    $Tmp = Join-Path $env:TEMP ("xpdf-install-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    try {
        $Setup = Join-Path $Tmp $Name
        $Sums = Join-Path $Tmp 'SHA256SUMS.txt'
        Write-Host "Descargando xpdf $Version..."
        Invoke-WebRequest -Uri "$Base/$Name" -OutFile $Setup -Headers $Headers -UseBasicParsing
        Invoke-WebRequest -Uri "$Base/SHA256SUMS.txt" -OutFile $Sums -Headers $Headers -UseBasicParsing

        $Line = Get-Content $Sums | Where-Object { ($_ -split '\s+')[-1].TrimStart('*') -eq $Name } | Select-Object -First 1
        if (-not $Line) { throw "SHA256SUMS.txt no incluye $Name." }
        $Expected = ($Line -split '\s+')[0].ToLower()
        $Actual = (Get-FileHash -Algorithm SHA256 -Path $Setup).Hash.ToLower()
        if ($Expected -ne $Actual) {
            throw 'La descarga no coincide con la huella publicada (SHA-256). No se instala nada.'
        }
        Write-Host 'Huella SHA-256 verificada. Instalando...'

        $Proc = Start-Process -FilePath $Setup -Wait -PassThru -ArgumentList @(
            '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', '/TASKS=desktopicon'
        )
        if ($Proc.ExitCode -ne 0) { throw "El instalador terminó con el código $($Proc.ExitCode)." }
    } finally {
        Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    }

    $Exe = Join-Path $env:LOCALAPPDATA 'Programs\xpdf\xpdf.exe'
    Write-Host "xpdf $Version instalado. Tienes el acceso directo en el escritorio y en el menú Inicio." -ForegroundColor Green
    if (Test-Path $Exe) { Start-Process -FilePath $Exe }
}
