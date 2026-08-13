[CmdletBinding(DefaultParameterSetName = "Home")]
param(
    [Parameter(ParameterSetName = "Home")]
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA "hermes"),
    [Parameter(Mandatory = $true, ParameterSetName = "Profile")]
    [string]$Profile,
    [Parameter(Mandatory = $true, ParameterSetName = "AllProfiles")]
    [switch]$AllProfiles,
    [string]$HermesAgent = (Join-Path $env:LOCALAPPDATA "hermes\hermes-agent"),
    [switch]$RestartGateway
)

$ErrorActionPreference = "Stop"
$PluginId = "auxiliary-fallbacks"
$RunId = [Guid]::NewGuid().ToString("N")

function Resolve-NormalizedPath([string]$Path) {
    $FullPath = [IO.Path]::GetFullPath($Path)
    $RootPath = [IO.Path]::GetPathRoot($FullPath)
    if ($FullPath.Length -gt $RootPath.Length) {
        $Separators = [char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
        $FullPath = $FullPath.TrimEnd($Separators)
    }
    $FullPath
}

function Assert-SafePath([string]$Root, [string]$Path, [bool]$AllowRoot = $false) {
    $FullPath = Resolve-NormalizedPath $Path
    $RootPrefix = $Root + [IO.Path]::DirectorySeparatorChar
    $IsRoot = $FullPath.Equals($Root, [StringComparison]::OrdinalIgnoreCase)
    if ((-not $AllowRoot -or -not $IsRoot) -and -not $FullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The path is outside the selected Hermes home: $FullPath"
    }

    $Current = $Root
    $RootItem = Get-Item -Force -LiteralPath $Current
    if (($RootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "A reparse point can redirect the plugin path: $Current"
    }
    if ($IsRoot) { return $FullPath }

    $Relative = $FullPath.Substring($RootPrefix.Length)
    foreach ($Part in @($Relative -split '[\\/]')) {
        if ($Part) { $Current = Join-Path $Current $Part }
        if (-not (Test-Path -LiteralPath $Current)) { break }
        $Item = Get-Item -Force -LiteralPath $Current
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A reparse point can redirect the plugin path: $Current"
        }
    }
    $FullPath
}

function Get-ConfigSnapshot([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Container) {
        throw "The Hermes configuration path is a directory: $Path"
    }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return @{ Exists = $true; Bytes = [IO.File]::ReadAllBytes($Path) }
    }
    @{ Exists = $false; Bytes = $null }
}

function Restore-ConfigSnapshot([string]$Path, [hashtable]$Snapshot, [string]$Id) {
    if (-not $Snapshot.Exists) {
        if (Test-Path -LiteralPath $Path) { Remove-Item -Force -LiteralPath $Path }
        return
    }
    $TemporaryPath = "$Path.$Id.rollback"
    try {
        [IO.File]::WriteAllBytes($TemporaryPath, [byte[]]$Snapshot.Bytes)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($TemporaryPath, $Path, $null)
        }
        else { [IO.File]::Move($TemporaryPath, $Path) }
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath) { Remove-Item -Force -LiteralPath $TemporaryPath }
    }
}

function Invoke-GatewayRestart([string]$Command, [string]$TargetHome) {
    $HadHome = Test-Path Env:HERMES_HOME
    $PreviousHome = $env:HERMES_HOME
    try {
        $env:HERMES_HOME = $TargetHome
        & $Command gateway restart
        if ($LASTEXITCODE -ne 0) { throw "The Hermes gateway did not restart for $TargetHome" }
    }
    finally {
        if ($HadHome) { $env:HERMES_HOME = $PreviousHome }
        else { Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue }
    }
}

$DefaultHermesRoot = Resolve-NormalizedPath (Join-Path $env:LOCALAPPDATA "hermes")
$ResolvedAgent = Resolve-NormalizedPath $HermesAgent
$Python = Join-Path $ResolvedAgent "venv\Scripts\python.exe"
$Hermes = Join-Path $ResolvedAgent "venv\Scripts\hermes.exe"
$Resolver = Join-Path $PSScriptRoot "profile_targets.py"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "The Hermes Python runtime was not found at $Python" }
if (-not (Test-Path -LiteralPath $Resolver -PathType Leaf)) { throw "The profile resolver was not found at $Resolver" }
if ($RestartGateway -and -not (Test-Path -LiteralPath $Hermes -PathType Leaf)) { throw "The Hermes command was not found at $Hermes" }

if ($PSCmdlet.ParameterSetName -eq "Home") {
    $SelectedProfiles = @([pscustomobject]@{ name = "selected"; path = (Resolve-NormalizedPath $HermesHome) })
}
else {
    if (-not (Test-Path -LiteralPath $DefaultHermesRoot -PathType Container)) {
        throw "The Hermes root was not found at $DefaultHermesRoot"
    }
    $ResolverArguments = @("--hermes-agent", $ResolvedAgent, "--hermes-root", $DefaultHermesRoot)
    if ($PSCmdlet.ParameterSetName -eq "AllProfiles") { $ResolverArguments += "--all-profiles" }
    else { $ResolverArguments += @("--profile", $Profile) }
    $ProfileJson = & $Python -B $Resolver @ResolverArguments
    if ($LASTEXITCODE -ne 0) { throw "Hermes could not resolve the selected profile." }
    try { $SelectedProfiles = @($ProfileJson | ConvertFrom-Json) }
    catch { throw "Hermes returned an invalid profile list: $($_.Exception.Message)" }
}

