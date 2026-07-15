"""
Standalone fire spread simulation demo.

Uses FireSimulation and rate_of_spread exactly as the original files define them.
No GUI — prints a text grid to the terminal each tick and a summary at the end.

Run:
    python run__simulation.py

Requires:
    rothermel.py and simulation.py in the same directory.
    numpy (pip install numpy)
"""

import numpy as np
import time

# ---------------------------------------------------------------------------
# Import exactly what simulation.py and rothermel.py expose
# ---------------------------------------------------------------------------
from simulation import FireSimulation, EMPTY, BURNING, BURNED
from rothermel import FUEL_MODELS, NEIGHBOR_DIRS, rate_of_spread

# DEFAULT_FUEL lives inside a docstring in rothermel.py so it is not importable.
# We define it here directly, matching the value used throughout the project.
DEFAULT_FUEL = "GR2"

# ---------------------------------------------------------------------------
# Grid size — kept small so the ASCII grid fits in a terminal window.
# (The GUI app sizes its grid dynamically from the map viewport; this demo
#  simply uses a fixed small grid.)
# ---------------------------------------------------------------------------
GRID_W = 40    # cells across (small enough for terminal output)
GRID_H = 30    # cells down

# ---------------------------------------------------------------------------
# Simulation scenario — tweak these to experiment
# ---------------------------------------------------------------------------

# Wind: 5 m/s blowing FROM the south (pushes fire northward)
WIND_SPEED_MS = 5.0      # self.wind_speed in FireSimulation
WIND_DIR_DEG  = 180.0    # self.wind_dir  in FireSimulation — FROM south

# Terrain: 15° slope facing north (fire climbing uphill when spreading north)
SLOPE_DEG  = 15.0   # loaded into self.slope  via load_environment()
ASPECT_DEG = 0.0    # loaded into self.aspect via load_environment() — slope faces N

# Fuel moisture: 8% everywhere (default in FireSimulation.__init__)
FUEL_MOISTURE = 0.08   # self.fuel_moisture default value

# Cell size in metres — same default as FireSimulation.__init__
CELL_SIZE_M = 100.0    # self.cell_size_m

# Speed: each step advances 60 simulated seconds (the same 60 s step the GUI uses)
DT_SECONDS = 60.0

# How many steps to run
MAX_STEPS = 30

# Ignition point — centre of grid
IGNITE_X = GRID_W // 2   # passed to sim.ignite(x, y)
IGNITE_Y = GRID_H // 2

# ---------------------------------------------------------------------------
# Build environment arrays — same dtypes as load_environment() expects
# ---------------------------------------------------------------------------

# slope and aspect: np.float32 arrays of shape (GRID_H, GRID_W)
# uniform terrain for clarity; real app fills these from fetch_dem()
slope_arr  = np.full((GRID_H, GRID_W), SLOPE_DEG,  dtype=np.float32)
aspect_arr = np.full((GRID_H, GRID_W), ASPECT_DEG, dtype=np.float32)

# fuel_model: object array of fuel id strings — same dtype as FireSimulation uses
fuel_model_arr = np.full((GRID_H, GRID_W), DEFAULT_FUEL, dtype=object)

# Put a strip of non-burnable cells (road / firebreak) across the middle column
# to demonstrate NB1 blocking spread — same "NB1" string used in FUEL_MODELS
FIREBREAK_COL = GRID_W // 2 + 8
fuel_model_arr[:, FIREBREAK_COL] = "NB1"

# fuel_moisture: np.float32 array — same default as FireSimulation.__init__
fuel_moisture_arr = np.full((GRID_H, GRID_W), FUEL_MOISTURE, dtype=np.float32)

# ---------------------------------------------------------------------------
# Print a quick scenario summary before starting
# ---------------------------------------------------------------------------

def compass(deg):
    """Convert degrees to nearest compass label."""
    labels = ["N","NE","E","SE","S","SW","W","NW"]
    return labels[int((deg + 22.5) / 45) % 8]

print("=" * 60)
print("  Rothermel CA Fire Spread — standalone demo")
print("=" * 60)
print(f"  Grid         : {GRID_W} × {GRID_H} cells  ({CELL_SIZE_M:.0f} m/cell)")
print(f"  Wind         : {WIND_SPEED_MS} m/s FROM {compass(WIND_DIR_DEG)} ({WIND_DIR_DEG:.0f}°)")
print(f"  Slope        : {SLOPE_DEG}°  facing {compass(ASPECT_DEG)} (aspect={ASPECT_DEG:.0f}°)")
print(f"  Fuel model   : {DEFAULT_FUEL}  ({FUEL_MODELS[DEFAULT_FUEL]})")
print(f"  Fuel moisture: {FUEL_MOISTURE*100:.0f}%")
print(f"  Firebreak    : column {FIREBREAK_COL} (NB1)")
print(f"  Ignition     : ({IGNITE_X}, {IGNITE_Y})")
print(f"  Timestep     : {DT_SECONDS:.0f} s per step")
print("=" * 60)

