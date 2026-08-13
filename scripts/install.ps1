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

function Assert-NoReparseAncestors([string]$Path) {
    $FullPath = Resolve-NormalizedPath $Path
    $RootPath = [IO.Path]::GetPathRoot($FullPath)
    $Current = $RootPath
    $Relative = $FullPath.Substring($RootPath.Length)
    foreach ($Part in @($Relative -split '[\\/]')) {
        if ($Part) { $Current = Join-Path $Current $Part }
        if (-not (Test-Path -LiteralPath $Current)) { break }
        $Item = Get-Item -Force -LiteralPath $Current
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A reparse point can redirect the selected Hermes home: $Current"
        }
    }
    $FullPath
}

function Find-HermesSourceRoot([string]$Path) {
    $Current = Resolve-NormalizedPath $Path
    while ($Current) {
        $HasConfigModule = Test-Path -LiteralPath (Join-Path $Current "hermes_cli\config.py") -PathType Leaf
        $HasConstants = Test-Path -LiteralPath (Join-Path $Current "hermes_constants.py") -PathType Leaf
        $HasProject = Test-Path -LiteralPath (Join-Path $Current "pyproject.toml") -PathType Leaf
        if ($HasConfigModule -and $HasConstants -and $HasProject) { return $Current }

        $Parent = [IO.Directory]::GetParent($Current)
        if ($null -eq $Parent) { break }
        $ParentPath = Resolve-NormalizedPath $Parent.FullName
        if ($ParentPath.Equals($Current, [StringComparison]::OrdinalIgnoreCase)) { break }
        $Current = $ParentPath
    }
    $null
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

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$SourceAgent = Join-Path $RepositoryRoot "plugin\agent\$PluginId"
$SourceDesktop = Join-Path $RepositoryRoot "plugin\desktop\$PluginId"
$DefaultHermesRoot = Resolve-NormalizedPath (Join-Path $env:LOCALAPPDATA "hermes")
$ResolvedAgent = Resolve-NormalizedPath $HermesAgent
$Python = Join-Path $ResolvedAgent "venv\Scripts\python.exe"
$Hermes = Join-Path $ResolvedAgent "venv\Scripts\hermes.exe"

$RequiredFiles = @(
    (Join-Path $SourceAgent "plugin.yaml"),
    (Join-Path $SourceAgent "dashboard\config_write.py"),
    (Join-Path $SourceAgent "dashboard\plugin_api.py"),
    (Join-Path $SourceDesktop "plugin.js"),
    (Join-Path $PSScriptRoot "plugin_state.py"),
    (Join-Path $PSScriptRoot "profile_targets.py"),
    $Python
)
foreach ($RequiredFile in $RequiredFiles) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "A required install file was not found: $RequiredFile"
    }
}
if ($RestartGateway -and -not (Test-Path -LiteralPath $Hermes -PathType Leaf)) {
    throw "The Hermes command was not found at $Hermes"
}

if ($PSCmdlet.ParameterSetName -eq "Home") {
    $SelectedProfiles = @([pscustomobject]@{ name = "selected"; path = (Resolve-NormalizedPath $HermesHome) })
}
else {
    if (-not (Test-Path -LiteralPath $DefaultHermesRoot -PathType Container)) {
        throw "The Hermes root was not found at $DefaultHermesRoot"
    }
    $ResolverArguments = @(
        "--hermes-agent", $ResolvedAgent,
        "--hermes-root", $DefaultHermesRoot
    )
    if ($PSCmdlet.ParameterSetName -eq "AllProfiles") {
        $ResolverArguments += "--all-profiles"
    }
    else {
        $ResolverArguments += @("--profile", $Profile)
    }
    $ProfileJson = & $Python -B (Join-Path $PSScriptRoot "profile_targets.py") @ResolverArguments
    if ($LASTEXITCODE -ne 0) { throw "Hermes could not resolve the selected profile." }
    try { $SelectedProfiles = @($ProfileJson | ConvertFrom-Json) }
    catch { throw "Hermes returned an invalid profile list: $($_.Exception.Message)" }
}