$Plans = @()
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
foreach ($SelectedProfile in $SelectedProfiles) {
    $TargetHome = Resolve-NormalizedPath ([string]$SelectedProfile.path)
    if (-not (Test-Path -LiteralPath $TargetHome -PathType Container)) { throw "The selected Hermes profile home was not found: $TargetHome" }
    $AgentPrefix = $ResolvedAgent + [IO.Path]::DirectorySeparatorChar
    if (
        $TargetHome.Equals($ResolvedAgent, [StringComparison]::OrdinalIgnoreCase) -or
        $TargetHome.StartsWith($AgentPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "A Hermes profile home cannot be the Hermes Agent source directory or one of its child directories: $TargetHome"
    }
    if ($PSCmdlet.ParameterSetName -ne "Home") { $TargetHome = Assert-SafePath $DefaultHermesRoot $TargetHome $true }
    $AgentTarget = Assert-SafePath $TargetHome (Join-Path $TargetHome "plugins\$PluginId")
    $DesktopTarget = Assert-SafePath $TargetHome (Join-Path $TargetHome "desktop-plugins\$PluginId")
    $ConfigPath = Assert-SafePath $TargetHome (Join-Path $TargetHome "config.yaml")
    $BackupRoot = Assert-SafePath $TargetHome (Join-Path $TargetHome "plugin-backups\removed-$PluginId-$Stamp-$RunId")
    foreach ($Target in @($AgentTarget, $DesktopTarget)) {
        if (Test-Path -LiteralPath $Target -PathType Leaf) { throw "The plugin target is a file: $Target" }
    }
    $Existing = @()
    if (Test-Path -LiteralPath $AgentTarget -PathType Container) {
        $Existing += @{ Name = "backend"; Source = $AgentTarget; Destination = (Join-Path $BackupRoot "agent"); Moved = $false }
    }
    if (Test-Path -LiteralPath $DesktopTarget -PathType Container) {
        $Existing += @{ Name = "Desktop"; Source = $DesktopTarget; Destination = (Join-Path $BackupRoot "desktop"); Moved = $false }
    }
    $Plans += @{
        Name = [string]$SelectedProfile.name
        Home = $TargetHome
        ConfigPath = $ConfigPath
        ConfigSnapshot = Get-ConfigSnapshot $ConfigPath
        BackupRoot = $BackupRoot
        Existing = $Existing
        ConfigMutationAttempted = $false
        RestartAttempted = $false
    }
}

try {
    foreach ($Plan in $Plans) {
        if ($Plan.Existing.Count -gt 0) {
            New-Item -ItemType Directory -Path $Plan.BackupRoot | Out-Null
            $Plan.BackupRoot = Assert-SafePath $Plan.Home $Plan.BackupRoot
            foreach ($Item in $Plan.Existing) {
                Move-Item -LiteralPath $Item.Source -Destination $Item.Destination
                $Item.Moved = $true
            }
        }
        $Plan.ConfigMutationAttempted = $true
        & $Python -B (Join-Path $PSScriptRoot "plugin_state.py") disable --hermes-agent $ResolvedAgent --hermes-home $Plan.Home
        if ($LASTEXITCODE -ne 0) { throw "Hermes did not disable the backend plugin for $($Plan.Name)." }
    }

    if ($RestartGateway) {
        foreach ($Plan in $Plans) {
            $Plan.RestartAttempted = $true
            Invoke-GatewayRestart $Hermes $Plan.Home
        }
    }
}
catch {
    $PrimaryError = $_.Exception.Message
    $RollbackErrors = [Collections.Generic.List[string]]::new()
    $RollbackPlans = @($Plans)
    [array]::Reverse($RollbackPlans)
    foreach ($Plan in $RollbackPlans) {
        if ($Plan.ConfigMutationAttempted) {
            try { Restore-ConfigSnapshot $Plan.ConfigPath $Plan.ConfigSnapshot $RunId }
            catch { $RollbackErrors.Add("Could not restore the configuration for $($Plan.Name): $($_.Exception.Message)") }
        }
        foreach ($Item in $Plan.Existing) {
            if (-not $Item.Moved) { continue }
            try {
                if (Test-Path -LiteralPath $Item.Source) { throw "The original target is not empty: $($Item.Source)" }
                Move-Item -LiteralPath $Item.Destination -Destination $Item.Source
            }
            catch { $RollbackErrors.Add("Could not restore the $($Item.Name) plugin for $($Plan.Name): $($_.Exception.Message)") }
        }
        if ($Plan.RestartAttempted) {
            try { Invoke-GatewayRestart $Hermes $Plan.Home }
            catch { $RollbackErrors.Add("Could not restart the restored gateway for $($Plan.Name): $($_.Exception.Message)") }
        }
        if (Test-Path -LiteralPath $Plan.BackupRoot -PathType Container) {
            try {
                if (@(Get-ChildItem -Force -LiteralPath $Plan.BackupRoot).Count -eq 0) { Remove-Item -Force -LiteralPath $Plan.BackupRoot }
            }
            catch { $RollbackErrors.Add("Could not remove the empty backup folder $($Plan.BackupRoot): $($_.Exception.Message)") }
        }
    }
    $Message = "Removal failed: $PrimaryError"
    if ($RollbackErrors.Count -gt 0) { $Message += " Rollback problems: " + ($RollbackErrors -join " | ") }
    throw $Message
}

foreach ($Plan in $Plans) {
    if ($Plan.Existing.Count -gt 0) { Write-Output "$($Plan.Name) plugin files moved to $($Plan.BackupRoot)" }
}
Write-Output "Saved fallback chains were not removed."
