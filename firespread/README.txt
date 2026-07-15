Fire Spread Simulator
=====================

Requirements
------------
Python 3.11 or newer (3.14 recommended)
Download from: https://www.python.org/downloads/

Setup (run once)
----------------
Open a terminal in this folder and run:

    pip install -r requirements.txt

Run
---
    python main.py

Controls
--------
The whole app is one resizable window: an always-on slippy map with the fire
simulation drawn as a live overlay on the terrain. No area-selection step.

- Drag map to pan
- Scroll wheel to zoom
- Resize the window freely
- "🔥 Ignite" button: arm ignite mode, then click the map to start a fire
  (the simulation area is captured automatically from the current view the
   first time you ignite; press Reset to relocate it)
- Speed +/- to control how fast simulated time passes
- Play/Pause and Reset buttons

API Keys (already configured in config.py)
------------------------------------------
- OpenTopography: for elevation data
- Sentinel Hub: for satellite vegetation data
- Open-Meteo: free weather data, no key needed