$Plans = @()
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
foreach ($SelectedProfile in $SelectedProfiles) {
    $TargetHome = Resolve-NormalizedPath ([string]$SelectedProfile.path)
    if (-not (Test-Path -LiteralPath $TargetHome -PathType Container)) {
        throw "The selected Hermes profile home was not found: $TargetHome"
    }
    $TargetHome = Assert-NoReparseAncestors $TargetHome
    $DetectedSourceRoot = Find-HermesSourceRoot $TargetHome
    if ($null -ne $DetectedSourceRoot) {
        throw "A Hermes profile home cannot be inside a Hermes Agent source directory: $DetectedSourceRoot"
    }
    $AgentPrefix = $ResolvedAgent + [IO.Path]::DirectorySeparatorChar
    if (
        $TargetHome.Equals($ResolvedAgent, [StringComparison]::OrdinalIgnoreCase) -or
        $TargetHome.StartsWith($AgentPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "A Hermes profile home cannot be the Hermes Agent source directory or one of its child directories: $TargetHome"
    }
    if ($PSCmdlet.ParameterSetName -ne "Home") {
        $TargetHome = Assert-SafePath $DefaultHermesRoot $TargetHome $true
    }
    $AgentTarget = Assert-SafePath $TargetHome (Join-Path $TargetHome "plugins\$PluginId")
    $DesktopTarget = Assert-SafePath $TargetHome (Join-Path $TargetHome "desktop-plugins\$PluginId")
    $ConfigPath = Assert-SafePath $TargetHome (Join-Path $TargetHome "config.yaml")
    $BackupRoot = Assert-SafePath $TargetHome (Join-Path $TargetHome "plugin-backups\$PluginId-$Stamp-$RunId")
    $ReceiptPath = Assert-SafePath $TargetHome (Join-Path $BackupRoot "transaction.json")
    if (Test-Path -LiteralPath $ReceiptPath) { throw "The transaction receipt path already exists: $ReceiptPath" }
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
        AgentTarget = $AgentTarget
        DesktopTarget = $DesktopTarget
        AgentParent = Split-Path -Parent $AgentTarget
        DesktopParent = Split-Path -Parent $DesktopTarget
        AgentParentExisted = Test-Path -LiteralPath (Split-Path -Parent $AgentTarget) -PathType Container
        DesktopParentExisted = Test-Path -LiteralPath (Split-Path -Parent $DesktopTarget) -PathType Container
        ConfigPath = $ConfigPath
        ReceiptPath = $ReceiptPath
        BackupRoot = $BackupRoot
        Existing = $Existing
        AgentCopyAttempted = $false
        DesktopCopyAttempted = $false
        ConfigMutationAttempted = $false
        RestartAttempted = $false
    }
}

