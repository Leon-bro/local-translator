# Builds the UI and the server into translator-ui.exe (React is embedded into the binary).
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 scripts without a BOM.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Push-Location frontend
try {
    if (-not (Test-Path node_modules)) {
        npm install
        if ($LASTEXITCODE) { throw "npm install failed" }
    }
    npm run build
    if ($LASTEXITCODE) { throw "npm run build failed" }
} finally { Pop-Location }
# Vite empties backend/dist; keep the placeholder so go:embed works on a fresh clone.
New-Item -ItemType File -Force backend\dist\.gitkeep | Out-Null

go -C backend build -o ..\translator-ui.exe .
if ($LASTEXITCODE) { throw "go build failed" }
Write-Host "Done: .\translator-ui.exe"
