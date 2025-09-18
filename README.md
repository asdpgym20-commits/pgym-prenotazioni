# Pgym 2.0 — Prenotazioni (Demo base)

## Avvio locale
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Vai su http://127.0.0.1:5000

## Deploy su Render
- Procfile già incluso (`web: gunicorn app:app`)
- requirements.txt già pronto
- Basta collegare il repo GitHub e fare deploy
