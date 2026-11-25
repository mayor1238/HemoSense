/*
  HemoSense reader firmware 0.7.0-cloud: your AD5933 measurement code, connecting straight to
  HemoSense on Nobus Cloud (no laptop bridge needed).
  Calibration setup unchanged: 10k series resistor + 5k reference on C0 (15k total).

  What changed from 0.6.1-wifi:
  - The reader joins a WiFi WITH internet (phone hotspot or venue WiFi, 2.4 GHz, no login page)
    and opens a WebSocket to the server:  ws://SERVER_IP:8080/ws/reader?device_id=...&key=...
  - The reader no longer makes its own WiFi or serves a web page (the app lives on Nobus).
  - Commands and replies are the same as before (start, stop, calibrate, sync / info, ack,
    reading, done, error), so the measurement code below is unchanged.
  - Button flow unchanged: calibrate -> snapshot -> recalibrate. Serial still prints "t_ms,z1,z2,z3".

  Libraries: "WebSockets" by Markus Sattler, "ArduinoJson" v7, "Adafruit SSD1306",
  "Adafruit GFX Library", plus your AD5933 library. webapp.h is no longer needed.
*/

#include <Wire.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "AD5933.h"

// ---------------- settings: CHANGE THESE ----------------
#define SIMULATE_READINGS 0          // 1 = fake readings (labelled "simulated" in the app)

const char* DEVICE_ID   = "HS-DEV-001";
const char* FIRMWARE    = "0.7.0-cloud-10k";

const char* WIFI_SSID   = "YourHotspotName";      // phone hotspot or venue WiFi (2.4 GHz)
const char* WIFI_PASS   = "YourHotspotPassword";

const char* SERVER_HOST = "102.209.46.251";       // your Nobus server IP
const uint16_t SERVER_PORT = 8080;                // the server's reader entrance (plain WebSocket)
const char* DEVICE_KEY  = "PASTE_DEVICE_KEY_HERE";   // DEVICE_KEY from the server's .env (never commit the real one)
// ---------------------------------------------------------

// OLED Display Configuration
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// AD5933 Configuration
#define START_FREQ  (30000)
#define FREQ_INCR   (100)
#define NUM_INCR    (10)
#define REF_RESIST  (15000.0) // 10k speed bump + 5k cal resistor on C0
#define SPEED_BUMP  (10000.0) // Physical 10k series resistor on the mux SIG line
#define MUX_OFFSET  (140.0)   // Internal MUX + wire parasitic resistance
#define MUX_SETTLE_MS (100)   // wait after switching channel (your tested value)

double systemGain = 0;        // single global gain factor
bool adOk = false;            // AD5933 started correctly

// Hardware Pins
const int s0 = 16;
const int s1 = 17;
const int s2 = 18;
const int s3 = 19;
const int buttonPin = 25;
const int buzzerPin = 26;
const int ledPin = 27;
const uint8_t WELL_MUX[3] = {1, 2, 3};   // Anti-A, Anti-B, Anti-D

int systemState = 0;   // 0 = not calibrated, 1 = calibrated, 2 = snapshot shown

struct SensorData {
  float impedance;
  float phase;
};

// ---------------- WiFi / test state ----------------
// ---------------- cloud link / test state ----------------
WebSocketsClient ws;
bool cloudUp = false;
bool calRequested = false;   // calibrate command from the app, run from loop()
String serverMsg;

struct Reading { uint32_t t; float re[3]; float im[3]; float z[3]; };
const uint16_t MAX_READINGS = 400;
Reading buf[MAX_READINGS];

struct Run {
  bool running = false;
  String testId, shortId;
  uint32_t startMs = 0, nextMs = 0, durationMs = 120000, intervalMs = 2000;
  uint16_t n = 0;
} run;

// --- CUSTOM I2C VOLTAGE LOCK FUNCTIONS ---
// Bypasses library bugs to ensure Range 4 (200mV) & Gain x1 (0x03) never change.
void setAD5933_Standby() {
  Wire.beginTransmission(0x0D);
  Wire.write(0x80); Wire.write(0xB3); // 1011 0011 (Standby)
  Wire.endTransmission();
}

