generator-chatshoper

Aplikacja Streamlit do generowania opisów SEO, scrapingu produktów/listingów i eksportu CSV dla Shoper oraz vinted-bulk-uploader.

## Uruchamianie macOS/Linux

1. Otwórz terminal w folderze projektu.
2. Utwórz środowisko:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
3. Zainstaluj zależności:
   ```bash
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   ```
4. Uruchom:
   ```bash
   python -m streamlit run app.py
   ```

## Konfiguracja

- `ANTHROPIC_API_KEY` można wpisać w sidebarze albo ustawić w `.streamlit/secrets.toml`.
- `GENERATOR_CHATSHOPER_MODELS` pozwala nadpisać listę modeli Claude, np.:
  ```bash
  export GENERATOR_CHATSHOPER_MODELS="claude-sonnet-4-6,claude-opus-4-6"
  ```

## Funkcje

- generowanie opisów SEO z linków produktów, ręcznie i bulk z listingu
- presety sklepów: Sansa Europe, Olek Motocykle, Adidas PL
- stabilne `product_code` z SKU albo deterministycznego hash produktu
- eksport Shoper CSV z wyborem `UTF-8 BOM` albo `UTF-8`
- walidator Shoper i raport błędów CSV
- eksport vinted-bulk-uploader z walidacją wymaganych pól
- zapis i odczyt projektu roboczego JSON
- podgląd karty produktu przed eksportem
- retry błędnych pozycji w bulk
- cache pobranych stron HTML w `.scrape-cache/`
- eksport logów błędów scrapingu/generowania do CSV

## Windows one-command install z GitHuba

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/seodemtechniczny-cpu/generator-chatshoper/main/install-windows.ps1 | iex"
```

Skrypt instaluje `git` i `python` przez `winget`, klonuje repo do `%USERPROFILE%\generator-chatshoper`, tworzy `.venv`, instaluje biblioteki i tworzy launcher `start-generator-chatshoper.cmd`.

Po instalacji Playwright może wymagać:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Testy

```bash
PYTHONPYCACHEPREFIX=/tmp/generator-chatshoper-pycache python3 -m unittest discover -s tests
```
