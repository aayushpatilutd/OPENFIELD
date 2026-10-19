# Openfield

Openfield is an intelligent accounts-payable operations workspace. This repository contains the current AP workflow application, rebranded with the Openfield visual identity.

## Brand system

- **Primary:** Openfield Green `#0E4D3A`
- **Deep:** `#082F25`
- **Field Light:** `#A7E381`
- **Warm Neutral:** `#F7F5EF`
- **Deep Slate:** `#1A1A1A`
- **Accent Lime:** `#C6FF5A`
- **Typography:** Inter (Satoshi is the intended premium brand typeface; Inter is used as a readily available web fallback)
- **Tagline:** Business runs brighter.

## Run locally

Python 3.11+ recommended.

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open `http://127.0.0.1:5100`. Review `.env.example` and `DEPLOY.md` before using any live integrations. Never commit `.env`, API credentials, production data, or database files.

## Deploy on Render

The project includes `render.yaml` and a Dockerfile. The blueprint uses a persistent disk because the app stores its database and uploaded documents on disk. That configuration requires a paid Render instance/disk; a free instance should be treated as a disposable demo only.

1. Push this repository to a **private GitHub repository**.
2. In Render, choose **New → Blueprint**, connect the repository, and select `render.yaml`.
3. Set the required `INVOICEHUB_FERNET_KEY` when prompted. Generate with:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. Keep demo mode enabled until you have reviewed the app and configured integrations.
5. Deploy, then check `/healthz` and log in with the credentials configured by the app. Change any default password immediately.

Read [DEPLOY.md](DEPLOY.md) and the comments in `render.yaml` before deployment. Do not switch demo mode off until credentials, access controls, backups, and data handling are verified.

## Notes

- This is an application rebrand, not a completed product/legal name clearance.
- The SVG is a starter logo mark and should be reviewed/redrawn before final commercial identity rollout.
- The app uses external Google Fonts for Inter when online; system fallbacks are provided.