# Show ROS in each of the 8 neighbour directions from the ignition cell
# Uses rate_of_spread() exactly as simulation.py calls it in step()
print("\nRate of spread from ignition cell (m/min):")
for (dy, dx), bearing in NEIGHBOR_DIRS.items():
    ros = rate_of_spread(
        fuel_id        = DEFAULT_FUEL,
        slope_deg      = SLOPE_DEG,
        aspect_deg     = ASPECT_DEG,
        wind_speed_ms  = WIND_SPEED_MS,
        wind_dir_deg   = WIND_DIR_DEG,
        spread_dir_deg = bearing,
        fuel_moisture  = FUEL_MOISTURE,
    )
    print(f"  {compass(bearing):>2} ({bearing:>5.1f}°): {ros:.3f} m/min")

print()

# ---------------------------------------------------------------------------
# Initialise simulation — same setup the GUI performs in App._region_ready()
# ---------------------------------------------------------------------------
sim = FireSimulation(GRID_W, GRID_H)

# load_environment() signature matches FireSimulation exactly
sim.load_environment(
    slope         = slope_arr,
    aspect        = aspect_arr,
    fuel_model    = fuel_model_arr,
    fuel_moisture = fuel_moisture_arr,
    wind_speed    = WIND_SPEED_MS,
    wind_dir      = WIND_DIR_DEG,
    cell_size_m   = CELL_SIZE_M,
)

# ignite() — same call the GUI makes from App._ignite_at()
sim.ignite(IGNITE_X, IGNITE_Y)

# ---------------------------------------------------------------------------
# ASCII display helpers
# ---------------------------------------------------------------------------

# Cell characters — one per simulation state (EMPTY / BURNING / BURNED)
CELL_CHAR = {
    EMPTY:   ".",    # unburned
    BURNING: "#",    # actively burning
    BURNED:  "x",    # ash
}

def print_grid(sim, step):
    """Print the current grid state with a border."""
    arr = sim.grid
    burning_count = sum(arr[y][x] == BURNING for y in range(sim.height) for x in range(sim.width))
    burned_count  = sum(arr[y][x] == BURNED  for y in range(sim.height) for x in range(sim.width))

    print(f"\nStep {step:>3}  |  {sim.elapsed_str():>12}  |  "
          f"burning={burning_count:>4}  burned={burned_count:>4}")
    print("+" + "-" * sim.width + "+")
    for y in range(sim.height):
        row = "".join(CELL_CHAR[sim.grid[y][x]] for x in range(sim.width))
        print("|" + row + "|")
    print("+" + "-" * sim.width + "+")

# ---------------------------------------------------------------------------
# Main simulation loop — mirrors the GUI's animation loop (App._tick())
# ---------------------------------------------------------------------------

print_grid(sim, step=0)

for step in range(1, MAX_STEPS + 1):
    # sim.step(dt_seconds) — the same call the GUI's _tick() makes each step
    sim.step(dt_seconds=DT_SECONDS)

    print_grid(sim, step)

    # sim.running goes False when no BURNING cells remain — same check as _tick()
    if not sim.running:
        print(f"\nFire extinguished after {step} steps.")
        break

    time.sleep(0.05)   # small delay so output is readable if piped

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
total_cells   = GRID_W * GRID_H
burned_cells  = sum(sim.grid[y][x] == BURNED  for y in range(GRID_H) for x in range(GRID_W))
burning_cells = sum(sim.grid[y][x] == BURNING for y in range(GRID_H) for x in range(GRID_W))

print("\n" + "=" * 60)
print("  Final summary")
print("=" * 60)
print(f"  Simulated time : {sim.elapsed_str()}")
print(f"  Total cells    : {total_cells}")
print(f"  Burned (ash)   : {burned_cells}  ({burned_cells/total_cells*100:.1f}%)")
print(f"  Still burning  : {burning_cells}")
print(f"  Unburned       : {total_cells - burned_cells - burning_cells}")
print(f"  Area burned    : {burned_cells * CELL_SIZE_M**2 / 1e6:.3f} km²")
print("=" * 60)