void setAD5933_InitStartFreq() {
  Wire.beginTransmission(0x0D);
  Wire.write(0x80); Wire.write(0x13); // 0001 0011 (Init Start Freq)
  Wire.endTransmission();
}

void setAD5933_StartSweep() {
  Wire.beginTransmission(0x0D);
  Wire.write(0x80); Wire.write(0x23); // 0010 0011 (Start Sweep)
  Wire.endTransmission();
}
// -----------------------------------------

// ---------------- screen ----------------
void screen(const String& a, const String& b = "", const String& c = "", const String& d = "") {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(WHITE);
  display.setCursor(0, 0);  display.println(a);
  display.setCursor(0, 16); display.println(b);
  display.setCursor(0, 32); display.println(c);
  display.setCursor(0, 48); display.println(d);
  display.display();
}


void screenIdle() {
  String l1 = WiFi.status() == WL_CONNECTED ? "WiFi " + WiFi.SSID() : "WiFi: connecting...";
  String l2 = cloudUp ? "Nobus: connected" : (serverMsg.length() ? serverMsg : "Nobus: connecting...");
  String l3 = String(DEVICE_ID);
  String l4 = systemState == 0 ? "Press to Calibrate" : "Ready - start on app";
  screen(l1, l2, l3, l4);
}

// ---------------- measurement ----------------
void setMuxChannel(int channel) {
  digitalWrite(s0, bitRead(channel, 0));
  digitalWrite(s1, bitRead(channel, 1));
  digitalWrite(s2, bitRead(channel, 2));
  digitalWrite(s3, bitRead(channel, 3));
}

// One AD5933 measurement at the start frequency. Returns false on timeout,
// so a missing chip can't freeze the reader (and the WiFi link) forever.
bool getRawComplex(int& real, int& imag) {
  setAD5933_Standby();
  setAD5933_InitStartFreq();
  delay(10);
  setAD5933_StartSweep();

  uint32_t t0 = millis();
  while ((AD5933::readStatusRegister() & 0x02) != 0x02) {
    if (millis() - t0 > 500) { setAD5933_Standby(); return false; }
    delay(1);
  }
  bool ok = AD5933::getComplexData(&real, &imag);
  setAD5933_Standby();
  return ok;
}

double getRawMagnitude() {
  int real, imag;
  if (!getRawComplex(real, imag)) return -1;
  return sqrt(pow(real, 2) + pow(imag, 2));
}

#if SIMULATE_READINGS
const char* SIM_TYPE = "B+";
float simBase[3] = {420, 510, 630};
float gaussNoise() {
  float u = (esp_random() + 1.0f) / 4294967297.0f, v = (esp_random() + 1.0f) / 4294967297.0f;
  return sqrtf(-2.0f * logf(u)) * cosf(6.2831853f * v);
}
#endif

// Measure one well (0 = Anti-A, 1 = Anti-B, 2 = Anti-D).
// re/im are the raw AD5933 values; z is the fluid impedance with offsets removed.
bool measureWell(uint8_t well, float tSec, float& re, float& im, float& z) {
#if SIMULATE_READINGS
  bool pos[3] = { strchr(SIM_TYPE, 'A') != nullptr, strchr(SIM_TYPE, 'B') != nullptr, strchr(SIM_TYPE, '+') != nullptr };
  float amp = pos[well] ? (well == 2 ? 8.0f : 11.0f) : 0.0f;
  float sig = 1.0f / (1.0f + expf(-(tSec - 35.0f) / 9.0f));
  z = simBase[well] * (1.0f + (amp * sig + gaussNoise() * 0.6f) / 100.0f);
  re = 1e7f / (z + SPEED_BUMP + MUX_OFFSET); im = -re * 0.05f;
  return true;
#else
  setMuxChannel(WELL_MUX[well]);
  delay(MUX_SETTLE_MS);
  int r, i;
  if (!getRawComplex(r, i)) return false;
  re = r; im = i;
  double mag = sqrt(pow(r, 2) + pow(i, 2));
  if (mag <= 0 || systemGain <= 0) return false;
  float total_impedance = 1.0 / (mag * systemGain);
  // Subtract both the speed bump and the multiplexer parasitic resistance
  z = total_impedance - SPEED_BUMP - MUX_OFFSET;
  if (z < 0) z = 0;
  return true;
#endif
}

