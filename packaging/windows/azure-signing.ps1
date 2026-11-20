# Microsoft Artifact Signing uses an OIDC Azure login and a non-exportable cloud key.
$ErrorActionPreference = 'Stop'
$dest = Join-Path $env:RUNNER_TEMP 'ori-artifact-signing'
$null = New-Item -ItemType Directory -Path $dest -ErrorAction Stop
$zip = Join-Path $dest 'microsoft-client.zip'
Invoke-WebRequest -Uri 'https://api.nuget.org/v3-flatcontainer/microsoft.trusted.signing.client/1.0.95/microsoft.trusted.signing.client.1.0.95.nupkg' -OutFile $zip
if ((Get-FileHash $zip).Hash -ne '3bfcf1e0a3cb42af1692f0a8ed45c15de070c2de86f28a59b2795d904d8a920f') {
    throw 'Microsoft Artifact Signing client package checksum mismatch'
}
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $dest 'client')
$endpoint = [uri]$env:ORI_AZURE_SIGNING_ENDPOINT
if ($endpoint.Scheme -ne 'https' -or -not $endpoint.Host.EndsWith('.codesigning.azure.net') -or
    $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or
    -not $env:ORI_AZURE_SIGNING_ACCOUNT -or -not $env:ORI_AZURE_SIGNING_PROFILE) {
    throw 'Configure the release Artifact Signing account, endpoint and certificate profile'
}
$metadata = Join-Path $dest 'profile.json'
$profile = @{Endpoint=$endpoint.AbsoluteUri; CodeSigningAccountName=$env:ORI_AZURE_SIGNING_ACCOUNT;
    CertificateProfileName=$env:ORI_AZURE_SIGNING_PROFILE}
[IO.File]::WriteAllText($metadata, ($profile | ConvertTo-Json))
$dlib = (Resolve-Path (Join-Path $dest 'client/bin/x64/Azure.CodeSigning.Dlib.dll')).Path
"ORI_AZURE_DLIB=$dlib" >> $env:GITHUB_ENV
"ORI_AZURE_SIGNING_METADATA=$metadata" >> $env:GITHUB_ENV
