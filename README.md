# HemoSense

**Know the blood group at the bedside, in about two minutes, with the result saved where every ward can find it.**

🔗 **Live app (Nobus Cloud):** https://102-209-46-251.sslip.io
🩺 **Health check:** https://102-209-46-251.sslip.io/api/health

Built at the **Ogun Innovate Hackathon 2026** (Ogun Tech Hub, Abeokuta). Deployed on **Nobus Cloud**.

---

## The problem

A blood transfusion saves lives only when the right blood goes to the right patient. When it does not, the danger is immediate: ABO-incompatible blood can destroy the patient's red cells within minutes.

- **Most reported transfusion incidents are avoidable process errors, not problems with the blood itself.** In the UK's national haemovigilance scheme (SHOT), 83% of all 2025 reports were due to avoidable errors.
- **The usual failures are human.** Blood is taken from the wrong patient, a tube is labelled away from the bedside, a name is mistyped, or the final check is skipped during an emergency.
- **The bedside check is manual and subjective.** Where countries require a bedside ABO card test, staff read the clumping (agglutination) by eye, need long training, and leave no digital record.
- **In Nigerian hospitals** the problems are sharper:
  - transfusion errors are a recognised patient-safety concern, including clerical errors that give a patient the wrong blood type
  - grouping results usually live on paper, so a patient's previous group is hard to find when it is needed to catch a mislabelled sample
  - emergencies (obstetric bleeding, road accidents, sickle-cell crises) leave little time to wait for the lab

**In short:** clinicians need a fast, objective bedside blood group that is recorded automatically and checked against the patient's history.

## Our solution

HemoSense is a small electronic reader plus a hospital web app.

1. **The reader** (ESP32 + AD5933 impedance chip) holds a three-well cartridge: **Anti-A, Anti-B and Anti-D**. A drop of blood goes in each well.
2. When the red cells clump (agglutinate), the **electrical impedance** of that well changes. The reader measures all three wells every few seconds for about two minutes.
3. Readings stream **live over WiFi to the HemoSense server on Nobus Cloud**. The server works out the **ABO and Rh group** (for example *A+* or *O−*), with a confidence score and warnings for bad wells.
4. The clinician confirms the patient's wristband and saves. The result is stored with the patient's record and **compared with their earlier results**. A mismatch raises an alert, which is how a wrong-blood-in-tube error gets caught.

Because the reading is electrical, the result is **objective, timed and saved**, not a judgement by eye on a paper card.

## What the app does

| Area | Features |
|---|---|
| **New test** | Patient → Cartridge → Run → Result. Find or register a patient (or create an emergency ID), scan the cartridge, watch the three wells live with a chart and progress bar, and see the group, confidence, flags and a history-mismatch alert. A wristband check is required before saving |
| **Clinical records** | Search every test by patient, group or lab result; open a patient's full history; record the lab's confirmatory group and see whether it agrees |
| **Research data** | Anonymised data (age band, sex, curve features), only from patients who consented; CSV download |
| **Dashboard** | Tests per day, blood-group mix, connected readers and which database is in use |
| **Settings (admin)** | Analysis method and thresholds (including a one-click *Copper board* preset), user accounts, export of all data to JSON, import of a file from an offline laptop |
| **Security** | Hashed passwords (scrypt), 12-hour sessions, login rate limiting, admin/operator roles, a secret key for every reader, HTTPS and security headers |

## How it works

```mermaid
flowchart LR
  R["HemoSense reader<br/>ESP32 + AD5933<br/>Anti-A · Anti-B · Anti-D"] -- "WebSocket /ws/reader<br/>(device key)" --> S
  subgraph N["Nobus Cloud"]
    S["Caddy (HTTPS)"] --> A["HemoSense app<br/>Starlette / Uvicorn"]
    A --> D[("PostgreSQL")]
  end
  B["Hospital desktop<br/>web browser"] -- "HTTPS + WebSocket /ws/app" --> S
```

- **Reader → server:** `info`, `reading {i, t_ms, z, re, im}`, `done`, `error`
- **Server → reader:** `start {test_id, duration_s, interval_s}`, `stop`, `calibrate`, `sync`
- **Blood group call** (`backend/analysis.py`), using one of two methods:
  - *relative:* % change in each well from the start of the run, compared with a threshold
  - *range:* the final impedance falls into a known agglutinated or non-agglutinated band
- **Flags:** empty, shorted, bridged or unstable wells. A run that drops out resumes with `sync`, so no readings are lost

## Nobus Cloud deployment

