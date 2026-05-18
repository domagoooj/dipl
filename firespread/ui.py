import tkinter as tk
from tkinter import messagebox
import threading
from PIL import Image, ImageTk

from simulation import FireSimulation, EMPTY, BURNING, BURNED
from data_fetcher import fetch_all, MapData
from slippy_map import SlippyMap

MAP_W = 960
MAP_H = 960

GRID_W = 120
GRID_H = 120
CELL = 8

FIRE_COLORS = {
    BURNING: (255, 80, 0),
    BURNED:  (40, 40, 40),
}


# ---------------------------------------------------------------------------
# Stage 1: Pannable/zoomable overview with Shift+drag bbox selection
# ---------------------------------------------------------------------------

class OverviewFrame(tk.Frame):
    def __init__(self, parent, on_bbox_selected):
        super().__init__(parent, bg="#1a1a2e")
        self.on_bbox_selected = on_bbox_selected
        # callback funkcija koja se zove kad korisnik oznaci mapu, definira u App(), triggera loadanje
        self._build()
        # zove funkciju koja napravi widgete aplikacije

    def _build(self):
        # Status bar
        bar = tk.Frame(self, bg="#222")
        bar.pack(fill=tk.X)
        # stretcha je cilon duzinon
        self._status = tk.Label(bar, text="Drag to pan  |  Scroll to zoom  |  Shift+drag to select simulation area",
                                bg="#222", fg="#aaa", font=("Segoe UI", 10))
        self._status.pack(side=tk.LEFT, padx=10, pady=4)
        self._coords = tk.Label(bar, text="", bg="#222", fg="#888", font=("Segoe UI", 10))
        self._coords.pack(side=tk.RIGHT, padx=10)

        # Canvas
        self.canvas = tk.Canvas(self, width=MAP_W, height=MAP_H,
                                bg="#1a1a2e", highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Motion>", self._on_mouse_move)

        # Slippy map
        self.slippy = SlippyMap(self.canvas, MAP_W, MAP_H, lat=45.8, lon=15.97, zoom=10)
        # Zagreb
        self.slippy.on_bbox = self._on_bbox
        self.slippy.on_status = lambda s: self._status.config(text=s)

    def _on_mouse_move(self, event):
        lat, lon = self.slippy._canvas_to_latlon(event.x, event.y)
        self._coords.config(text=f"{lat:.5f}, {lon:.5f}")

    def _on_bbox(self, west, south, east, north):
        self.on_bbox_selected(west, south, east, north)


# ---------------------------------------------------------------------------
# Loading screen
# ---------------------------------------------------------------------------

class LoadingFrame(tk.Frame):
    def __init__(self, parent, bbox, on_done, on_error):
        super().__init__(parent, bg="#1a1a2e", width=MAP_W, height=MAP_H + 30)
        self.pack_propagate(False)
        self.bbox = bbox
        self.on_done = on_done
        self.on_error = on_error
        self._build()
        self.after(100, self._fetch)

    def _build(self):
        w, s, e, n = self.bbox
        tk.Label(self, text="Fetching map data...", bg="#1a1a2e", fg="white",
                 font=("Segoe UI", 16, "bold")).pack(expand=True, pady=(120, 6))
        tk.Label(self, text=f"N {n:.5f}   S {s:.5f}   W {w:.5f}   E {e:.5f}",
                 bg="#1a1a2e", fg="#888", font=("Segoe UI", 10)).pack()
        self._status = tk.Label(self, text="Downloading tiles...",
                                bg="#1a1a2e", fg="#aaa", font=("Segoe UI", 10))
        self._status.pack(pady=10)

    def _fetch(self):
        w, s, e, n = self.bbox

        def run():
            try:
                data = fetch_all(w, s, e, n, GRID_W, GRID_H)
                self.after(0, lambda: self.on_done(data))
            except Exception as ex:
                msg = str(ex)
                self.after(0, lambda: self.on_error(msg))

        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Stage 2: Simulation view
# ---------------------------------------------------------------------------

class SimFrame(tk.Frame):
    def __init__(self, parent, map_data, on_back):
        super().__init__(parent)
        self.map_data = map_data
        self.on_back = on_back
        self.sim = FireSimulation(GRID_W, GRID_H)
        # Load all environmental layers into simulation
        self.sim.load_environment(
            slope         = map_data.slope,
            aspect        = map_data.aspect,
            fuel_model    = map_data.fuel_model,
            fuel_moisture = map_data.fuel_moisture_grid,
            wind_speed    = map_data.wind_speed,
            wind_dir      = map_data.wind_dir,
            cell_size_m   = map_data.cell_size_m,
        )
        self.paused = True
        self.speed_ms = 1000          # always 1 real second between ticks
        self.speed_factor = 60.0      # simulated seconds per real second (default 1 min/s)
        self._bg_photo = None
        self._fire_photo = None
        self._tick_id = None
        self._build()
        self._draw_background()
        self._tick()

    def _build(self):
        cw, ch = GRID_W * CELL, GRID_H * CELL

        self.canvas = tk.Canvas(self, width=cw, height=ch,
                                bg="#228B22", highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<B1-Motion>", self._on_click)

        # Single image item — we composite fire onto background each frame
        self._display_photo = None
        self._display_item = self.canvas.create_image(0, 0, anchor="nw")

        ctrl = tk.Frame(self, bg="#333", pady=6)
        ctrl.pack(fill=tk.X)

        tk.Button(ctrl, text="← Back", width=8, command=self._back).pack(side=tk.LEFT, padx=6)
        self.btn_pause = tk.Button(ctrl, text="Play", width=8, command=self._toggle_pause)
        self.btn_pause.pack(side=tk.LEFT, padx=2)
        tk.Button(ctrl, text="Reset", width=8, command=self._reset).pack(side=tk.LEFT, padx=2)

        # Speed multiplier: how many simulated seconds pass per real second
        tk.Label(ctrl, text="Speed:", bg="#333", fg="white").pack(side=tk.LEFT, padx=(12, 2))
        tk.Button(ctrl, text="-", width=2, command=self._slower).pack(side=tk.LEFT)
        self.speed_label = tk.Label(ctrl, text=self._speed_text(),
                                    bg="#333", fg="white", width=9)
        self.speed_label.pack(side=tk.LEFT)
        tk.Button(ctrl, text="+", width=2, command=self._faster).pack(side=tk.LEFT)

        # Elapsed simulated time display
        self.time_label = tk.Label(ctrl, text="⏱ 0s", bg="#333",
                                   fg="#88ff88", font=("Segoe UI", 10))
        self.time_label.pack(side=tk.LEFT, padx=(14, 2))

        # Weather / cell info on the right
        md = self.map_data
        w = md.weather
        info = (f"Wind: {md.wind_speed:.1f} m/s @ {md.wind_dir:.0f}°  |  "
                f"T: {w['temperature']:.0f}°C  RH: {w['humidity']:.0f}%  |  "
                f"FM: {w['fuel_moisture']*100:.1f}%  |  "
                f"Cell: {md.cell_size_m:.0f} m  |  Click to ignite")
        tk.Label(ctrl, text=info, bg="#333", fg="#aaa").pack(side=tk.RIGHT, padx=10)
    def _draw_background(self):
        cw, ch = GRID_W * CELL, GRID_H * CELL
        # background_image is already stored at canvas resolution (grid*CELL)
        self._bg_image = self.map_data.background_image.convert("RGB")
        self._display_photo = ImageTk.PhotoImage(self._bg_image)
        self.canvas.itemconfig(self._display_item, image=self._display_photo)

    def _on_click(self, event):
        gx, gy = event.x // CELL, event.y // CELL
        self.sim.ignite(gx, gy)
        self.paused = False
        self.btn_pause.config(text="Pause")
        self._redraw_fire()  # show ignition point immediately

    def _toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.config(text="Play" if self.paused else "Pause")
        if not self.paused:
            self._redraw_fire()

    def _reset(self):
        self.sim.reset()
        self.paused = True
        self.btn_pause.config(text="Play")
        self.time_label.config(text="⏱ 0s")
        # Restore plain background
        self._display_photo = ImageTk.PhotoImage(self._bg_image)
        self.canvas.itemconfig(self._display_item, image=self._display_photo)
    def _back(self):
        if self._tick_id:
            self.after_cancel(self._tick_id)
        self.on_back()

    def _slower(self):
        # Speed steps: 1s, 5s, 10s, 30s, 1m, 5m, 10m, 30m, 1h, 6h
        steps = [1, 5, 10, 30, 60, 300, 600, 1800, 3600, 21600]
        idx = next((i for i, s in enumerate(steps) if s >= self.speed_factor), len(steps)-1)
        self.speed_factor = steps[max(0, idx - 1)]
        self.speed_label.config(text=self._speed_text())

    def _faster(self):
        steps = [1, 5, 10, 30, 60, 300, 600, 1800, 3600, 21600]
        idx = next((i for i, s in enumerate(steps) if s >= self.speed_factor), 0)
        self.speed_factor = steps[min(len(steps)-1, idx + 1)]
        self.speed_label.config(text=self._speed_text())

    def _speed_text(self):
        s = self.speed_factor
        if s < 60:
            return f"×{s:.0f} (1s/s)"
        elif s < 3600:
            return f"×{s:.0f} ({s/60:.0f}m/s)"
        else:
            return f"×{s:.0f} ({s/3600:.0f}h/s)"

    def _tick(self):
        if not self.paused:
            # Advance simulation by speed_factor simulated seconds
            self.sim.step(dt_seconds=self.speed_factor)
            self._redraw_fire()
            # Update elapsed time display
            self.time_label.config(text=f"⏱ {self.sim.elapsed_str()}")
            if not self.sim.running:
                print("[tick] simulation stopped (no burning cells)")
        # Always reschedule every 1 real second
        self._tick_id = self.after(1000, self._tick)

    def _redraw_fire(self):
        import numpy as np
        grid_arr = np.array(self.sim.grid, dtype=np.uint8)  # (H, W)

        # Start from background RGB
        frame = np.array(self._bg_image, dtype=np.uint8)  # (ch, cw, 3)

        # Build small (GRID_H, GRID_W, 3) fire colour array
        fire_rgb = np.zeros((GRID_H, GRID_W, 3), dtype=np.uint8)
        fire_rgb[grid_arr == BURNING] = FIRE_COLORS[BURNING]
        fire_rgb[grid_arr == BURNED]  = FIRE_COLORS[BURNED]
        fire_mask = (grid_arr == BURNING) | (grid_arr == BURNED)  # (H, W) bool

        # Scale up fire layers to full canvas size
        fire_rgb_big  = np.repeat(np.repeat(fire_rgb,  CELL, axis=0), CELL, axis=1)
        fire_mask_big = np.repeat(np.repeat(fire_mask, CELL, axis=0), CELL, axis=1)

        # Composite: replace background pixels where fire exists
        frame[fire_mask_big] = fire_rgb_big[fire_mask_big]

        img = Image.fromarray(frame, mode="RGB")
        self._display_photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfig(self._display_item, image=self._display_photo)


# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fire Spread Simulator")
        self.resizable(False, False)
        self._frame = None
        self._show_overview()

    def _show_overview(self):
        self._switch(OverviewFrame(self, on_bbox_selected=self._on_bbox))

    def _on_bbox(self, west, south, east, north):
        self._switch(LoadingFrame(self, (west, south, east, north),
                                  on_done=self._on_data_ready,
                                  on_error=self._on_error))

    def _on_data_ready(self, map_data):
        self._switch(SimFrame(self, map_data, on_back=self._show_overview))

    def _on_error(self, msg):
        messagebox.showerror("Error", msg)
        self._show_overview()

    def _switch(self, frame):
        if self._frame:
            self._frame.destroy()
        self._frame = frame
        frame.pack(fill=tk.BOTH, expand=True)


if __name__ == "__main__":
    App().mainloop()
