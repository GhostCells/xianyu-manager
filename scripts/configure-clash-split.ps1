param(
    [string]$ClashDataRoot = "D:\study\clash\Clash-for-Windows-0.20.8-x64-CN\Clash\Data",
    [string]$ActiveProfile = ""
)

$ErrorActionPreference = "Stop"
$ProfilesRoot = Join-Path $ClashDataRoot "profiles"
$ProfileListPath = Join-Path $ProfilesRoot "list.yml"
$ProfilePath = ""
$SettingsPath = Join-Path $ClashDataRoot "cfw-settings.yaml"
$Marker = "# Codex-Xianyu split rules"
$DirectGroup = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String("8J+OryDlhajnkIPnm7Tov54=")
)
$ProxyGroup = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String("8J+agCDoioLngrnpgInmi6k=")
)

if ([string]::IsNullOrWhiteSpace($ActiveProfile)) {
    if (-not (Test-Path -LiteralPath $ProfileListPath)) {
        throw "Clash profile list not found: $ProfileListPath"
    }
    $ListText = [System.IO.File]::ReadAllText($ProfileListPath, [System.Text.Encoding]::UTF8)
    $IndexMatch = [regex]::Match($ListText, '(?m)^index:\s*(\d+)\s*$')
    $ProfileMatches = [regex]::Matches($ListText, '(?m)^\s+(?:-\s+)?time:\s*([^\s]+\.yml)\s*$')
    if (-not $IndexMatch.Success -or $ProfileMatches.Count -eq 0) {
        throw "Could not identify the active Clash profile from list.yml."
    }
    $ActiveIndex = [int]$IndexMatch.Groups[1].Value
    if ($ActiveIndex -lt 0 -or $ActiveIndex -ge $ProfileMatches.Count) {
        throw "The active Clash profile index is out of range: $ActiveIndex"
    }
    $ActiveProfile = $ProfileMatches[$ActiveIndex].Groups[1].Value
}

$ProfilePath = Join-Path $ProfilesRoot $ActiveProfile
if (-not (Test-Path -LiteralPath $ProfilePath)) {
    throw "Active Clash profile not found: $ProfilePath"
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$ProfileBackup = "$ProfilePath.codex-backup-$Timestamp"
Copy-Item -LiteralPath $ProfilePath -Destination $ProfileBackup

$ProfileText = [System.IO.File]::ReadAllText($ProfilePath, [System.Text.Encoding]::UTF8)
if (-not $ProfileText.Contains($Marker)) {
    $Rules = @"
rules:
    $Marker
    - 'DOMAIN-SUFFIX,goofish.com,$DirectGroup'
    - 'DOMAIN-SUFFIX,dingtalk.com,$DirectGroup'
    - 'DOMAIN-SUFFIX,taobao.com,$DirectGroup'
    - 'DOMAIN-SUFFIX,alicdn.com,$DirectGroup'
    - 'DOMAIN-SUFFIX,baidu.com,$DirectGroup'
    - 'DOMAIN-SUFFIX,baidupcs.com,$DirectGroup'
    - 'DOMAIN-SUFFIX,openai.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,chatgpt.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,oaistatic.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,oaiusercontent.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,oaistatsig.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,openaimerge.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,workos.com,$ProxyGroup'
    - 'DOMAIN-SUFFIX,workoscdn.com,$ProxyGroup'
    - 'DOMAIN,challenges.cloudflare.com,$ProxyGroup'
"@
    $RulesPattern = [System.Text.RegularExpressions.Regex]::new('(?m)^rules:\s*\r?\n')
    $Updated = $RulesPattern.Replace($ProfileText, ($Rules.TrimEnd() + "`r`n"), 1)
    if ($Updated -eq $ProfileText) {
        throw "Could not find the rules section in the active Clash profile."
    }
    [System.IO.File]::WriteAllText($ProfilePath, $Updated, [System.Text.UTF8Encoding]::new($false))
}

if (Test-Path -LiteralPath $SettingsPath) {
    $SettingsBackup = "$SettingsPath.codex-backup-$Timestamp"
    Copy-Item -LiteralPath $SettingsPath -Destination $SettingsBackup
    $SettingsText = [System.IO.File]::ReadAllText($SettingsPath, [System.Text.Encoding]::UTF8)
    if ($SettingsText -match '(?m)^hideAfterStartup\s*:') {
        $SettingsText = [regex]::Replace($SettingsText, '(?m)^hideAfterStartup\s*:.*$', 'hideAfterStartup: true')
    } else {
        $SettingsText += "`r`nhideAfterStartup: true`r`n"
    }
    [System.IO.File]::WriteAllText($SettingsPath, $SettingsText, [System.Text.UTF8Encoding]::new($false))
}

Write-Host "Clash split rules configured." -ForegroundColor Green
Write-Host "Active profile: $ActiveProfile"
Write-Host "Profile backup: $ProfileBackup"