SensorData getSingleFrequencyData(uint8_t well) {
  float re, im, z;
  if (measureWell(well, 0, re, im, z)) return {z, (float)(atan2(im, re) * 180.0 / PI)};
  return {0.0, 0.0};
}

// ---------------- messaging ----------------
void sendJson(int client, JsonDocument& d) {
  if (!cloudUp) return;            // offline: the server asks for a "sync" when we are back
  String out; serializeJson(d, out);
  ws.sendTXT(out);
}

void sendError(int client, const char* msg) {
  JsonDocument d; d["type"] = "error"; d["msg"] = msg; sendJson(client, d);
}

void sendInfo(int client) {
  JsonDocument d;
  d["type"] = "info"; d["device_id"] = DEVICE_ID; d["firmware"] = FIRMWARE;
  d["simulate"] = (bool)SIMULATE_READINGS;
  d["sensor_ok"] = adOk || SIMULATE_READINGS;
  d["calibrated"] = systemGain > 0;
  d["state"] = run.running ? "running" : "idle";
  if (run.running) { d["test_id"] = run.testId; d["n"] = run.n; }
  sendJson(client, d);
}

void sendReading(int client, uint16_t i) {
  Reading& r = buf[i];
  JsonDocument d;
  d["type"] = "reading"; d["test_id"] = run.testId; d["i"] = i; d["t_ms"] = r.t;
  JsonArray z = d["z"].to<JsonArray>(), re = d["re"].to<JsonArray>(), im = d["im"].to<JsonArray>();
  for (int k = 0; k < 3; k++) { z.add(roundf(r.z[k] * 100) / 100); re.add(r.re[k]); im.add(r.im[k]); }
  sendJson(client, d);
}

// ---------------- calibration and snapshot (your button flow) ----------------
bool runCalibration() {
  digitalWrite(ledPin, HIGH);

  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(WHITE);
  display.setCursor(0, 0);
  display.println("Calibrating...");
  display.drawRect(14, 30, 100, 10, WHITE);
  display.display();

  for (int i = 0; i <= 96; i += 4) {
    display.fillRect(16, 32, i, 6, WHITE);
    display.display();
    delay(20);
    ws.loop();
  }

  bool ok;
#if SIMULATE_READINGS
  systemGain = 1.0; ok = true;
#else
  // Route to calibration resistor on Channel 0
  setMuxChannel(0);
  delay(200);
  // Manual Calibration at 200mV
  double rawMag = getRawMagnitude();
  ok = rawMag > 0;
  if (ok) systemGain = 1.0 / (REF_RESIST * rawMag);
#endif
  Serial.println(ok ? "\n[Hardware Calibrated]" : "\n[Calibration Failed - No Signal]");

  if (ok) { tone(buzzerPin, 2000, 100); delay(150); tone(buzzerPin, 2500, 100); }
  else    { tone(buzzerPin, 400, 400); }

  JsonDocument d; d["type"] = "calibrated"; d["ok"] = ok; d["gain"] = systemGain;
  sendJson(-1, d);

  digitalWrite(ledPin, LOW);
  if (ok) systemState = 1;
  screen(ok ? "Calibration Done!" : "Calibration FAILED", "", ok ? "Start test on app" : "Check AD5933 / C0", "or press for snapshot");
  return ok;
}

void takeSnapshotReading() {
  digitalWrite(ledPin, HIGH);
  screen("Reading Channels...");

  SensorData ch1 = getSingleFrequencyData(0);
  SensorData ch2 = getSingleFrequencyData(1);
  SensorData ch3 = getSingleFrequencyData(2);

  Serial.println("--- Snapshot Reading ---");
  Serial.print("CH1: "); Serial.print(ch1.impedance); Serial.println(" Ohms");
  Serial.print("CH2: "); Serial.print(ch2.impedance); Serial.println(" Ohms");
  Serial.print("CH3: "); Serial.print(ch3.impedance); Serial.println(" Ohms");

  JsonDocument d; d["type"] = "snapshot";
  JsonArray z = d["z"].to<JsonArray>(); z.add(ch1.impedance); z.add(ch2.impedance); z.add(ch3.impedance);
  sendJson(-1, d);

  display.clearDisplay();
  display.setCursor(0, 0);
  display.println("Snapshot Result:");
  display.print("CH1: "); display.println(ch1.impedance, 0);
  display.print("CH2: "); display.println(ch2.impedance, 0);
  display.print("CH3: "); display.println(ch3.impedance, 0);
  display.println("Press to Recalibrate");
  display.display();

  tone(buzzerPin, 1000, 100);
  digitalWrite(ledPin, LOW);
}

