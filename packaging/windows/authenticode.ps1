# Run only on the provisioned release signer. Windows owns private-key access.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Path,
    [switch] $VerifyOnly
)
$ErrorActionPreference = 'Stop'
$fingerprint = $env:ORI_WINDOWS_CERT_SHA1
$azure = [bool]$env:ORI_AZURE_DLIB
if ($azure) {
    if (-not $env:ORI_WINDOWS_CERT_SUBJECT) { throw 'Configure the approved Azure certificate subject' }
} elseif ($fingerprint -notmatch '^[a-fA-F0-9]{40}$') { throw 'Configure the release certificate thumbprint' }
function Test-ReleaseSigner($signature) {
    if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate) { return $false }
    if ($azure) { return $signature.SignerCertificate.Subject -ceq $env:ORI_WINDOWS_CERT_SUBJECT }
    return $signature.SignerCertificate.Thumbprint -eq $fingerprint
}
$tsa = [uri]$env:ORI_WINDOWS_TIMESTAMP_URL
if (-not $tsa.IsAbsoluteUri -or $tsa.Scheme -notin @('http', 'https') -or -not $tsa.Host -or $tsa.UserInfo -or $tsa.Fragment -or $tsa.Query) {
    throw 'Configure the public HTTP(S) endpoint of the trusted RFC 3161 timestamp service'
}
$target = (Get-Item -LiteralPath $Path -ErrorAction Stop).FullName
$kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
$sdk = Get-ChildItem -LiteralPath $kits -Directory | Where-Object {
    $_.Name -match '^10\.0\.\d+\.\d+$' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'x64\signtool.exe'))
} | Sort-Object { [Version]$_.Name } -Descending | Select-Object -First 1
if (-not $sdk) { throw 'x64 Windows SDK SignTool unavailable' }
$tool = Join-Path $sdk.FullName 'x64\signtool.exe'
if (-not $VerifyOnly) {
    # Tauri can encounter an already-signed staged helper. Keep its embedded
    # manifest hash intact, signing a new file once only.
    $existing = Get-AuthenticodeSignature -LiteralPath $target
    if ($existing.Status -eq 'Valid' -and $existing.TimeStamperCertificate -and [IO.Path]::GetExtension($target) -eq '.dll') {
        return  # Keep vendor-signed runtime libraries intact.
    }
    if (-not (Test-ReleaseSigner $existing)) {
        if ($azure) {
            & $tool sign /fd SHA256 /tr $tsa.AbsoluteUri /td SHA256 /dlib $env:ORI_AZURE_DLIB /dmdf $env:ORI_AZURE_SIGNING_METADATA $target
        } else {
            & $tool sign /fd SHA256 /sha1 $fingerprint /tr $tsa.AbsoluteUri /td SHA256 $target
        }
        if ($LASTEXITCODE -ne 0) { throw 'Authenticode signing failed' }
    }
}
& $tool verify /pa /all /tw $target
if ($LASTEXITCODE -ne 0) { throw 'WinVerifyTrust failed or reported a missing timestamp' }
# WinVerifyTrust verifies the chain. Inspect the PE's CMS as well so a legacy
# Authenticode countersignature cannot satisfy the RFC 3161 / SHA-256 claim.
Add-Type -AssemblyName System.Security.Cryptography.Pkcs
$reader = [IO.BinaryReader]::new([IO.File]::OpenRead($target))
try {
    $reader.BaseStream.Position = 0x3c
    $pe = $reader.ReadUInt32()
    $reader.BaseStream.Position = $pe
    if ($reader.ReadUInt32() -ne 0x4550) { throw 'Invalid PE header' }
    $optional = [long]$pe + 24
    $reader.BaseStream.Position = $optional
    $magic = $reader.ReadUInt16()
    if ($magic -eq 0x20b) { $directories = 112 }
    elseif ($magic -eq 0x10b) { $directories = 96 }
    else { throw 'Unknown PE optional-header format' }
    $reader.BaseStream.Position = $optional + $directories + 32
    $offset = $reader.ReadUInt32()
    $size = $reader.ReadUInt32()
    if ($size -lt 8 -or $size -gt 16MB -or [long]$offset + $size -gt $reader.BaseStream.Length) {
        throw 'Missing or invalid PE certificate table'
    }
    $reader.BaseStream.Position = $offset
    $length = $reader.ReadUInt32()
    if ($length -lt 8 -or $length -gt $size -or $reader.ReadUInt16() -ne 0x200 -or $reader.ReadUInt16() -ne 2) {
        throw 'Expected a PKCS#7 WIN_CERTIFICATE'
    }
    $cms = [System.Security.Cryptography.Pkcs.SignedCms]::new()
    $cms.Decode($reader.ReadBytes($length - 8))
} finally { $reader.Dispose() }
$sig = Get-AuthenticodeSignature -LiteralPath $target
if (-not (Test-ReleaseSigner $sig)) {
    throw 'Untrusted release signer or missing timestamp certificate'
}
$sha256 = '2.16.840.1.101.3.4.2.1'
$timestampFound = $false
foreach ($signer in $cms.SignerInfos) {
    if ($signer.Certificate.Thumbprint -ne $sig.SignerCertificate.Thumbprint -or $signer.DigestAlgorithm.Value -ne $sha256) { continue }
    foreach ($attr in $signer.UnsignedAttributes) {
        if ($attr.Oid.Value -notin @('1.3.6.1.4.1.311.3.3.1', '1.2.840.113549.1.9.16.2.14')) { continue }
        foreach ($encoded in $attr.Values) {
            $token = $null
            $consumed = 0
            $decoded = [System.Security.Cryptography.Pkcs.Rfc3161TimestampToken]::TryDecode(
                [ReadOnlyMemory[byte]]::new($encoded.RawData), [ref]$token, [ref]$consumed)
            if ($decoded -and $token.TokenInfo.HashAlgorithmId.Value -eq $sha256) { $timestampFound = $true }
        }
    }
}
if (-not $timestampFound) { throw 'Verified signer is missing an RFC 3161 timestamp with a SHA-256 message imprint' }
