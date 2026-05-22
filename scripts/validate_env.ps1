param(
    [string]$EnvPath = ".env"
)

if (-not (Test-Path $EnvPath)) {
    Write-Error "Missing $EnvPath. Copy .env.example to .env first."
    exit 1
}

$required = @(
    "DATABASE_URL",
    "EXCHANGE",
    "TRADING_MODE",
    "MARKET_TYPE",
    "LIVE_TRADING_ENABLED",
    "FUTURES_TRADING_CONFIRMED",
    "USER_TIMEZONE"
)

$optionalSecrets = @(
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID"
)

$envMap = @{}
Get-Content $EnvPath | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $parts = $_ -split '=', 2
    $envMap[$parts[0].Trim()] = $parts[1].Trim().Trim('"')
}

Write-Host "ProScalp AI Trader .env readiness" -ForegroundColor Cyan
Write-Host ""

foreach ($key in $required) {
    $value = $envMap[$key]
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "[MISSING] $key" -ForegroundColor Red
    } else {
        Write-Host "[OK]      $key = $value" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "External secret status, values hidden:" -ForegroundColor Cyan
foreach ($key in $optionalSecrets) {
    $value = $envMap[$key]
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "[BLANK]   $key" -ForegroundColor Yellow
    } else {
        Write-Host "[SET]     $key = ***redacted***" -ForegroundColor Green
    }
}

Write-Host ""
$mode = $envMap["TRADING_MODE"]
$exchange = $envMap["EXCHANGE"]
if ($mode -eq "paper") {
    Write-Host "Paper mode: exchange private keys are optional. Public market data and the scanner can run without keys." -ForegroundColor Green
} elseif ($mode -eq "testnet") {
    Write-Host "Testnet mode: configure the selected exchange API key and secret." -ForegroundColor Yellow
} elseif ($mode -match "live") {
    Write-Host "Live mode: LIVE_TRADING_ENABLED must be true. Futures also needs FUTURES_TRADING_CONFIRMED=true." -ForegroundColor Red
}

if ($exchange -eq "binance" -and ([string]::IsNullOrWhiteSpace($envMap["BINANCE_API_KEY"]) -or [string]::IsNullOrWhiteSpace($envMap["BINANCE_API_SECRET"]))) {
    Write-Host "Selected exchange is Binance, but Binance private keys are blank." -ForegroundColor Yellow
}

if ($exchange -eq "bybit" -and ([string]::IsNullOrWhiteSpace($envMap["BYBIT_API_KEY"]) -or [string]::IsNullOrWhiteSpace($envMap["BYBIT_API_SECRET"]))) {
    Write-Host "Selected exchange is Bybit, but Bybit private keys are blank." -ForegroundColor Yellow
}

if ([string]::IsNullOrWhiteSpace($envMap["TELEGRAM_BOT_TOKEN"]) -or [string]::IsNullOrWhiteSpace($envMap["TELEGRAM_CHAT_ID"])) {
    Write-Host "Telegram alerts are not connected yet." -ForegroundColor Yellow
}
