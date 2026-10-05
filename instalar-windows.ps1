# ============================================================================
#  Instalador de norlex-ia para Windows (un solo paso en el servidor)
#
#  Uso (PowerShell, preferente como administrador):
#    powershell -ExecutionPolicy Bypass -File .\instalar-windows.ps1
#
#  Qué hace:
#    1. Verifica Python 3.10+ (te dice cómo instalarlo si falta)
#    2. Crea el entorno virtual .venv en esta carpeta
#    3. Instala las dependencias (fastapi, uvicorn, groq, pyodbc...)
#    4. Crea el .env desde .env.example si no existe (EDÍTALO: GROQ_API_KEY)
#    5. Prueba que la app arranca y responde /health
#
#  Después de este paso, la app .NET levanta norlex-ia sola al arrancar.
# ============================================================================

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $raiz

function Paso($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "    OK  $msg" -ForegroundColor Green }
function Fallo($msg) { Write-Host "    X   $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "==============================================" -ForegroundColor DarkCyan
Write-Host "  Instalador de norlex-ia (IA del chatbot)"      -ForegroundColor DarkCyan
Write-Host "  Carpeta: $raiz"                                 -ForegroundColor DarkCyan
Write-Host "==============================================" -ForegroundColor DarkCyan
Write-Host ""

# ── 1) Python ───────────────────────────────────────────────────────────
Paso "Buscando Python 3.10 o superior..."

$py = $null
foreach ($candidato in @("py", "python", "python3")) {
    try {
        $v = & $candidato --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match "3\.(\d+)") {
            if ([int]$Matches[1] -ge 10) { $py = $candidato; break }
        }
    } catch { }
}

if (-not $py) {
    Fallo "No encontré Python 3.10+."
    Write-Host ""
    Write-Host "  Instálalo así:" -ForegroundColor Yellow
    Write-Host "    winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host "  (o descárgalo de https://www.python.org/downloads/ marcando"
    Write-Host "   'Add python.exe to PATH' en el instalador)"
    Write-Host ""
    Write-Host "  Luego vuelve a ejecutar este script."
    exit 1
}
Ok "$( & $py --version )"

# ── 2) Entorno virtual ──────────────────────────────────────────────────
# Un .venv copiado desde otra máquina está roto (apunta al Python de allá):
# no basta con que exista, hay que probar que funcione.
$venvOk = $false
if (Test-Path "$raiz\.venv\Scripts\python.exe") {
    & "$raiz\.venv\Scripts\python.exe" --version *> $null
    if ($LASTEXITCODE -eq 0) { $venvOk = $true }
}

if ($venvOk) {
    Paso "El entorno virtual ya existe y funciona. Reutilizando."
} else {
    if (Test-Path "$raiz\.venv") {
        Paso "El .venv existe pero está roto (¿copiado de otra máquina?). Recreándolo..."
        Remove-Item -Recurse -Force "$raiz\.venv"
    } else {
        Paso "Creando el entorno virtual .venv..."
    }
    & $py -m venv "$raiz\.venv"
    if ($LASTEXITCODE -ne 0) { Fallo "No se pudo crear el venv."; exit 1 }
    Ok ".venv creado"
}
$pythonVenv = "$raiz\.venv\Scripts\python.exe"

# ── 3) Dependencias ─────────────────────────────────────────────────────
Paso "Instalando dependencias (puede tardar 1-2 minutos)..."
& $pythonVenv -m pip install --disable-pip-version-check -q --upgrade pip
& $pythonVenv -m pip install --disable-pip-version-check -q -r "$raiz\requirements.txt"
if ($LASTEXITCODE -ne 0) { Fallo "Falló pip install. Revisa conexión a internet."; exit 1 }
Ok "Dependencias instaladas"

# ── 4) Archivo .env ─────────────────────────────────────────────────────
if (Test-Path "$raiz\.env") {
    Paso "El .env ya existe. NO lo toco."
} else {
    Paso "Creando .env desde .env.example..."
    Copy-Item "$raiz\.env.example" "$raiz\.env"
    Ok ".env creado"
    Write-Host ""
    Write-Host "  !! ACCIÓN REQUERIDA !!" -ForegroundColor Yellow
    Write-Host "  Edita $raiz\.env y coloca al menos:"           -ForegroundColor Yellow
    Write-Host "    GROQ_API_KEY=gsk_...            (tu llave de https://console.groq.com)" -ForegroundColor Yellow
    Write-Host "    NORLEX_IA_SHARED_SECRET=...     (el MISMO de Norlex:Ia:SharedSecret en appsettings del sitio)" -ForegroundColor Yellow
    Write-Host ""
}

# ── 5) Prueba de arranque ───────────────────────────────────────────────
if (-not (Test-Path "$raiz\.env")) {
    Write-Host "Sin .env no puedo probar el arranque. Instalación a medias." -ForegroundColor Yellow
    exit 0
}

$envGrok = (Select-String -Path "$raiz\.env" -Pattern "^GROQ_API_KEY=gsk_pon" -Quiet)
if ($envGrok) {
    Write-Host "  !! Falta la GROQ_API_KEY real en .env; salto la prueba de arranque." -ForegroundColor Yellow
    exit 0
}

Paso "Probando arranque en el puerto 8099..."
$proceso = Start-Process -FilePath $pythonVenv `
    -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8099" `
    -WorkingDirectory $raiz -PassThru -WindowStyle Hidden

$salud = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 600
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8099/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $salud = $true; break }
    } catch { }
    if ($proceso.HasExited) { break }
}

if ($salud) {
    Ok "norlex-ia arrancó y responde /health"
    try { (Invoke-WebRequest -Uri "http://127.0.0.1:8099/health" -UseBasicParsing).Content } catch { }
    Stop-Process -Id $proceso.Id -Force -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "  INSTALACIÓN COMPLETA ✔" -ForegroundColor Green
    Write-Host "  Ahora reinicia el sitio en IIS (o la app) y el chatbot usará la IA." -ForegroundColor Green
} else {
    Fallo "El servicio no respondió /health. Causas comunes:"
    Write-Host "    - Falta el 'ODBC Driver 18 for SQL Server':"
    Write-Host "        winget install Microsoft.ODBCDriverforSQLServer"
    Write-Host "    - NORLEX_DB_SERVER en .env no coincide con el SQL de este servidor"
    Write-Host "    - La BD BD_Fed no existe en ese servidor o el usuario no tiene permisos"
    if (-not $proceso.HasExited) { Stop-Process -Id $proceso.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}
