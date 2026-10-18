# Instala Jarvis no PC pessoal, sem conectar aos servidores corporativos.
param([switch]$NoLaunch)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Set-Location $PSScriptRoot
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    & py -3.14 -m venv (Join-Path $root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Python 3.14 nao encontrado" }
}
Write-Host "[1/5] Instalando bibliotecas do Jarvis..."
& $py -m pip install --disable-pip-version-check -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar bibliotecas" }
$modelPath = Join-Path $root "models\vosk-model-small-pt-0.3"
if (-not (Test-Path (Join-Path $modelPath "conf\model.conf"))) {
    Write-Host "[2/5] Baixando reconhecimento de voz portugues..."
    $folder = Join-Path $root "models"
    New-Item -ItemType Directory -Force $folder | Out-Null
    $file = Join-Path $env:TEMP "jarvis-vosk-pt.zip"
    Invoke-WebRequest -UseBasicParsing "https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip" -OutFile $file
    Expand-Archive -LiteralPath $file -DestinationPath $folder -Force
    Remove-Item $file -Force
}
Write-Host "[3/5] Preparando Ollama local..."
$ollamaCmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
$ollama = if ($ollamaCmd) { $ollamaCmd.Source } else { Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) {
    Write-Host "Instalando Ollama (gratuito)..."
    & winget install -e --id Ollama.Ollama --source winget --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Falha na instalacao do Ollama" }
    $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ollama)) { throw "Ollama nao localizado apos a instalacao" }
}
$live = $false
try {
    Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
    $live = $true
} catch {}
if (-not $live) {
    Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
    for ($n = 0; $n -lt 50; $n++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null
            $live = $true
            break
        } catch {}
    }
}
if (-not $live) { throw "Ollama local nao iniciou na porta 11434" }
Write-Host "[4/5] Baixando modelo Qwen 2.5 1.5B..."
& $ollama pull qwen2.5:1.5b
if ($LASTEXITCODE -ne 0) { throw "Falha no download do modelo de linguagem" }
Write-Host "[5/5] Validando Jarvis..."
& $py (Join-Path $root "jarvis.py") --check
if ($LASTEXITCODE -ne 0) { throw "Jarvis nao passou na validacao" }
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
New-Item -Path $regKey -Force | Out-Null
$launcher = '"' + $pythonw + '" "' + (Join-Path $root "jarvis.py") + '" --minimized'
Set-ItemProperty -Path $regKey -Name "JarvisPessoal" -Value $launcher
if (-not $NoLaunch) {
    Start-Process -FilePath $pythonw -ArgumentList ('"' + (Join-Path $root "jarvis.py") + '"') -WorkingDirectory $root
}
Write-Host "JARVIS_INSTALL_OK"
