# BenchCraft installer for Windows (PowerShell 5.1+):
#
#   irm https://raw.githubusercontent.com/HaisamAbbas/BenchCraft/main/install.ps1 | iex
#
# Installs uv if missing (https://astral.sh/uv; it brings its own Python), downloads the
# BenchCraft wheel from a GitHub Release, verifies it against the release's SHA256SUMS and
# installs it as an isolated tool: `benchcraft` on PATH, nothing in your projects.
#
# Optional environment variables:
#   BENCHCRAFT_VERSION   a release tag, e.g. v0.1.0rc1 (default: the newest release)
#   BENCHCRAFT_RELEASES  a folder or URL holding a release's files (for testing a build)
#   BENCHCRAFT_NO_MODIFY_PATH=1  leave PATH alone (uv's tool bin directory is not added)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is far faster without it
$Repo = 'HaisamAbbas/BenchCraft'

function Say([string]$Message) { Write-Host "benchcraft: $Message" }

function Install-BenchCraft {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say 'installing uv (the Python tool manager from https://astral.sh/uv)'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'uv was installed but is not on PATH; open a new terminal and run this again'
        }
    }

    $Source = $env:BENCHCRAFT_RELEASES
    if (-not $Source) {
        $Tag = $env:BENCHCRAFT_VERSION
        if (-not $Tag) {
            # The newest release, pre-releases included (releases/latest skips those).
            $Tag = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases?per_page=1")[0].tag_name
            if (-not $Tag) { throw "no BenchCraft release found at github.com/$Repo" }
        }
        $Source = "https://github.com/$Repo/releases/download/$Tag"
    }
    $Remote = $Source -match '^https?://'
    Say "installing from $Source"

    $Work = Join-Path $env:TEMP ("benchcraft-install-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $Work | Out-Null
    try {
        $Sums = Join-Path $Work 'SHA256SUMS'
        if ($Remote) { Invoke-WebRequest "$Source/SHA256SUMS" -OutFile $Sums -UseBasicParsing }
        else { Copy-Item (Join-Path $Source 'SHA256SUMS') $Sums }

        $Entry = Get-Content $Sums | ForEach-Object {
            if ($_ -match '^([0-9a-f]{64})\s+\*?(aibench-[^-]+-py3-none-any\.whl)$') {
                [pscustomobject]@{ Hash = $Matches[1]; Name = $Matches[2] }
            }
        } | Select-Object -First 1
        if (-not $Entry) { throw "the release at $Source lists no BenchCraft wheel" }

        $Wheel = Join-Path $Work $Entry.Name
        if ($Remote) { Invoke-WebRequest "$Source/$($Entry.Name)" -OutFile $Wheel -UseBasicParsing }
        else { Copy-Item (Join-Path $Source $Entry.Name) $Wheel }
        $Actual = (Get-FileHash $Wheel -Algorithm SHA256).Hash.ToLower()
        if ($Actual -ne $Entry.Hash) {
            throw "$($Entry.Name) does not match the release's SHA256SUMS; not installed"
        }

        Say "installing $($Entry.Name)"
        # uv reports progress on stderr. Windows PowerShell turns redirected native stderr
        # into error records, which 'Stop' would treat as a failure: judge by exit code.
        $ErrorActionPreference = 'Continue'
        uv tool install --force --python 3.12 $Wheel 2>&1 | ForEach-Object { Write-Host "  $_" }
        $Code = $LASTEXITCODE
        if ($Code -eq 0 -and -not $env:BENCHCRAFT_NO_MODIFY_PATH) {
            uv tool update-shell 2>&1 | Out-Null
        }
        $ErrorActionPreference = 'Stop'
        if ($Code -ne 0) { throw "uv tool install failed (exit $Code)" }
    }
    finally {
        Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
    }

    Say 'done. Open a new terminal, go to your project and type: benchcraft'
    Say 'update: run this installer again; remove: uv tool uninstall aibench'
}

Install-BenchCraft
