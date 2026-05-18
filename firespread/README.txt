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
- Drag map to pan
- Scroll wheel to zoom
- Shift + drag to select simulation area
- Click on the loaded map to ignite fire
- Speed +/- to control how fast simulated time passes
- Play/Pause and Reset buttons

API Keys (already configured in config.py)
------------------------------------------
- OpenTopography: for elevation data
- Sentinel Hub: for satellite vegetation data
- Open-Meteo: free weather data, no key needed
