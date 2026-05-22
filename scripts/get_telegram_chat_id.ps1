param(
    [string]$EnvPath = ".env"
)

if (-not (Test-Path $EnvPath)) {
    Write-Error "Missing $EnvPath"
    exit 1
}

$tokenLine = Get-Content $EnvPath | Where-Object { $_ -match '^TELEGRAM_BOT_TOKEN=' } | Select-Object -First 1
if (-not $tokenLine) {
    Write-Error "TELEGRAM_BOT_TOKEN is missing from $EnvPath"
    exit 1
}

$token = ($tokenLine -split '=', 2)[1].Trim().Trim('"')
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Error "TELEGRAM_BOT_TOKEN is blank. Add the token from BotFather first."
    exit 1
}

$url = "https://api.telegram.org/bot$token/getUpdates"
$response = Invoke-RestMethod -Uri $url -Method Get
$updates = $response.result

if (-not $updates -or $updates.Count -eq 0) {
    Write-Host "No Telegram updates found yet." -ForegroundColor Yellow
    Write-Host "Open your bot in Telegram, send it a message like 'hello', then run this script again."
    exit 0
}

$chats = @{}
foreach ($update in $updates) {
    $message = $update.message
    if (-not $message) { $message = $update.edited_message }
    if (-not $message) { continue }
    $chat = $message.chat
    if (-not $chat) { continue }
    $chats["$($chat.id)"] = [PSCustomObject]@{
        chat_id = $chat.id
        type = $chat.type
        title = if ($chat.title) { $chat.title } else { "$($chat.first_name) $($chat.last_name)".Trim() }
        username = $chat.username
    }
}

if ($chats.Count -eq 0) {
    Write-Host "No chat IDs found in updates. Send the bot a new message and run again." -ForegroundColor Yellow
    exit 0
}

$chats.Values | Format-Table -AutoSize
Write-Host ""
Write-Host "Copy the chat_id you want into .env as TELEGRAM_CHAT_ID=..." -ForegroundColor Cyan
