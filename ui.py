"""
Single-view fire spread UI.

The whole app is one resizable window: a slippy OpenStreetMap map that is always
visible, with the live fire simulation drawn as a translucent overlay glued to
the terrain. There is no area-selection step and no screen switching.

Interaction
-----------
- Drag            : pan the map
- Scroll wheel    : zoom
- "Ignite"        : arm ignite mode, then click the map to start a fire
- Bottom toolbar  : Play/Pause, Reset, Speed, elapsed time, weather/status

The simulation region is captured automatically from the current viewport the
first time you ignite (or after Reset), so you simply zoom to an area and light
it up. The fire is geo-anchored: it stays put on the map while you pan and zoom.
"""

import math
import threading

import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import numpy as np

from simulation import FireSimulation, EMPTY, BURNING, BURNED
from data_fetcher import fetch_all
from slippy_map import SlippyMap

FIRE_RGBA = {
    BURNING: (255, 80, 0, 235),
    BURNED:  (40, 40, 40, 205),
}

TARGET_CELL_M = 30.0   
MIN_CELLS = 24
MAX_CELLS = 220

SPEED_STEPS = [1, 2, 5, 10, 30, 60]


# ---------------------------------------------------------------------------
# Fire overlay — draws the simulation grid on top of the slippy map
# ---------------------------------------------------------------------------

class FireOverlay:
    """
    Renders the fire grid as a translucent RGBA image reprojected onto the
    slippy map. Content is rebuilt only when the simulation changes; on pan /
    zoom / resize the cached image is just re-placed and clipped to the canvas.
    """

    def __init__(self, canvas, slippy):
        self.canvas = canvas
        self.slippy = slippy
        self.bbox = None          # (west, south, east, north) of the sim region
        self._src = None          # PIL RGBA image at grid resolution
        self._photo = None
        self._item = None         # canvas image item
        self._border = None       # canvas rectangle showing region bounds

    def set_region(self, bbox):
        self.bbox = bbox

    def clear(self):
        self.bbox = None
        self._src = None
        self._photo = None
        if self._item is not None:
            self.canvas.delete(self._item)
            self._item = None
        if self._border is not None:
            self.canvas.delete(self._border)
            self._border = None

    def refresh_content(self, sim):
        """Rebuild the RGBA grid image from the simulation state, then redraw."""
        grid = np.asarray(sim.grid, dtype=np.uint8)          # (H, W)
        h, w = grid.shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[grid == BURNING] = FIRE_RGBA[BURNING]
        rgba[grid == BURNED] = FIRE_RGBA[BURNED]
        self._src = Image.fromarray(rgba, mode="RGBA")
        self.redraw()

    def redraw(self):
        """Reproject the cached grid image onto the current map view."""
        if self.bbox is None:
            return

        west, south, east, north = self.bbox
        # Region corners in canvas pixels (NW -> top-left, SE -> bottom-right)
        x0, y0 = self.slippy.latlon_to_canvas(north, west)
        x1, y1 = self.slippy.latlon_to_canvas(south, east)
        rx0, ry0 = min(x0, x1), min(y0, y1)
        rx1, ry1 = max(x0, x1), max(y0, y1)

        self._draw_border(rx0, ry0, rx1, ry1)

        if self._src is None:
            return
        rect_w = rx1 - rx0
        rect_h = ry1 - ry0
        if rect_w < 1 or rect_h < 1:
            self._hide_item()
            return

        # Clip the region rectangle to the visible canvas so we never build an
        # image larger than the screen (keeps panning smooth at any zoom).
        cw, ch = self.slippy.width, self.slippy.height
        vx0, vy0 = max(0.0, rx0), max(0.0, ry0)
        vx1, vy1 = min(cw, rx1), min(ch, ry1)
        if vx1 - vx0 < 1 or vy1 - vy0 < 1:
            self._hide_item()
            return

        # Corresponding crop box in the source (grid-resolution) image.
        sw, sh = self._src.size
        cx0 = int((vx0 - rx0) / rect_w * sw)
        cy0 = int((vy0 - ry0) / rect_h * sh)
        cx1 = max(cx0 + 1, int(math.ceil((vx1 - rx0) / rect_w * sw)))
        cy1 = max(cy0 + 1, int(math.ceil((vy1 - ry0) / rect_h * sh)))
        crop = self._src.crop((cx0, cy0, min(cx1, sw), min(cy1, sh)))

        out_w = max(1, int(round(vx1 - vx0)))
        out_h = max(1, int(round(vy1 - vy0)))
        disp = crop.resize((out_w, out_h), Image.NEAREST)

        self._photo = ImageTk.PhotoImage(disp)
        if self._item is None:
            self._item = self.canvas.create_image(
                int(round(vx0)), int(round(vy0)), anchor="nw", image=self._photo)
        else:
            self.canvas.coords(self._item, int(round(vx0)), int(round(vy0)))
            self.canvas.itemconfigure(self._item, image=self._photo, state="normal")
        self.canvas.tag_raise(self._item)
        if self._border is not None:
            self.canvas.tag_raise(self._border)

    def _draw_border(self, rx0, ry0, rx1, ry1):
        coords = (rx0, ry0, rx1, ry1)
        if self._border is None:
            self._border = self.canvas.create_rectangle(
                *coords, outline="#FF8C00", width=2, dash=(6, 4))
        else:
            self.canvas.coords(self._border, *coords)
            self.canvas.tag_raise(self._border)

    def _hide_item(self):
        if self._item is not None:
            self.canvas.itemconfigure(self._item, state="hidden")


