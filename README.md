# Homelab Power Manager

## Setup with UV

Install UV (Windows PowerShell):
```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

Create virtual environment and install dependencies:
```powershell
uv venv
uv sync
```

Run the app:
```powershell
uv run python app.py
```

Environment variables:
```powershell
$env:SSH_KEY_PASSPHRASE="your-passphrase"
```
