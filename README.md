# HemoSense

Bedside ABO/Rh blood grouping. A HemoSense reader (ESP32 + AD5933) measures three wells (Anti-A, Anti-B, Anti-D); the readings stream live to a web app for hospital desktops; every patient, test and reading is stored in PostgreSQL on **Nobus Cloud**.

```
Hospital desktop ──HTTPS──▶ Nobus server (Caddy → HemoSense app → PostgreSQL) ◀──WebSocket── reader
```

## Run on Nobus

On a fresh Ubuntu server (ports 22, 80, 443, 8080 open):

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/mayor1238/HemoSense.git hemosense
cd hemosense
bash deploy/setup.sh
```

It prints the link, the admin password and the `DEVICE_KEY`.

Update after every push: `cd ~/hemosense && git pull && sudo docker compose up -d --build`

## Run on a laptop

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --reload        # http://localhost:8000  (admin / hemosense-admin)
python -m pytest -q             # end-to-end test (pip install pytest)
```

## Layout

| Path | What |
|---|---|
| `backend/app.py` | API, logins, live links for readers (`/ws/reader`) and apps (`/ws/app`) |
| `backend/analysis.py` | Blood group call from the three curves |
| `backend/db.py` | Tables; SQLite locally, PostgreSQL on Nobus |
| `backend/static/` | The web app |
| `deploy/` | Caddy (HTTPS) and the setup script |

Prototype for research and demonstration. Confirm every result with standard laboratory grouping.
