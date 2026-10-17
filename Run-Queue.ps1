param(
    [Parameter(Mandatory = $true)][string]$InputDirectory,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$Python = 'py',
    [string]$Ffmpeg = 'ffmpeg',
    [string]$Ffprobe = 'ffprobe',
    [string]$Profile = 'hevc-nvenc',
    [int]$MinAge = 300,
    [ValidateRange(1, 2147483647)][int]$Jobs = 1,
    [ValidatePattern('^[0-9]+[xX][0-9]+$')][string]$Resize,
    [int]$ProgramId = -1,
    [switch]$DryRun,
    [switch]$VerifyFull,
    [switch]$AllowInputErrors
)
$ErrorActionPreference = 'Stop'
if ($MinAge -lt 0) { throw 'MinAge must be nonnegative.' }
# Use full executable paths for scheduled runs, whose PATH may differ.
$runner = Get-Command -Name $Python -CommandType Application -ErrorAction Stop
$cliArgs = @()
if ([IO.Path]::GetFileNameWithoutExtension($runner.Source) -eq 'py') {
    $cliArgs += '-3'
}
$cliArgs += @((Join-Path $PSScriptRoot 'encode.py'), 'encode', $InputDirectory,
    '--output-dir', $OutputDirectory, '--profile', $Profile,
    '--ffmpeg', $Ffmpeg, '--ffprobe', $Ffprobe, '--recursive', '--min-age', "$MinAge", '--jobs', "$Jobs")
if ($ProgramId -ge 0) { $cliArgs += @('--program', "$ProgramId") }
if ($DryRun) { $cliArgs += '--dry-run' }
if ($VerifyFull) { $cliArgs += '--verify-full' }
if ($AllowInputErrors) { $cliArgs += '--allow-input-errors' }
if ($Resize) { $cliArgs += @('--resize', $Resize) }
& $runner.Source @cliArgs
exit $LASTEXITCODE
