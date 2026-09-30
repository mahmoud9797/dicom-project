# Run once in an Administrator PowerShell on a PC that hosts DicomExtractor.
# Allows only TCP port 8000 from the local/VPN networks below.
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an Administrator PowerShell window.'
}

$pythonExe = (& python -c 'import sys; print(sys.executable)').Trim()
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Python was not found on PATH. Install Python and retry.'
}

$ruleName = 'DicomExtractor TCP 8000 LAN'
$allowedNetworks = @('192.168.1.0/24', '192.168.10.0/24', '172.30.30.0/24')
$existing = Get-NetFirewallRule -PolicyStore PersistentStore -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule -PolicyStore PersistentStore -DisplayName $ruleName `
        -Direction Inbound -Action Allow -Enabled True -Profile Private,Public `
        -Protocol TCP -LocalPort 8000 -Program $pythonExe `
        -RemoteAddress $allowedNetworks | Out-Null
} else {
    Write-Host "Existing $ruleName rule found; leaving it unchanged."
}

# A broader block rule for this same Python program takes precedence over the
# narrow allow rule. Disable only local inbound TCP block rules for this binary.
$blockRules = Get-NetFirewallRule -PolicyStore PersistentStore |
    Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' }
foreach ($rule in $blockRules) {
    $application = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule
    $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule
    if ($application.Program -ieq $pythonExe -and $port.Protocol -eq 'TCP') {
        Disable-NetFirewallRule -InputObject $rule
        Write-Host "Disabled broad inbound TCP block rule: $($rule.DisplayName)"
    }
}

Write-Host "LAN firewall setup complete for $pythonExe on TCP port 8000."
Write-Host "Allowed client networks: $($allowedNetworks -join ', ')"
Write-Host 'Open http://<this PC IPv4 address>:8000/ from a device on an allowed network.'