// ---------------- test run ----------------
void finishRun(const char* reason) {
  if (!run.running) return;
  run.running = false;
  JsonDocument d; d["type"] = "done"; d["test_id"] = run.testId; d["n"] = run.n; d["reason"] = reason;
  sendJson(-1, d);
  tone(buzzerPin, 2500, 150);
  digitalWrite(ledPin, LOW);
  screen("Test finished", run.shortId, String(run.n) + " readings sent", "Result is on the app");
}

void startRun(int client, JsonDocument& cmd) {
  if (run.running) { sendError(client, "Reader is busy with another test"); return; }
  if (!adOk && !SIMULATE_READINGS) { sendError(client, "AD5933 not responding. Check wiring and restart the reader."); return; }
  if (systemGain <= 0) { sendError(client, "Reader is not calibrated. Tap Calibrate reader in the app or press the button."); return; }

  String id = cmd["test_id"] | "";
  if (id.length() < 8 || id.length() > 40) { sendError(client, "Missing or bad test_id"); return; }
  float dur = cmd["duration_s"] | 120.0f, iv = cmd["interval_s"] | 2.0f;
  if (dur < 10 || dur > 600) { sendError(client, "Sweep length must be 10-600 s"); return; }
  if (iv < 1.0f || iv > 30)  { sendError(client, "Interval must be 1-30 s (each reading takes about 0.4 s)"); return; }
  if (dur / iv + 1 > MAX_READINGS) { sendError(client, "Too many readings for memory"); return; }

  run.testId = id;
  run.shortId = String((const char*)(cmd["short_id"] | "HS-?"));
  run.durationMs = (uint32_t)(dur * 1000);
  run.intervalMs = (uint32_t)(iv * 1000);
  run.n = 0; run.startMs = millis(); run.nextMs = run.startMs; run.running = true;

  JsonDocument a; a["type"] = "ack"; a["cmd"] = "start"; a["test_id"] = run.testId;
  a["short_id"] = run.shortId; a["duration_s"] = dur; a["interval_s"] = iv;
  sendJson(-1, a);

  digitalWrite(ledPin, HIGH);
  tone(buzzerPin, 1500, 80);
  screen("TEST - check app ID:", run.shortId, "0 / " + String((int)dur) + " s");
}

void runStep() {
  if (!run.running || (int32_t)(millis() - run.nextMs) < 0) return;
  uint32_t t = millis() - run.startMs;
  if (t > run.durationMs + run.intervalMs / 2 || run.n >= MAX_READINGS) { finishRun("complete"); return; }

  Reading& r = buf[run.n];
  r.t = t;
  for (uint8_t k = 0; k < 3; k++) {
    if (!measureWell(k, t / 1000.0f, r.re[k], r.im[k], r.z[k])) {
      sendError(-1, "AD5933 did not return valid data");
      finishRun("sensor_error");
      return;
    }
    ws.loop();   // keep the WiFi link alive between channels
  }
  sendReading(-1, run.n);
  Serial.printf("%lu,%.2f,%.2f,%.2f\n", (unsigned long)r.t, r.z[0], r.z[1], r.z[2]);
  run.n++;
  run.nextMs += run.intervalMs;
  if ((int32_t)(millis() - run.nextMs) > 0) run.nextMs = millis();   // fell behind: don't burst

  if (t >= run.durationMs) finishRun("complete");
  else screen("TEST - check app ID:", run.shortId, String(t / 1000) + " / " + String(run.durationMs / 1000) + " s",
              String(run.n) + " readings");
}

