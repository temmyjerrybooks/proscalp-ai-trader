param(
    [Parameter(Mandatory = $true)]
    [string]$HostName,

    [string]$User = "ubuntu",

    [Parameter(Mandatory = $true)]
    [string]$KeyPath,

    [string]$RemoteDir = "/opt/proscalp-ai-trader",

    [switch]$CopyEnv
)

$ErrorActionPreference = "Stop"

function Require-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing command '$Name'. Install or enable it before deployment."
    }
}

Require-Command ssh
Require-Command scp
Require-Command tar

if (-not (Test-Path $KeyPath)) {
    throw "SSH key not found: $KeyPath"
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$archive = Join-Path $env:TEMP "proscalp-ai-trader-deploy.tar.gz"

Write-Host "Creating deployment archive..." -ForegroundColor Cyan
if (Test-Path $archive) {
    Remove-Item -LiteralPath $archive -Force
}

$repoRootPath = $repoRoot.Path
$fileList = Join-Path $env:TEMP "proscalp-ai-trader-files.txt"
$includedRoots = @("backend", "frontend", "config", "docs", "nginx", "scripts", "systemd")
$includedFiles = @(".env.example", ".gitignore", "README.md", "docker-compose.yml", "docker-compose.prod.yml")

if ($CopyEnv) {
    $includedFiles += ".env"
    Write-Host "Copying .env to the VM. Make sure this is intentional." -ForegroundColor Yellow
} else {
    Write-Host "Not copying .env. You will create/edit it on the VM." -ForegroundColor Yellow
}

$blockedSegments = @("node_modules", "dist", "__pycache__", ".pytest_cache", ".venv")
$files = New-Object System.Collections.Generic.List[string]

foreach ($relativeFile in $includedFiles) {
    $fullPath = Join-Path $repoRootPath $relativeFile
    if (Test-Path $fullPath) {
        $files.Add($relativeFile.Replace("\", "/"))
    }
}

foreach ($root in $includedRoots) {
    $fullRoot = Join-Path $repoRootPath $root
    if (-not (Test-Path $fullRoot)) {
        continue
    }

    Get-ChildItem -Path $fullRoot -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object {
        $relative = $_.FullName.Substring($repoRootPath.Length).TrimStart("\", "/")
        $segments = $relative -split "[\\/]+"
        $isBlocked = $false

        foreach ($segment in $segments) {
            if ($blockedSegments -contains $segment) {
                $isBlocked = $true
                break
            }
        }

        if ($_.Extension -in @(".pyc", ".pyo") -or $isBlocked) {
            return
        }

        $files.Add($relative.Replace("\", "/"))
    }
}

$files | Sort-Object -Unique | Set-Content -Path $fileList -Encoding ascii

& tar -czf $archive -C $repoRootPath -T $fileList
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create deployment archive. Check file permissions on generated cache/build folders and retry."
}

$target = "$User@$HostName"
Write-Host "Uploading archive to $target..." -ForegroundColor Cyan
& scp -i $KeyPath $archive "${target}:/tmp/proscalp-ai-trader-deploy.tar.gz"

$remoteCommand = @"
set -euo pipefail
sudo mkdir -p '$RemoteDir'
sudo chown -R '$User':'$User' '$RemoteDir'
tar -xzf /tmp/proscalp-ai-trader-deploy.tar.gz -C '$RemoteDir'
cd '$RemoteDir'
chmod +x scripts/*.sh
env_was_present=0
if [ -f .env ]; then
  env_was_present=1
fi
bash scripts/setup.sh
if [ "`$env_was_present" -eq 0 ]; then
  echo 'Created .env on VM. Edit it before deploying containers.'
  exit 20
fi
bash scripts/deploy_oracle.sh
"@

Write-Host "Running remote setup/deploy..." -ForegroundColor Cyan
& ssh -i $KeyPath $target $remoteCommand
$exitCode = $LASTEXITCODE

if ($exitCode -eq 20) {
    Write-Host ""
    Write-Host "Project uploaded and server prepared. Now SSH in, edit .env, then deploy:" -ForegroundColor Yellow
    Write-Host "ssh -i `"$KeyPath`" $target"
    Write-Host "cd $RemoteDir"
    Write-Host "nano .env"
    Write-Host "bash scripts/deploy_oracle.sh"
    exit 0
}

if ($exitCode -ne 0) {
    exit $exitCode
}

Write-Host ""
Write-Host "Deployment command finished." -ForegroundColor Green
Write-Host "Open: http://$HostName"
Write-Host "Health: http://$HostName/health"
