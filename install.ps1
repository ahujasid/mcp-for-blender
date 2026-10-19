# MCP for Blender installer for Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://www.mcp-for-blender.com/install.ps1 | iex"
#
# Installs uv (with its official installer) if it's missing, then runs
# `uvx mcp-for-blender setup`, which configures your MCP clients and the
# Blender addon.

# Wrapped in a function so `irm | iex` leaves nothing behind in the session,
# and errors return instead of closing the user's PowerShell window.
function Install-McpForBlender {
    $ErrorActionPreference = 'Stop'

    function Find-Uvx {
        $cmd = Get-Command uvx -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        # Where uv's installer puts it, before a new shell has it on PATH.
        foreach ($dir in @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME, "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            if ($dir -and (Test-Path (Join-Path $dir 'uvx.exe'))) { return (Join-Path $dir 'uvx.exe') }
        }
        return $null
    }

    Write-Host 'MCP for Blender installer'

    $uvx = Find-Uvx
    if (-not $uvx) {
        Write-Host 'Installing uv, which runs MCP for Blender (https://docs.astral.sh/uv/)...'
        powershell -ExecutionPolicy ByPass -c 'irm https://astral.sh/uv/install.ps1 | iex'
        $uvx = Find-Uvx
        if (-not $uvx) {
            Write-Host 'uv installed, but uvx was not found. Open a new terminal and run: uvx mcp-for-blender setup' -ForegroundColor Red
            return
        }
    }

    # Keep uvx's folder on PATH for setup, which records uvx's absolute path.
    $env:Path = "$(Split-Path $uvx);$env:Path"
    # uvx shows nothing while it downloads the package, which can take a while.
    Write-Host 'Downloading MCP for Blender. The first run can take a minute...'
    # --refresh-package: check PyPI now, so a release from minutes ago isn't
    # missed because uv cached the package list.
    & $uvx --refresh-package mcp-for-blender mcp-for-blender@latest setup @args
}

Install-McpForBlender @args