// ---------------- commands from the server ----------------
void onWs(WStype_t type, uint8_t* payload, size_t len) {
  if (type == WStype_CONNECTED) {
    cloudUp = true; serverMsg = "";
    Serial.println("Connected to HemoSense on Nobus");
    sendInfo(-1);
    if (!run.running && systemState != 2) screenIdle();
    return;
  }
  if (type == WStype_DISCONNECTED) {
    if (cloudUp) Serial.println("Nobus link lost, retrying...");
    bool was = cloudUp; cloudUp = false;
    if (was && !run.running && systemState != 2) screenIdle();
    return;
  }
  if (type != WStype_TEXT) return;

  JsonDocument cmd;
  if (deserializeJson(cmd, payload, len)) return;
  String t = cmd["type"] | "";

  if (t == "hello") sendInfo(-1);
  else if (t == "start") startRun(-1, cmd);
  else if (t == "stop") {
    if (run.running && run.testId == (cmd["test_id"] | "")) finishRun("stopped");
  }
  else if (t == "calibrate") {
    if (run.running) sendError(-1, "Can't calibrate during a test");
    else calRequested = true;      // done in loop(), not inside this callback
  }
  else if (t == "sync") {
    if (run.testId == (cmd["test_id"] | "")) {
      for (uint16_t i = 0; i < run.n; i++) sendReading(-1, i);
      if (!run.running && run.n > 0) {
        JsonDocument d; d["type"] = "done"; d["test_id"] = run.testId; d["n"] = run.n; d["reason"] = "complete";
        sendJson(-1, d);
      }
    }
  }
  else if (t == "ping") { JsonDocument d; d["type"] = "pong"; sendJson(-1, d); }
  else if (t == "error") {         // the server refused us (usually a wrong DEVICE_KEY)
    serverMsg = "Server: " + String((const char*)(cmd["msg"] | "refused"));
    Serial.println(serverMsg);
    if (!run.running) screenIdle();
  }
}

// ---------------- setup / loop ----------------
void setup() {
  Wire.begin();
  Serial.begin(115200);

  pinMode(s0, OUTPUT);
  pinMode(s1, OUTPUT);
  pinMode(s2, OUTPUT);
  pinMode(s3, OUTPUT);
  pinMode(buzzerPin, OUTPUT);
  pinMode(ledPin, OUTPUT);
  pinMode(buttonPin, INPUT_PULLUP);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println(F("SSD1306 allocation failed"));   // carry on without the screen
  }

  adOk = AD5933::reset() &&
         AD5933::setInternalClock(true) &&
         AD5933::setStartFrequency(START_FREQ) &&
         AD5933::setIncrementFrequency(FREQ_INCR) &&
         AD5933::setNumberIncrements(NUM_INCR);
  if (!adOk) Serial.println("AD5933 Init Failed!");   // WiFi still starts so the app can show the error


  // ----- WiFi with internet -----
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  screen("Joining WiFi...", WIFI_SSID, String(DEVICE_ID), FIRMWARE);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(250);
  if (WiFi.status() == WL_CONNECTED) Serial.println("WiFi OK: " + WiFi.localIP().toString());
  else Serial.println("WiFi not connected yet; will keep trying");

  // ----- link to HemoSense on Nobus -----
  String path = String("/ws/reader?device_id=") + DEVICE_ID + "&key=" + DEVICE_KEY;
  ws.begin(SERVER_HOST, SERVER_PORT, path.c_str());
  ws.onEvent(onWs);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);

  if (!adOk && !SIMULATE_READINGS) screen("AD5933 Init Failed!", "Check wiring", "App will show", "the error");
  else screenIdle();
}

void loop() {
  ws.loop();
  runStep();

  if (calRequested && !run.running) {
    calRequested = false;
    runCalibration();
    sendInfo(-1);
  }

  static uint32_t lastWifi = 0;   // refresh the screen if WiFi drops or returns
  static bool wifiWas = false;
  if (millis() - lastWifi > 2000) {
    lastWifi = millis();
    bool now = WiFi.status() == WL_CONNECTED;
    if (now != wifiWas) { wifiWas = now; if (!run.running && systemState != 2) screenIdle(); }
  }


  // Button: same flow as before (calibrate -> snapshot -> recalibrate), ignored during a test
  if (!run.running && digitalRead(buttonPin) == LOW) {
    delay(50);
    while (digitalRead(buttonPin) == LOW) { ws.loop(); delay(5); }

    if (systemState == 0 || systemState == 2) {
      runCalibration();
      sendInfo(-1);
    } else if (systemState == 1) {
      takeSnapshotReading();
      systemState = 2;
    }
  }
}
