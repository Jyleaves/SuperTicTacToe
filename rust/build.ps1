#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$cargoRoot = if ($env:CARGO_HOME) { $env:CARGO_HOME } else {
    Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cargo'
}
$cargoRoot = [IO.Path]::GetFullPath($cargoRoot)
$previousFlags = $env:CARGO_ENCODED_RUSTFLAGS
$buildFlags = @('-C', 'target-feature=+crt-static')
if ($previousFlags) {
    $buildFlags += $previousFlags.Split([char]31)
} elseif ($env:RUSTFLAGS) {
    $buildFlags += ($env:RUSTFLAGS -split '\s+' | Where-Object { $_ })
}
foreach ($mapping in @(@($projectRoot, 'project'), @($cargoRoot, 'cargo'))) {
    # rustc diagnostics and panic locations can use either Windows path spelling.
    $buildFlags += "--remap-path-prefix=$($mapping[0])=$($mapping[1])"
    $buildFlags += "--remap-path-prefix=$($mapping[0].Replace('\', '/'))=$($mapping[1])"
}

Push-Location -LiteralPath $projectRoot
try {
    $env:CARGO_ENCODED_RUSTFLAGS = $buildFlags -join [char]31
    Write-Host '[1/2] Build Rust backend'
    & cargo build --release --locked --manifest-path rust/Cargo.toml --target-dir rust/target
    if ($LASTEXITCODE -ne 0) { throw "Backend build failed ($LASTEXITCODE)" }
    Write-Host '[2/2] Build desktop application'
    & cargo build --release --locked --manifest-path rust/app/Cargo.toml --target-dir rust/app/target
    if ($LASTEXITCODE -ne 0) { throw "Desktop build failed ($LASTEXITCODE)" }
    Copy-Item -LiteralPath 'rust/target/release/sttt.dll' -Destination 'super_ttt/sttt.dll'
    Copy-Item -LiteralPath 'rust/app/target/release/sttt-app.exe' -Destination 'SuperTicTacToe.exe'
    Write-Host '[OK] SuperTicTacToe.exe and super_ttt/sttt.dll updated'
} finally {
    $env:CARGO_ENCODED_RUSTFLAGS = $previousFlags
    Pop-Location
}
