# Contributing

Thank you for helping with Hermes Auxiliary Fallbacks.

## Safety rules

- Do not change files in the Hermes Agent source tree.
- Do not store or print provider credentials.
- Use the provider and model catalog that Hermes supplies.
- Keep install and removal operations inside the selected Hermes data home.
- Add a test for each configuration write or rollback change.

## Development setup

Use Python 3.11 or newer, Node.js, and Windows PowerShell 5.1 or newer.

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\requirements-dev.txt
& .\.venv\Scripts\python.exe -m pytest -q
node --check .\plugin\desktop\auxiliary-fallbacks\plugin.js
```

Parse the PowerShell scripts without running them:

```powershell
$files = @('.\scripts\install.ps1', '.\scripts\uninstall.ps1')
foreach ($file in $files) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $file),
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count) {
        $errors | ForEach-Object { Write-Error $_.Message }
        exit 1
    }
}
```

The live Vision test is not part of CI. It sends an image to a real provider. Use it only with a test image and an account that you control.

## Pull requests

- Keep each pull request focused.
- Explain user-visible behavior and safety effects.
- Include the checks that you ran.
- Remove secrets, personal paths, and private host names from logs and examples.
