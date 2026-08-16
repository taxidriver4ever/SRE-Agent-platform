param(
    [ValidateSet("all", "order-service", "inventory-service", "user-service", "payment-service", "notification-service", "recommendation-service")]
    [string]$Service = "all",
    [ValidateSet("good", "bad", "both")]
    [string]$Version = "both",
    [switch]$SkipKindLoad
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$infraRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path $infraRoot -Parent
$kindCommand = Get-Command kind -ErrorAction SilentlyContinue
$kindExe = if ($env:KIND_EXE) { $env:KIND_EXE } elseif ($kindCommand) { $kindCommand.Source } else { Join-Path $workspaceRoot "tools\kind.exe" }
if (-not (Test-Path $kindExe)) { throw "kind executable not found; set KIND_EXE or place tools/kind.exe in workspace" }
$services = @("order-service", "inventory-service", "user-service", "payment-service", "notification-service", "recommendation-service")
if ($Service -ne "all") { $services = @($Service) }
$versions = if ($Version -eq "both") { @("good", "bad") } else { @($Version) }

foreach ($serviceName in $services) {
    $repository = Join-Path $workspaceRoot $serviceName
    $resolvedRepository = (Resolve-Path $repository).Path
    if (-not $resolvedRepository.StartsWith($workspaceRoot)) { throw "Unsafe repository path: $resolvedRepository" }

    foreach ($versionName in $versions) {
        $sha = (git -C $resolvedRepository rev-parse $versionName).Trim()
        if ($sha -notmatch '^[0-9a-f]{40}$') { throw "Invalid $serviceName/$versionName SHA: $sha" }
        $worktree = Join-Path ([System.IO.Path]::GetTempPath()) "sre-lab-worktrees\$serviceName-$sha"
        $safeTempRoot = (Join-Path ([System.IO.Path]::GetTempPath()) "sre-lab-worktrees")
        New-Item -ItemType Directory -Force -Path $safeTempRoot | Out-Null
        if (Test-Path $worktree) {
            if (-not $worktree.StartsWith($safeTempRoot)) { throw "Unsafe worktree cleanup path: $worktree" }
            git -C $resolvedRepository worktree remove --force $worktree 2>$null
        }
        try {
            git -C $resolvedRepository worktree add --detach $worktree $sha | Out-Host
            $image = "sre-lab/${serviceName}:$sha"
            docker build --build-arg "VCS_REF=$sha" --build-arg "VCS_SOURCE=$resolvedRepository" -t $image $worktree
            if ($LASTEXITCODE -ne 0) { throw "docker build failed for $image" }
            if (-not $SkipKindLoad) { & $kindExe load docker-image --name sre-lab $image }
            Write-Host "Built $serviceName $versionName -> $image"
        } finally {
            if (Test-Path $worktree) { git -C $resolvedRepository worktree remove --force $worktree }
        }
    }
}
