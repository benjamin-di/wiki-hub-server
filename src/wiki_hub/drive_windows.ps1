# Wiki Hub network drive for Windows. Run in PowerShell (no administrator rights needed):
#   irm __WIKI_SERVER__/drive/windows | iex
# It signs in through your browser, saves this computer's own drive login in
# Credential Manager without displaying it, and maps the drive in Explorer.
# Nothing is installed. Windows PowerShell 5.1 and PowerShell 7 are supported.

$WikiDriveServer = '__WIKI_SERVER__'

function Assert-WikiDriveWindows {
    if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) {
        throw 'This command connects a Windows computer. Other systems can use wiki drive connect.'
    }
    if ($PSVersionTable.PSEdition -ne 'Core') {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
            [Net.SecurityProtocolType]::Tls12
    }
}

function Get-WikiDriveErrorBody($ErrorRecord) {
    if (-not $ErrorRecord.ErrorDetails -or -not $ErrorRecord.ErrorDetails.Message) { return $null }
    try { return $ErrorRecord.ErrorDetails.Message | ConvertFrom-Json } catch { return $null }
}

function Invoke-WikiDriveApi([string]$Method, [string]$Path, [string]$Token, $Body) {
    $request = @{ Method = $Method; Uri = $WikiDriveServer + $Path; UseBasicParsing = $true
                  Headers = @{ Authorization = "Bearer $Token" } }
    if ($null -ne $Body) {
        $request.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress))
        $request.ContentType = 'application/json'
    }
    try {
        return Invoke-RestMethod @request
    } catch {
        $detail = Get-WikiDriveErrorBody $_
        if ($detail -and $detail.error) { throw [string]$detail.error }
        throw "Wiki Hub could not be reached ($($_.Exception.Message))."
    }
}

function Open-WikiDriveBrowser([string]$Address) {
    try { Start-Process $Address } catch { }
}

function Get-WikiDriveToken {
    $config = Invoke-RestMethod -Uri "$WikiDriveServer/api/config" -UseBasicParsing
    $endpoints = Invoke-RestMethod -Uri ($config.issuer.TrimEnd('/') + '/.well-known/openid-configuration') `
        -UseBasicParsing
    $device = Invoke-RestMethod -Method Post -Uri $endpoints.device_authorization_endpoint -UseBasicParsing `
        -Body @{ client_id = $config.client_id; scope = $config.scope; resource = $config.resource }
    $address = $device.verification_uri_complete
    if (-not $address) { $address = $device.verification_uri }
    Write-Host "Approve this computer in your browser. If it did not open, go to $($device.verification_uri)"
    Write-Host "and enter the code $($device.user_code)."
    Open-WikiDriveBrowser $address
    $interval = 5
    if ($device.interval) { $interval = [Math]::Max(1, [int]$device.interval) }
    $lifetime = 600
    if ($device.expires_in) { $lifetime = [Math]::Min(600, [int]$device.expires_in) }
    $deadline = (Get-Date).AddSeconds($lifetime)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $interval
        try {
            $token = Invoke-RestMethod -Method Post -Uri $endpoints.token_endpoint -UseBasicParsing -Body @{
                grant_type = 'urn:ietf:params:oauth:grant-type:device_code'
                device_code = $device.device_code; client_id = $config.client_id; resource = $config.resource }
            if ($token.access_token) { return [string]$token.access_token }
        } catch {
            $detail = Get-WikiDriveErrorBody $_
            if (-not $detail) { throw "The sign-in service could not be reached ($($_.Exception.Message))." }
            if ($detail.error -eq 'authorization_pending') { continue }
            if ($detail.error -eq 'slow_down') { $interval += 5; continue }
            if ($detail.error -in @('access_denied', 'expired_token')) { throw 'Sign-in was declined or expired.' }
            throw 'The sign-in service could not complete sign-in.'
        }
    }
    throw 'Sign-in timed out.'
}

# Windows operations. Output from Windows tools is never displayed.

function Invoke-WikiDriveNet([string[]]$Arguments) {
    $output = & net.exe @Arguments 2>&1 | Out-String
    return @{ Output = $output; ExitCode = $LASTEXITCODE }
}

function Get-WikiDriveMappings {
    $found = @{}
    foreach ($line in ((Invoke-WikiDriveNet @('use')).Output -split "`r?`n")) {
        $letter = [regex]::Match($line, '(?<![\\\w])([A-Za-z]:)(?=\s)')
        $remote = [regex]::Match($line, '(\\\\\S+)')
        if ($letter.Success -and $remote.Success) { $found[$letter.Groups[1].Value.ToUpper()] = $remote.Groups[1].Value }
    }
    return $found
}

