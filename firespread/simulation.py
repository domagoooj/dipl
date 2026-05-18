"""
CA-Rothermel fire spread simulation.

Time model:
  - The simulation tracks elapsed simulated time in seconds.
  - Each call to step(dt_seconds) advances the simulation by dt_seconds of
    simulated time. The Rothermel ROS (m/min) is converted to a per-second
    ignition probability using the actual dt.
  - The UI calls step() once per real-world second, passing dt = speed_factor
    so that speed_factor=1 means real-time, speed_factor=60 means 1 minute
    of fire time per real second, etc.
"""

import random
import math
import numpy as np

from rothermel import (
    rate_of_spread, NEIGHBOR_DIRS,
    DEFAULT_FUEL
)

EMPTY   = 0
BURNING = 1
BURNED  = 2


def ros_to_prob_seconds(ros_m_min, cell_size_m, dt_seconds):
    """
    Convert Rothermel ROS (m/min) to ignition probability for a timestep
    of dt_seconds real (simulated) seconds.

    P = min(1, ROS_m_per_s * dt_seconds / cell_size_m)
      = min(1, ROS_m_min / 60 * dt_seconds / cell_size_m)
    """
    ros_m_per_s = ros_m_min / 60.0
    return min(1.0, ros_m_per_s * dt_seconds / cell_size_m)


class FireSimulation:
    def __init__(self, width, height):
        self.width  = width
        self.height = height

        self.grid = [[EMPTY] * width for _ in range(height)]
        self.running = False

        # Environmental layers
        self.slope         = np.zeros((height, width), dtype=np.float32)
        self.aspect        = np.zeros((height, width), dtype=np.float32)
        self.fuel_model    = np.full((height, width), DEFAULT_FUEL, dtype=object)
        self.fuel_moisture = np.full((height, width), 0.08, dtype=np.float32)

        # Wind
        self.wind_speed = 0.0   # m/s
        self.wind_dir   = 0.0   # degrees FROM

        # Geometry
        self.cell_size_m = 100.0   # metres per cell

        # Time tracking
        self.elapsed_seconds = 0.0   # total simulated seconds since ignition

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def load_environment(self, slope, aspect, fuel_model, fuel_moisture,
                         wind_speed, wind_dir, cell_size_m=100.0):
        self.slope         = slope.astype(np.float32)
        self.aspect        = aspect.astype(np.float32)
        self.fuel_model    = fuel_model
        self.fuel_moisture = fuel_moisture.astype(np.float32)
        self.wind_speed    = wind_speed
        self.wind_dir      = wind_dir
        self.cell_size_m   = cell_size_m

    def ignite(self, x, y):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.grid[y][x] = BURNING
            self.running = True

    def reset(self):
        self.grid            = [[EMPTY] * self.width for _ in range(self.height)]
        self.running         = False
        self.elapsed_seconds = 0.0

    # ------------------------------------------------------------------
    # Simulation step
    # ------------------------------------------------------------------

    def step(self, dt_seconds=1.0):
        """
        Advance the simulation by dt_seconds of simulated time.

        dt_seconds=1   → real-time (1 simulated second per call)
        dt_seconds=60  → 1 simulated minute per call (60× speed)
        dt_seconds=3600 → 1 simulated hour per call (3600× speed)
        """
        if not self.running:
            return
        # ako nema vatre, odma izlazi iz funkcije    

        self.elapsed_seconds += dt_seconds
        # total simulated time

        new_grid    = [row[:] for row in self.grid]
        # nova kopija grida, za svaki tick, pravi se novi grid
        any_burning = False
        # bit ce true kad se bilo koja zapali, koristi se da se vidi oce li simulacija stat

        for y in range(self.height):
            for x in range(self.width):
                if self.grid[y][x] != BURNING:
                    continue

        # gleda svaku celiju u gridu red po red, gleda samo burning, burned i empty se ne mogu sirit

                new_grid[y][x] = BURNED
                # trenutna celija ce u sljedecen ticku bit burned

                for (dy, dx), bearing in NEIGHBOR_DIRS.items():
                # gleda sve susjedne celije, bearing je smjer kompasa prema susjedu od originalnog
                    ny, nx = y + dy, x + dx
                    # y, x su trenutna celija koja gori, dy, dx je korak do susjeda, ny i nx su koordinate susjeda
                    if not (0 <= nx < self.width and 0 <= ny < self.height):
                        continue
                    # safety da novi nije izvan windowa, ako je, onda se vatra teleportira na drugi red
                    # za column dobijemo indexerror i crasha
                    if self.grid[ny][nx] != EMPTY:
                        continue
                    # bez ovog, burned susjed ce opet pokrenut rothermela i potencijalno opet postat burning

                    ros = rate_of_spread(
                        fuel_id        = str(self.fuel_model[ny, nx]),
                        slope_deg      = float(self.slope[ny, nx]),
                        aspect_deg     = float(self.aspect[ny, nx]),
                        wind_speed_ms  = self.wind_speed,
                        wind_dir_deg   = self.wind_dir,
                        spread_dir_deg = bearing,
                        fuel_moisture  = float(self.fuel_moisture[ny, nx]),
                    )
                    # pozivanje rothermel funkcije, koristimo susjednu celiju jer ona odlucuje oce li se vatra sirit

                    # Diagonal cells are √2 farther away
                    dist = self.cell_size_m * (math.sqrt(2) if dx != 0 and dy != 0 else 1.0)
                    prob = ros_to_prob_seconds(ros, dist, dt_seconds)
                    # izracuna se vjerojatnost oce li se prosirit ili ne

                    if random.random() < prob:
                        new_grid[ny][nx] = BURNING
                        any_burning = True
                    # random jer u realnosti imamo male random varijacije u gorivu, atmosferi itd
                    # ako je random vrijednost manja od izracunate vjerojatnosti, vatra se siri na susida

        self.grid    = new_grid
        # sljedece stanje postaje trenutno stanje
        self.running = any_burning

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @property
    def elapsed_minutes(self):
        return self.elapsed_seconds / 60.0

    @property
    def elapsed_hours(self):
        return self.elapsed_seconds / 3600.0

    def elapsed_str(self):
        """Human-readable elapsed simulated time."""
        s = int(self.elapsed_seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m:02d}m {sec:02d}s"
        elif m > 0:
            return f"{m}m {sec:02d}s"
        else:
            return f"{sec}s"
