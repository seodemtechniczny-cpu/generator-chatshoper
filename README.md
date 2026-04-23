generator-chatshoper

Uruchamianie:
1. Otworz terminal w folderze projektu
2. python3 -m venv .venv
3. source .venv/bin/activate
4. pip install -r requirements.txt
5. python3 -m streamlit run app.py

Windows one-command install z GitHuba:
```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/seodemtechniczny-cpu/generator-chatshoper/main/install-windows.ps1 | iex"
```

Skrypt:
- instaluje `git` i `python` przez `winget`, jeśli ich brakuje
- klonuje repo do `%USERPROFILE%\generator-chatshoper`
- tworzy `.venv`
- instaluje biblioteki z `requirements.txt`
- tworzy launcher `start-generator-chatshoper.cmd`