function Test-WikiDriveLetterInUse([string]$Letter) {
    return Test-Path -LiteralPath "$Letter\"
}

function Get-WikiDriveFreeLetter([string]$Preferred) {
    $used = Get-WikiDriveMappings
    foreach ($letter in @($Preferred) + @('WZYXVUTSRQPONMLKJIHGFED'.ToCharArray() | ForEach-Object { "${_}:" })) {
        if (-not $used.ContainsKey($letter) -and -not (Test-WikiDriveLetterInUse $letter)) { return $letter }
    }
    throw 'No free drive letter is available.'
}

function Mount-WikiDriveShare([string]$Letter, [string]$Unc) {
    $options = @('/persistent:yes')
    if ((Invoke-WikiDriveNet @('use', '/?')).Output -match 'REQUIREPRIVACY') {
        $options += @('/requireprivacy', '/requireintegrity')
    }
    $result = Invoke-WikiDriveNet (@('use', $Letter, $Unc) + $options)
    if ($result.ExitCode -ne 0) {
        throw "Windows could not map $Letter (net use exit $($result.ExitCode)). Check that this computer is on the home network or private route."
    }
}

function Dismount-WikiDriveShares([string[]]$Uncs) {
    $wanted = @($Uncs | Where-Object { $_ } | ForEach-Object { $_.ToLower() })
    $mappings = Get-WikiDriveMappings
    foreach ($letter in @($mappings.Keys)) {
        if ($wanted -contains $mappings[$letter].ToLower()) { Invoke-WikiDriveNet @('use', $letter, '/delete', '/y') | Out-Null }
    }
}

function Set-WikiDriveLabel([string]$Unc, [string]$Text) {
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2\' + $Unc.Replace('\', '#')
    if (-not (Test-Path -LiteralPath $key)) { New-Item -Path $key -Force | Out-Null }
    Set-ItemProperty -LiteralPath $key -Name '_LabelFromReg' -Value $Text
}

