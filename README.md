# WiFi Location

A small experiment that uses a MacBook's WiFi signals to roughly estimate indoor position and detect motion.

## What it does

- **Plan:** Draw your room as a set of rectangular areas.
- **Ölçüm (Measure):** Walk around with the MacBook and click where you are standing on the map. The WiFi "fingerprint" of each spot is recorded and a signal map builds up.
- **Canlı (Live):** Estimates where the MacBook is by comparing live readings with the recorded fingerprints (grid-based Bayes filter).
- **Hareket (Motion):** With the MacBook sitting still, tells whether something is moving in the room from the jitter of the connected network's signal.

The interface is in Turkish.

## Running

Requirements: macOS, Python 3, Xcode command line tools (for the `swift` command).

```bash
python3 server.py
```

Then open `http://localhost:8765` in a browser. Double-clicking `baslat.command` in Finder does the same.

## Limits

- macOS allows a full WiFi scan only about every 13 seconds, so the position updates slowly.
- Accuracy is at room/zone level (roughly 1–2 meters), not GPS-like.
- The position shown is the MacBook's; an iPhone does not allow WiFi scanning.
- Motion detection only says "yes / no", it does not give a position.

## Files

| File | Role |
|---|---|
| `wifiscan.swift` | Reads signals through CoreWLAN |
| `server.py` | Local server, stores measurements, detects motion |
| `locate.py` | Signal map and position filter |
| `simulate.py` | Tries the algorithm in a simulated flat |
| `index.html` | Interface |

Measurements are kept only on your own computer in `data.json`, which is not committed.
