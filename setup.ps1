# One-time setup on each Windows laptop. Works with NO internet:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    Write-Host "This laptop is $($env:PROCESSOR_ARCHITECTURE). The codec needs an x86-64 (Intel/AMD) CPU; ARM laptops compute different numbers and cannot decode." -ForegroundColor Red
    exit 1
}

# ---- Python 3.11 / 3.12 / 3.13 (the bundled offline packages cover exactly these)
$py = $null
foreach ($v in @("3.12", "3.11", "3.13")) {
    try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {}
}
if (-not $py) {
    try {
        & python -c "import sys; assert (3, 11) <= sys.version_info[:2] <= (3, 13)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = @("python") }
    } catch {}
}
if (-not $py) {
    Write-Host "Need Python 3.11, 3.12 or 3.13. Install one (python.org installer or: winget install -e --id Python.Python.3.12), open a NEW PowerShell, rerun." -ForegroundColor Red
    exit 1
}
Write-Host "Using $($py -join ' ')"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if ($py.Count -gt 1) { & $py[0] $py[1] -m venv .venv } else { & $py[0] -m venv .venv }
    if ($LASTEXITCODE -ne 0) { Write-Host "could not create .venv" -ForegroundColor Red; exit 1 }
}

# ---- packages: from the bundled wheels\ folder (offline); internet only as a fallback
Write-Host "Installing packages from wheels\ (offline)..."
& .venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.txt -q --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Write-Host "Offline install failed; trying the internet..." -ForegroundColor Yellow
    & .venv\Scripts\python.exe -m pip install -r requirements.txt -q --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 1 }
}
Write-Host "packages OK" -ForegroundColor Green

# ---- model: models\ -> Downloads\ -> any removable drive -> internet. Always hash-checked.
$model = "models\SmolLM2-360M.Q8_0.gguf"
$want = "8e0b159d81bce47993580827a26ec01e64092edd55165b6076e6fbfa37995eb4"
# Exact file: mradermacher's upload. QuantFactory's file has the SAME NAME but different bytes and will not decode.
$url = "https://huggingface.co/mradermacher/SmolLM2-360M-GGUF/resolve/main/SmolLM2-360M.Q8_0.gguf"
New-Item -ItemType Directory -Force -Path models | Out-Null
if (-not (Test-Path $model)) {
    $cands = @((Join-Path $env:USERPROFILE "Downloads\SmolLM2-360M.Q8_0.gguf"))
    Get-PSDrive -PSProvider FileSystem | ForEach-Object {
        $cands += (Join-Path $_.Root "SmolLM2-360M.Q8_0.gguf")
        $cands += (Join-Path $_.Root "meshchat\models\SmolLM2-360M.Q8_0.gguf")
    }
    $src = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($src) {
        Write-Host "Copying model from $src"
        Copy-Item $src $model
    } else {
        Write-Host "Model not found locally; downloading 386 MB from $url"
        try {
            if (Get-Command curl.exe -ErrorAction SilentlyContinue) { & curl.exe -L --fail -o $model $url }
            else { $ProgressPreference = "SilentlyContinue"; Invoke-WebRequest -Uri $url -OutFile $model }
        } catch {}
    }
}
if (Test-Path $model) {
    $h = (Get-FileHash $model -Algorithm SHA256).Hash.ToLower()
    if ($h -eq $want) { Write-Host "model SHA-256 OK" -ForegroundColor Green }
    else {
        Write-Host "model SHA-256 $h is WRONG (want $want). Renamed to .bad; delete it and rerun." -ForegroundColor Red
        Move-Item -Force $model "$model.bad"
    }
} else {
    Write-Host "NO MODEL. Copy SmolLM2-360M.Q8_0.gguf (from the other laptop's meshchat\models\) by USB stick into $PSScriptRoot\models\ and rerun." -ForegroundColor Red
}

Write-Host "`n--- tests (simulated radios, no hardware needed) ---"
& .venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider

if (Test-Path $model) {
    Write-Host "`n--- codec check (must say fingerprint 422ac23fca10 and PASS) ---"
    & .venv\Scripts\python.exe meshchat.py check
}

Write-Host "`nRadio serial ports on this laptop:"
& .venv\Scripts\python.exe -m serial.tools.list_ports -v
Write-Host "`nNext: SETUP.md stage 3 (radio prep)."