| Nobus service | How we use it |
|---|---|
| **Compute instance** (Ubuntu 24.04, public IP `102.209.46.251`) | Runs the app, PostgreSQL and Caddy with Docker Compose |
| **Security group** | Opens SSH 22, HTTP 80, HTTPS 443 and 8080 (the reader's plain WebSocket entrance) |
| **Managed Database** (PostgreSQL) | Optional: set `DATABASE_URL` and the app uses it instead of the local container |
| **Cube** | Optional: runs the app straight from this repo using the root `Dockerfile` (port 8000) |
| **EyesOn** | Watches the live link and `/api/health` |

### Deploy on a fresh Nobus server

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/mayor1238/HemoSense.git hemosense
cd hemosense
bash deploy/setup.sh          # installs Docker, writes .env, starts everything
```

The script prints the link, the admin password and the `DEVICE_KEY`. **Keep `.env` private; it is never committed.**

Update after each push:

```bash
cd ~/hemosense && git pull && sudo docker compose up -d --build
```

### Use the Nobus Managed Database

Create a PostgreSQL database (Managed Services → Managed Database) and allow the server's IP. Then run:

```bash
echo 'DATABASE_URL=postgresql://USER:PASSWORD@DB-HOST:5432/DBNAME' >> .env
sudo docker compose up -d
```

Tables are created automatically. The dashboard shows which database is in use.

### Run on Nobus Cube

Managed Services → Cube → Custom Repository `https://github.com/mayor1238/HemoSense.git`, branch `main`, **Dockerfile Present**, container port **8000**. Environment variables:

- `DATABASE_URL`
- `ADMIN_PASSWORD`
- `DEVICE_KEY`

## Connect a reader

**Option 1: the reader connects straight to Nobus (recommended).** Flash `firmware/hemosense_cloud/hemosense_cloud.ino`:

1. Set `WIFI_SSID` and `WIFI_PASS` to a 2.4 GHz WiFi with internet (a phone hotspot works).
2. Set `DEVICE_KEY` to the server's key. **Do not commit the real key.**
3. Install these libraries: WebSockets (Markus Sattler), ArduinoJson 7, Adafruit SSD1306, Adafruit GFX and the AD5933 library.

The reader appears in the app as `HS-DEV-001`.

**Option 2: laptop bridge (no firmware change).** Put the laptop on the reader's WiFi (`HemoSense-XXXX`), give it internet another way, and run:

```bash
pip install websockets
python tools/reader_bridge.py --server wss://102-209-46-251.sslip.io --key YOUR_DEVICE_KEY
```

**No reader?** Start a pretend reader. Its results are clearly labelled *Simulated*:

```bash
sudo docker compose exec app python tools/virtual_reader.py --server ws://localhost:8000 --key YOUR_DEVICE_KEY --speed 4
```

## Run on a laptop

```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --reload        # http://localhost:8000 (local default login: admin / hemosense-admin)
python -m pytest -q             # end-to-end test (pip install pytest)
```

Offline tests can be moved to Nobus later: export them as JSON on the laptop, then use Settings → Import file on the live app.

## Repository layout

| Path | What it is |
|---|---|
| `backend/app.py` | API, logins and the live links for readers (`/ws/reader`) and browsers (`/ws/app`) |
| `backend/analysis.py` | Turns the three impedance curves into a blood group |
| `backend/db.py` | Tables: SQLite on a laptop, PostgreSQL on Nobus |
| `backend/static/` | The desktop web app (HTML, CSS, JavaScript) |
| `backend/tests/` | End-to-end test: login → patient → test → live readings → result → save → export/import |
| `firmware/hemosense_cloud/` | Reader firmware that connects directly to Nobus |
| `tools/` | Pretend reader and laptop bridge |
| `deploy/` | Caddy (automatic HTTPS) and the one-command setup script |
| `docker-compose.yml`, `Dockerfile` | Server stack, and the image for Nobus Cube |

## Tech stack

- **Back end:** Python, Starlette, Uvicorn and WebSockets
- **Database:** PostgreSQL (SQLite on a laptop)
- **Front end:** plain HTML, CSS and JavaScript with SVG charts, no framework
- **Hardware:** ESP32, AD5933 impedance analyser, multiplexer, OLED
- **Infrastructure:** Docker Compose and Caddy on Nobus Cloud

## What's next

- Validate against standard laboratory grouping on a larger set of samples
- Print a result slip and print a patient wristband from the app
- Link with hospital records (HL7/FHIR) and blood-bank stock, so a unit is checked against the patient before it is hung

## Team

- **Mayowa Titilola** (GitHub [mayor1238](https://github.com/mayor1238))
- **Oriaku David**
- **Odiwe Emeka**

## References

- SHOT (UK haemovigilance), *Annual SHOT Report 2025: Headline data*. [shotuk.org](https://www.shotuk.org/wp-content/uploads/2026/07/3.-Headline-Data-Deaths-Major-Morbidity-and-ABO-Incompatible-Transfusions-2025.pdf)
- SHOT, *Key messages and recommendations*, including the bedside checklist. [shotuk.org](https://www.shotuk.org/wp-content/uploads/2025/06/4.-Key-Messages-and-Recommendations-1.pdf)
- ClinicalTrials.gov NCT04344613: a point-of-care analyser for bedside ABO/D checking. [clinicaltrials.gov](https://clinicaltrials.gov/study/NCT04344613)
- BusinessDay Nigeria, on blood transfusion errors in Nigeria's health system. [businessday.ng](https://businessday.ng/?p=655978)

> ⚠️ HemoSense is a prototype for research and demonstration. Confirm every result with standard laboratory grouping and cross-matching before transfusion.
