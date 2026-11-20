param([switch]$ResetToken)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$tokenFile = Join-Path $PSScriptRoot '.discord-token'

if (!(Test-Path -LiteralPath $python)) {
    throw 'The local Python environment is missing. Follow the installation steps in README.md.'
}
try {
    if ($ResetToken -or !$env:DISCORD_BOT_TOKEN) {
        Add-Type -AssemblyName System.Security
        $scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        $tokenBytes = $null
        try {
            if (!$ResetToken -and (Test-Path -LiteralPath $tokenFile)) {
                # Existing launchers also used Windows DPAPI with UTF-16LE and hex storage.
                $hex = (Get-Content -Raw -LiteralPath $tokenFile).Trim()
                if ($hex -notmatch '\A(?:[0-9a-fA-F]{2})+\z') { throw 'Saved token is damaged. Run Start Bot.cmd -ResetToken to replace it.' }
                $encrypted = [byte[]]::new($hex.Length / 2)
                for ($i = 0; $i -lt $encrypted.Length; $i++) {
                    $encrypted[$i] = [Convert]::ToByte($hex.Substring(2 * $i, 2), 16)
                }
                $tokenBytes = [System.Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, $scope)
                $env:DISCORD_BOT_TOKEN = [Text.Encoding]::Unicode.GetString($tokenBytes)
            } else {
                Write-Host 'Open https://discord.com/developers/applications and create an application for this bot.'
                Write-Host 'In Bot settings, choose Reset Token or Copy Token. Paste the bot token below.'
                Write-Host 'Your input is hidden. Windows encrypts the saved token for your account on this PC.'
                $secret = Read-Host 'Discord bot token' -AsSecureString
                if (!$secret.Length) { throw 'A Discord bot token is required.' }
                $credential = [PSCredential]::new('discord-bot', $secret)
                $env:DISCORD_BOT_TOKEN = $credential.GetNetworkCredential().Password
                $tokenBytes = [Text.Encoding]::Unicode.GetBytes($env:DISCORD_BOT_TOKEN)
                $encrypted = [System.Security.Cryptography.ProtectedData]::Protect($tokenBytes, $null, $scope)
                $hex = [BitConverter]::ToString($encrypted).Replace('-', '')
                Set-Content -LiteralPath "$tokenFile.tmp" -Value $hex -Encoding Ascii
                Move-Item -LiteralPath "$tokenFile.tmp" -Destination $tokenFile -Force
            }
        } finally {
            if ($null -ne $tokenBytes) { [Array]::Clear($tokenBytes, 0, $tokenBytes.Length) }
        }
    }
    $env:PYTHONUTF8 = '1'
    & $python bot.py
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:DISCORD_BOT_TOKEN -ErrorAction SilentlyContinue
}