# ---------------------------------------------------------------------------
# Root application — one window, one canvas, one bottom toolbar
# ---------------------------------------------------------------------------

class App(tk.Tk):
    BAR_BG = "#2b2b3d"
    BTN_BG = "#3d3d52"
    IGNITE_ON = "#ff5522"

    def __init__(self):
        super().__init__()
        self.title("Fire Spread Simulator")
        self.geometry("960x720")
        self.minsize(560, 420)

        # Simulation state
        self.sim = None
        self.map_data = None
        self.paused = True
        self.steps_per_tick = 5
        self.ignite_mode = False
        self._loading = False
        self._tick_id = None

        self._build()
        self._tick()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _build(self):
        # Bottom toolbar first so it reserves its height.
        bar = tk.Frame(self, bg=self.BAR_BG)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._build_toolbar(bar)

        # Map canvas fills everything above the toolbar.
        self.canvas = tk.Canvas(self, bg="#1a1a2e", highlightthickness=0,
                                cursor="fleur")
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.slippy = SlippyMap(self.canvas, 960, 660,
                                lat=45.8, lon=15.97, zoom=11)
        self.overlay = FireOverlay(self.canvas, self.slippy)

        self.slippy.on_redraw = self.overlay.redraw
        self.slippy.on_click = self._on_map_click
        self.slippy.on_status = self._set_status

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Motion>", self._on_motion)

        self._set_status("Arm 🔥 Ignite, then click the map to start a fire")

    def _build_toolbar(self, bar):
        def button(text, cmd, width=8):
            return tk.Button(bar, text=text, width=width, command=cmd,
                             bg=self.BTN_BG, fg="white", activebackground="#50506a",
                             activeforeground="white", relief=tk.RAISED, bd=1,
                             highlightthickness=0)

        self.btn_ignite = button("🔥 Ignite", self._toggle_ignite, width=10)
        self.btn_ignite.pack(side=tk.LEFT, padx=(8, 4), pady=6)

        self.btn_pause = button("▶ Play", self._toggle_pause, width=8)
        self.btn_pause.pack(side=tk.LEFT, padx=2, pady=6)

        button("↺ Reset", self._reset).pack(side=tk.LEFT, padx=2, pady=6)

        tk.Label(bar, text="Speed:", bg=self.BAR_BG, fg="#ccc").pack(
            side=tk.LEFT, padx=(12, 2))
        button("−", self._slower, width=2).pack(side=tk.LEFT)
        self.speed_label = tk.Label(bar, text=self._speed_text(), width=8,
                                    bg=self.BAR_BG, fg="white")
        self.speed_label.pack(side=tk.LEFT)
        button("+", self._faster, width=2).pack(side=tk.LEFT)

        self.time_label = tk.Label(bar, text="⏱ 0s", bg=self.BAR_BG,
                                   fg="#88ff88", font=("Segoe UI", 10))
        self.time_label.pack(side=tk.LEFT, padx=(14, 6))

        # Right side: coordinates + status/weather
        self.coord_label = tk.Label(bar, text="", bg=self.BAR_BG, fg="#888")
        self.coord_label.pack(side=tk.RIGHT, padx=10)
        self.status_label = tk.Label(bar, text="", bg=self.BAR_BG, fg="#aaa")
        self.status_label.pack(side=tk.RIGHT, padx=10)

    # ------------------------------------------------------------------
    # Map / canvas events
    # ------------------------------------------------------------------

    def _on_resize(self, event):
        # Resizing the canvas resizes the map, which triggers overlay.redraw
        # through the on_redraw hook.
        self.slippy.resize(event.width, event.height)

    def _on_motion(self, event):
        lat, lon = self.slippy.canvas_to_latlon(event.x, event.y)
        self.coord_label.config(text=f"{lat:.5f}, {lon:.5f}")

    def _on_map_click(self, lat, lon):
        if not self.ignite_mode or self._loading:
            return
        if self.sim is None:
            # No active region yet: capture the current viewport and fetch data.
            self._begin_region(lat, lon)
        else:
            self._ignite_at(lat, lon)

    # ------------------------------------------------------------------
    # Region setup (async data fetch)
    # ------------------------------------------------------------------

    def _viewport_bbox(self):
        latN, lonW = self.slippy.canvas_to_latlon(0, 0)
        latS, lonE = self.slippy.canvas_to_latlon(self.slippy.width,
                                                  self.slippy.height)
        west, east = min(lonW, lonE), max(lonW, lonE)
        south, north = min(latN, latS), max(latN, latS)
        return west, south, east, north

    def _grid_dims(self, bbox):
        # Choose the cell count so each cell is ~TARGET_CELL_M metres, regardless
        # of zoom. Clamp to [MIN_CELLS, MAX_CELLS]: when the ignited area is very
        # large the cap makes cells grow (keeps the CA fast) rather than stalling.
        west, south, east, north = bbox
        mid = math.radians((south + north) / 2)
        m_w = (east - west) * 111320 * math.cos(mid)
        m_h = (north - south) * 110540
        gw = int(max(MIN_CELLS, min(MAX_CELLS, round(m_w / TARGET_CELL_M))))
        gh = int(max(MIN_CELLS, min(MAX_CELLS, round(m_h / TARGET_CELL_M))))
        return gw, gh

    def _begin_region(self, ignite_lat, ignite_lon):
        bbox = self._viewport_bbox()
        gw, gh = self._grid_dims(bbox)
        self._loading = True
        self._set_status("Fetching map data… (elevation, vegetation, weather)")
        self.config(cursor="watch")

        west, south, east, north = bbox

        def run():
            try:
                data = fetch_all(west, south, east, north, gw, gh)
                self.after(0, lambda: self._region_ready(
                    bbox, gw, gh, data, ignite_lat, ignite_lon))
            except Exception as ex:
                msg = str(ex)
                self.after(0, lambda: self._region_failed(msg))

        threading.Thread(target=run, daemon=True).start()

    def _region_ready(self, bbox, gw, gh, data, ignite_lat, ignite_lon):
        self._loading = False
        self.config(cursor="")
        self.map_data = data

        self.sim = FireSimulation(gw, gh)
        self.sim.load_environment(
            slope         = data.slope,
            aspect        = data.aspect,
            fuel_model    = data.fuel_model,
            fuel_moisture = data.fuel_moisture_grid,
            wind_speed    = data.wind_speed,
            wind_dir      = data.wind_dir,
            cell_size_m   = data.cell_size_m,
        )
        self.overlay.set_region(bbox)
        self._ignite_at(ignite_lat, ignite_lon)
        self._set_weather_status()

    def _region_failed(self, msg):
        self._loading = False
        self.config(cursor="")
        self._set_status("Fetch failed")
        messagebox.showerror("Error", msg)

    # ------------------------------------------------------------------
    # Ignition / simulation controls
    # ------------------------------------------------------------------

    def _ignite_at(self, lat, lon):
        if self.sim is None or self.overlay.bbox is None:
            return
        west, south, east, north = self.overlay.bbox
        if not (west <= lon <= east and south <= lat <= north):
            self._set_status("Outside sim area — Reset to start a fire elsewhere")
            return
        gx = int((lon - west) / (east - west) * self.sim.width)
        gy = int((north - lat) / (north - south) * self.sim.height)
        gx = max(0, min(self.sim.width - 1, gx))
        gy = max(0, min(self.sim.height - 1, gy))
        self.sim.ignite(gx, gy)
        self.paused = False
        self.btn_pause.config(text="⏸ Pause")
        self.overlay.refresh_content(self.sim)

    def _toggle_ignite(self):
        self.ignite_mode = not self.ignite_mode
        if self.ignite_mode:
            self.canvas.config(cursor="crosshair")
            self.btn_ignite.config(relief=tk.SUNKEN, bg=self.IGNITE_ON)
            self._set_status("Ignite armed — click the map to light a fire")
        else:
            self.canvas.config(cursor="fleur")
            self.btn_ignite.config(relief=tk.RAISED, bg=self.BTN_BG)
            self._set_weather_status()

    def _toggle_pause(self):
        if self.sim is None:
            self._set_status("Nothing to play — ignite a fire first")
            return
        self.paused = not self.paused
        self.btn_pause.config(text="▶ Play" if self.paused else "⏸ Pause")
        if not self.paused:
            self.overlay.refresh_content(self.sim)

    def _reset(self):
        if self.sim is not None:
            self.sim.reset()
        self.sim = None
        self.map_data = None
        self.overlay.clear()
        self.paused = True
        self.btn_pause.config(text="▶ Play")
        self.time_label.config(text="⏱ 0s")
        self._set_status("Reset — arm 🔥 Ignite and click to start a new fire")

    def _slower(self):
        idx = next((i for i, s in enumerate(SPEED_STEPS)
                    if s >= self.steps_per_tick), len(SPEED_STEPS) - 1)
        self.steps_per_tick = SPEED_STEPS[max(0, idx - 1)]
        self.speed_label.config(text=self._speed_text())

    def _faster(self):
        idx = next((i for i, s in enumerate(SPEED_STEPS)
                    if s >= self.steps_per_tick), 0)
        self.steps_per_tick = SPEED_STEPS[min(len(SPEED_STEPS) - 1, idx + 1)]
        self.speed_label.config(text=self._speed_text())

    def _speed_text(self):
        m = self.steps_per_tick
        return f"{m} min/s" if m < 60 else f"{m // 60} h/s"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _tick(self):
        if self.sim is not None and not self.paused and self.sim.running:
            for _ in range(self.steps_per_tick):
                self.sim.step(dt_seconds=60)
                if not self.sim.running:
                    break
            self.overlay.refresh_content(self.sim)
            self.time_label.config(text=f"⏱ {self.sim.elapsed_str()}")
        self._tick_id = self.after(1000, self._tick)

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def _set_status(self, text):
        self.status_label.config(text=text)

    def _set_weather_status(self):
        if self.map_data is None:
            self._set_status("Arm 🔥 Ignite, then click the map to start a fire")
            return
        md = self.map_data
        w = md.weather
        self._set_status(
            f"Wind {md.wind_speed:.1f} m/s @ {md.wind_dir:.0f}°  |  "
            f"T {w['temperature']:.0f}°C  RH {w['humidity']:.0f}%  |  "
            f"FM {w['fuel_moisture'] * 100:.1f}%  |  Cell {md.cell_size_m:.0f} m")


if __name__ == "__main__":
    App().mainloop()