function Initialize-WikiDriveCredentialType {
    if ('WikiHubDriveCredential' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using FILETIME = System.Runtime.InteropServices.ComTypes.FILETIME;

public static class WikiHubDriveCredential {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct CREDENTIAL {
        public int Flags; public int Type; public string TargetName; public string Comment; public FILETIME LastWritten;
        public int CredentialBlobSize; public IntPtr CredentialBlob; public int Persist; public int AttributeCount;
        public IntPtr Attributes; public string TargetAlias; public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredWrite(ref CREDENTIAL credential, int flags);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredDelete(string target, int type, int flags);

    // A domain password credential is what Windows uses for SMB; it persists on this computer only.
    public static void Save(string target, string user, string password) {
        byte[] blob = System.Text.Encoding.Unicode.GetBytes(password);
        IntPtr buffer = Marshal.AllocHGlobal(blob.Length);
        try {
            Marshal.Copy(blob, 0, buffer, blob.Length);
            CREDENTIAL credential = new CREDENTIAL();
            credential.Type = 2; credential.TargetName = target; credential.UserName = user; credential.Persist = 2;
            credential.CredentialBlobSize = blob.Length; credential.CredentialBlob = buffer;
            if (!CredWrite(ref credential, 0)) {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
        } finally {
            for (int i = 0; i < blob.Length; i++) { Marshal.WriteByte(buffer, i, 0); blob[i] = 0; }
            Marshal.FreeHGlobal(buffer);
        }
    }

    public static void Delete(string target) { CredDelete(target, 2, 0); }
}
'@
}

function Save-WikiDriveCredential([string]$Target, [string]$User, [string]$Password) {
    Initialize-WikiDriveCredentialType
    try { [WikiHubDriveCredential]::Save($Target, $User, $Password) } catch {
        throw "Windows Credential Manager refused the drive login ($($_.Exception.InnerException.Message))."
    }
}

function Remove-WikiDriveCredential([string]$Target) {
    Initialize-WikiDriveCredentialType
    [WikiHubDriveCredential]::Delete($Target)
}

function Open-WikiDriveFolder([string]$Letter) {
    try { Start-Process explorer.exe "$Letter\" } catch { }
}

function Connect-WikiDrive([string]$Token, $Status, [string]$Computer) {
    # Devices previously connected from this computer; replaced once the new login works.
    $previous = @($Status.devices | Where-Object { -not $_.revoked -and $_.name -eq $Computer })
    $mappings = Get-WikiDriveMappings
    $letter = $null
    foreach ($key in $mappings.Keys) { if ($mappings[$key] -eq $Status.unc) { $letter = $key } }
    if (-not $letter) { $letter = Get-WikiDriveFreeLetter 'W:' }
    $credential = Invoke-WikiDriveApi 'POST' '/api/drive/devices' $Token @{ name = $Computer }
    $saved = $false
    $inboxLetter = $null
    try {
        Save-WikiDriveCredential $credential.server $credential.login $credential.password
        $saved = $true
        # Both shares may hold the previous device's SMB session.
        Dismount-WikiDriveShares @($credential.unc, $Status.inbox_unc)
        Mount-WikiDriveShare $letter $credential.unc
        Set-WikiDriveLabel $credential.unc 'Wiki'
        if ($Status.inbox_unc) {
            $inboxLetter = Get-WikiDriveFreeLetter 'I:'
            Mount-WikiDriveShare $inboxLetter $Status.inbox_unc
            Set-WikiDriveLabel $Status.inbox_unc 'Wiki Inbox'
        }
    } catch {
        $failure = $_
        # Each cleanup step runs even if an earlier one fails; the original error is reported.
        if ($saved) { try { Dismount-WikiDriveShares @($credential.unc, $Status.inbox_unc) } catch { } }
        try { Invoke-WikiDriveApi 'POST' "/api/drive/devices/$($credential.device.id)/revoke" $Token $null | Out-Null } catch { }
        if ($saved) {
            # The saved login replaced the previous device's; neither is usable now.
            try { Remove-WikiDriveCredential $credential.server } catch { }
            foreach ($old in $previous) {
                try { Invoke-WikiDriveApi 'POST' "/api/drive/devices/$($old.id)/revoke" $Token $null | Out-Null } catch { }
            }
        }
        throw $failure
    } finally {
        $credential.password = $null
    }
    foreach ($old in $previous) {
        try { Invoke-WikiDriveApi 'POST' "/api/drive/devices/$($old.id)/revoke" $Token $null | Out-Null } catch { }
    }
    $editable = @($Status.wikis | Where-Object { $_.access -eq 'write' }).Count
    Write-Host ''
    Write-Host "Connected. Your wikis are in Explorer as $letter (Wiki)." -ForegroundColor Green
    if ($editable) {
        Write-Host "You can edit pages in the wiki folder of $editable wiki(s); saved changes are published after about 20 seconds."
    } else {
        Write-Host 'The drive is read-only for you.'
    }
    if ($inboxLetter) { Write-Host "Files for intake go in $inboxLetter (Wiki Inbox)." }
    Open-WikiDriveFolder $letter
}

function Disconnect-WikiDrive([string]$Token, $Status, [string]$Computer) {
    Dismount-WikiDriveShares @($Status.unc, $Status.inbox_unc)
    Remove-WikiDriveCredential $Status.server
    $revoked = 0
    $pending = 0
    foreach ($old in @($Status.devices | Where-Object { (-not $_.revoked -or $_.pending) -and $_.name -eq $Computer })) {
        $result = Invoke-WikiDriveApi 'POST' "/api/drive/devices/$($old.id)/revoke" $Token $null
        if ($result.pending) { $pending += 1 }
        $revoked += 1
    }
    if ($pending) {
        Write-Host 'Removed the drive locally; server revocation is pending. Run the same command again to finish.' `
            -ForegroundColor Yellow
        return
    }
    Write-Host "Disconnected the Wiki drive from this computer ($revoked login(s) revoked)." -ForegroundColor Green
}

function Invoke-WikiDrive([string]$Action) {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    try {
        Assert-WikiDriveWindows
        $token = Get-WikiDriveToken
        $status = Invoke-WikiDriveApi 'GET' '/api/drive' $token $null
        $computer = $env:COMPUTERNAME
        if ($Action -eq 'disconnect') { Disconnect-WikiDrive $token $status $computer }
        else { Connect-WikiDrive $token $status $computer }
    } catch {
        # Report plainly and never close the window: this runs inside the person's own PowerShell.
        $outcome = 'connected'
        if ($Action -eq 'disconnect') { $outcome = 'disconnected' }
        Write-Host "The Wiki drive was not $($outcome): $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Run the same command again, or ask the person who manages Wiki Hub."
    }
}

Invoke-WikiDrive '__WIKI_ACTION__'