try {
    foreach ($Plan in $Plans) {
        $Plan.Home = Assert-NoReparseAncestors $Plan.Home
        New-Item -ItemType Directory -Force -Path $Plan.AgentParent | Out-Null
        New-Item -ItemType Directory -Force -Path $Plan.DesktopParent | Out-Null
        $Plan.Home = Assert-NoReparseAncestors $Plan.Home
        $Plan.AgentTarget = Assert-SafePath $Plan.Home $Plan.AgentTarget
        $Plan.DesktopTarget = Assert-SafePath $Plan.Home $Plan.DesktopTarget

        New-Item -ItemType Directory -Path $Plan.BackupRoot | Out-Null
        $Plan.BackupRoot = Assert-SafePath $Plan.Home $Plan.BackupRoot
        if ($Plan.Existing.Count -gt 0) {
            foreach ($Item in $Plan.Existing) {
                $Plan.Home = Assert-NoReparseAncestors $Plan.Home
                $Item.Source = Assert-SafePath $Plan.Home $Item.Source
                $Item.Destination = Assert-SafePath $Plan.Home $Item.Destination
                Move-Item -LiteralPath $Item.Source -Destination $Item.Destination
                $Item.Moved = $true
            }
        }
        $Plan.Home = Assert-NoReparseAncestors $Plan.Home
        $Plan.AgentTarget = Assert-SafePath $Plan.Home $Plan.AgentTarget
        $Plan.AgentCopyAttempted = $true
        Copy-Item -Recurse -LiteralPath $SourceAgent -Destination $Plan.AgentTarget
        $Plan.Home = Assert-NoReparseAncestors $Plan.Home
        $Plan.DesktopTarget = Assert-SafePath $Plan.Home $Plan.DesktopTarget
        $Plan.DesktopCopyAttempted = $true
        Copy-Item -Recurse -LiteralPath $SourceDesktop -Destination $Plan.DesktopTarget

        $Plan.Home = Assert-NoReparseAncestors $Plan.Home
        $Plan.ConfigPath = Assert-SafePath $Plan.Home $Plan.ConfigPath
        $Plan.ReceiptPath = Assert-SafePath $Plan.Home $Plan.ReceiptPath
        $Plan.ConfigMutationAttempted = $true
        & $Python -B (Join-Path $PSScriptRoot "plugin_state.py") enable --hermes-agent $ResolvedAgent --hermes-home $Plan.Home --receipt $Plan.ReceiptPath
        $PluginStateExitCode = $LASTEXITCODE
        if ($PluginStateExitCode -ne 0) { throw "Hermes did not enable the backend plugin for $($Plan.Name)." }
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
    $PreservedFailedCopies = [Collections.Generic.List[string]]::new()
    $RollbackPlans = @($Plans)
    [array]::Reverse($RollbackPlans)
    foreach ($Plan in $RollbackPlans) {
        try { $Plan.Home = Assert-NoReparseAncestors $Plan.Home }
        catch {
            $RollbackErrors.Add("The profile path changed before rollback for $($Plan.Name): $($_.Exception.Message)")
            continue
        }
        foreach ($Copy in @(
            @{ Attempted = $Plan.DesktopCopyAttempted; Target = $Plan.DesktopTarget; Destination = (Join-Path $Plan.BackupRoot "failed-desktop") },
            @{ Attempted = $Plan.AgentCopyAttempted; Target = $Plan.AgentTarget; Destination = (Join-Path $Plan.BackupRoot "failed-agent") }
        )) {
            if ($Copy.Attempted -and (Test-Path -LiteralPath $Copy.Target)) {
                try {
                    $SafeTarget = Assert-SafePath $Plan.Home $Copy.Target
                    if (-not (Test-Path -LiteralPath $Plan.BackupRoot -PathType Container)) {
                        New-Item -ItemType Directory -Path $Plan.BackupRoot | Out-Null
                    }
                    $SafeDestination = Assert-SafePath $Plan.Home $Copy.Destination
                    if (Test-Path -LiteralPath $SafeDestination) {
                        throw "The recovery destination already exists: $SafeDestination"
                    }
                    Move-Item -LiteralPath $SafeTarget -Destination $SafeDestination
                    $PreservedFailedCopies.Add($SafeDestination)
                }
                catch { $RollbackErrors.Add("Could not preserve $($Copy.Target): $($_.Exception.Message)") }
            }
        }
        foreach ($Item in $Plan.Existing) {
            if (-not $Item.Moved) { continue }
            try {
                if (Test-Path -LiteralPath $Item.Source) { throw "The original target is not empty: $($Item.Source)" }
                Move-Item -LiteralPath $Item.Destination -Destination $Item.Source
            }
            catch { $RollbackErrors.Add("Could not restore the $($Item.Name) plugin for $($Plan.Name): $($_.Exception.Message)") }
        }
        if ($Plan.ConfigMutationAttempted) {
            if (Test-Path -LiteralPath $Plan.ReceiptPath -PathType Leaf) {
                try {
                    $Plan.ConfigPath = Assert-SafePath $Plan.Home $Plan.ConfigPath
                    $Plan.ReceiptPath = Assert-SafePath $Plan.Home $Plan.ReceiptPath
                    & $Python -B (Join-Path $PSScriptRoot "plugin_state.py") rollback --hermes-agent $ResolvedAgent --hermes-home $Plan.Home --receipt $Plan.ReceiptPath
                    if ($LASTEXITCODE -ne 0) { throw "Hermes did not roll back the plugin allow-list." }
                    Remove-Item -Force -LiteralPath $Plan.ReceiptPath
                }
                catch {
                    $RollbackErrors.Add("Could not restore the plugin allow-list for $($Plan.Name): $($_.Exception.Message). Receipt: $($Plan.ReceiptPath)")
                }
            }
            else {
                $RollbackErrors.Add("No transaction receipt was available for $($Plan.Name). No plugin allow-list rollback was attempted.")
            }
        }
        if ($Plan.RestartAttempted) {
            try { Invoke-GatewayRestart $Hermes $Plan.Home }
            catch { $RollbackErrors.Add("Could not restart the restored gateway for $($Plan.Name): $($_.Exception.Message)") }
        }
        foreach ($Parent in @(
            @{ Existed = $Plan.DesktopParentExisted; Path = $Plan.DesktopParent },
            @{ Existed = $Plan.AgentParentExisted; Path = $Plan.AgentParent },
            @{ Existed = $false; Path = $Plan.BackupRoot }
        )) {
            if (-not $Parent.Existed -and (Test-Path -LiteralPath $Parent.Path -PathType Container)) {
                try {
                    if (@(Get-ChildItem -Force -LiteralPath $Parent.Path).Count -eq 0) { Remove-Item -Force -LiteralPath $Parent.Path }
                }
                catch { $RollbackErrors.Add("Could not remove the empty folder $($Parent.Path): $($_.Exception.Message)") }
            }
        }
    }
    $Message = "Install failed: $PrimaryError"
    if ($PreservedFailedCopies.Count -gt 0) {
        $Message += " Failed install files were preserved at: " + ($PreservedFailedCopies -join ", ") + "."
    }
    if ($RollbackErrors.Count -gt 0) { $Message += " Rollback problems: " + ($RollbackErrors -join " | ") }
    throw $Message
}

foreach ($Plan in $Plans) {
    if (Test-Path -LiteralPath $Plan.ReceiptPath -PathType Leaf) {
        try { Remove-Item -Force -LiteralPath $Plan.ReceiptPath }
        catch { Write-Warning "Could not remove the completed transaction receipt: $($Plan.ReceiptPath)" }
    }
    if ($Plan.Existing.Count -eq 0 -and (Test-Path -LiteralPath $Plan.BackupRoot -PathType Container)) {
        if (@(Get-ChildItem -Force -LiteralPath $Plan.BackupRoot).Count -eq 0) {
            Remove-Item -Force -LiteralPath $Plan.BackupRoot
        }
    }
    if ($Plan.Existing.Count -gt 0) { Write-Output "Previous $($Plan.Name) plugin files moved to $($Plan.BackupRoot)" }
    Write-Output "Installed backend for $($Plan.Name): $($Plan.AgentTarget)"
    Write-Output "Installed Desktop plugin for $($Plan.Name): $($Plan.DesktopTarget)"
}
Write-Output "No Hermes Agent source file was changed."
