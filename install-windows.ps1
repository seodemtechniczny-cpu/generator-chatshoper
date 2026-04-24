param(
    [string]$RepoUrl = "https://github.com/seodemtechniczny-cpu/generator-chatshoper.git",
    [string]$InstallDir = "$HOME\generator-chatshoper"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[generator-chatshoper] $Message"
}

function Ensure-Command {
    param(
        [string]$CommandName,
        [string]$WingetId
    )

    if (Get-Command $CommandName -ErrorAction SilentlyContinue) {
        return
    }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Brakuje '$CommandName' oraz 'winget'. Zainstaluj $CommandName ręcznie i uruchom skrypt ponownie."
    }

    Write-Step "Instaluje $CommandName przez winget..."
    winget install --id $WingetId -e --accept-package-agreements --accept-source-agreements --silent
}

function Resolve-GitCommand {
    $candidates = @(
        (Get-Command git -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        "$env:ProgramFiles\Git\cmd\git.exe",
        "$env:ProgramFiles\Git\bin\git.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    throw "Nie znaleziono git po instalacji."
}

function Resolve-PythonCommand {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @{ Type = "py"; Path = $pyLauncher }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return @{ Type = "python"; Path = $pythonCmd }
    }

    $pythonCandidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:SystemRoot\py.exe"
    )

    foreach ($candidate in $pythonCandidates) {
        if (Test-Path $candidate) {
            if ($candidate -like "*\py.exe") {
                return @{ Type = "py"; Path = $candidate }
            }
            return @{ Type = "python"; Path = $candidate }
        }
    }

    throw "Nie znaleziono Pythona po instalacji."
}

Ensure-Command -CommandName "git" -WingetId "Git.Git"
Ensure-Command -CommandName "py" -WingetId "Python.Python.3.11"

$gitCommand = Resolve-GitCommand
$pythonCommand = Resolve-PythonCommand

if (-not (Test-Path $InstallDir)) {
    Write-Step "Klonuje repozytorium do $InstallDir"
    & $gitCommand clone $RepoUrl $InstallDir
} else {
    Write-Step "Repozytorium już istnieje. Aktualizuję..."
    & $gitCommand -C $InstallDir pull --ff-only
}

Write-Step "Tworzę środowisko virtualenv"
if ($pythonCommand.Type -eq "py") {
    & $pythonCommand.Path -3 -m venv "$InstallDir\.venv"
} else {
    & $pythonCommand.Path -m venv "$InstallDir\.venv"
}

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"

Write-Step "Instaluje biblioteki z requirements.txt"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r "$InstallDir\requirements.txt"
& $venvPython -m playwright install chromium

$launcherPath = Join-Path $InstallDir "start-generator-chatshoper.cmd"
$launcherBody = @"
@echo off
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python -m streamlit run app.py
"@
Set-Content -Path $launcherPath -Value $launcherBody -Encoding ASCII

Write-Step "Instalacja zakończona."
Write-Step "Start aplikacji:"
Write-Host "  $launcherPath"

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Step "Uwaga: ustaw klucz ANTHROPIC_API_KEY w aplikacji albo w %USERPROFILE%\.streamlit\secrets.toml"
}
