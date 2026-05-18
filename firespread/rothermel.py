"""
Rothermel (1972) fire spread model.

Computes rate of spread (m/min) for a single cell given:
  - fuel model parameters
  - slope (degrees)
  - aspect (degrees, 0=N clockwise)
  - wind speed (m/s at 10m)
  - wind direction (degrees, 0=N clockwise, direction wind is coming FROM)
  - fuel moisture (fraction, e.g. 0.08 = 8%)

Reference:
  Rothermel, R.C. (1972). A mathematical model for predicting fire spread
  in wildland fuels. USDA Forest Service Research Paper INT-115.

Units used internally: US customary (ft, lb, BTU) as per original paper,
converted to metric at input/output boundaries.
"""

import math

# ---------------------------------------------------------------------------
# Scott & Burgan (2005) simplified fuel models
# Keys map to (w0, delta, sigma, h, Mx, rho_p)
#   w0    : oven-dry fuel load (lb/ft²)
#   delta : fuel bed depth (ft)
#   sigma : surface-area-to-volume ratio (ft²/ft³ = 1/ft)
#   h     : heat content (BTU/lb)
#   Mx    : moisture of extinction (fraction)
#   rho_p : particle density (lb/ft³)
# ---------------------------------------------------------------------------

FUEL_MODELS = {
    # id: (w0,   delta, sigma, h,     Mx,   rho_p)
    "GR1": (0.10,  0.4,  2200, 8000, 0.25, 32),  # Short sparse dry climate grass
    "GR2": (0.20,  1.0,  2000, 8000, 0.25, 32),  # Low load dry climate grass
    "GR4": (0.87,  2.0,  1500, 8000, 0.25, 32),  # Moderate load dry climate grass
    "GS1": (0.20,  0.9,  1800, 8000, 0.30, 32),  # Low load, dry climate grass-shrub
    "SH1": (0.25,  1.0,  1600, 8000, 0.25, 32),  # Low load dry climate shrub
    "SH5": (1.00,  6.0,  750,  8000, 0.30, 32),  # High load dry climate shrub
    "TU1": (0.20,  0.6,  1600, 8000, 0.30, 32),  # Low load, dry climate timber-grass
    "TL1": (0.30,  0.2,  2000, 8000, 0.35, 32),  # Low load compact conifer litter
    "TL5": (1.15,  0.3,  1500, 8000, 0.35, 32),  # High load conifer litter
    "NB1": (0.00,  0.1,  1000, 8000, 0.99, 32),  # Non-burnable (urban/water/rock)
}

"""
# Default fuel model per land cover class (simplified ESA WorldCover mapping)
# WorldCover classes: 10=tree, 20=shrub, 30=grass, 40=cropland, 50=built,
#                     60=bare, 70=snow, 80=water, 90=wetland, 95=mangrove
LANDCOVER_TO_FUEL = {
    10: "TL5",   # dense tree cover → heavy conifer litter
    20: "SH5",   # shrubland → high load shrub
    30: "GR4",   # grassland → moderate grass
    40: "GR2",   # cropland → low load grass
    50: "NB1",   # built-up → non-burnable
    60: "NB1",   # bare → non-burnable
    70: "NB1",   # snow/ice → non-burnable
    80: "NB1",   # water → non-burnable
    90: "GS1",   # wetland → grass-shrub
    95: "GS1",   # mangrove → grass-shrub
}
"""
DEFAULT_FUEL = "GR2"


# ---------------------------------------------------------------------------
# Core Rothermel rate-of-spread calculation
# ---------------------------------------------------------------------------

def _rothermel_ros(fuel_id, slope_deg, wind_speed_ms, wind_dir_deg,
                   spread_dir_deg, fuel_moisture):
    """
    Compute rate of spread (m/min) in direction spread_dir_deg.

    Parameters
    ----------
    fuel_id        : str, key in FUEL_MODELS
    slope_deg      : float, slope steepness in degrees
    wind_speed_ms  : float, wind speed m/s at 10m height
    wind_dir_deg   : float, direction wind comes FROM (0=N, 90=E)
    spread_dir_deg : float, direction of potential spread (0=N, 90=E)
    fuel_moisture  : float, dead fine fuel moisture fraction (e.g. 0.08)
    """
    params = FUEL_MODELS.get(fuel_id, FUEL_MODELS[DEFAULT_FUEL])
    w0, delta, sigma, h, Mx, rho_p = params

    # Non-burnable
    if fuel_id == "NB1" or w0 == 0:
        return 0.0

    # Moisture damping — fire dies if moisture >= extinction moisture
    if fuel_moisture >= Mx:
        return 0.0

    # Convert wind to ft/min (1 m/s = 196.85 ft/min)
    U = wind_speed_ms * 196.85

    # Packing ratio
    beta = w0 / (delta * rho_p)
    beta_op = 3.348 * sigma**(-0.8189)          # optimum packing ratio
    beta_ratio = beta / beta_op

    # Reaction intensity (BTU/ft²/min)
    A = 133.0 * sigma**(-0.7913)
    gamma_max = sigma**1.5 / (495 + 0.0594 * sigma**1.5)
    gamma = gamma_max * (beta_ratio**A) * math.exp(A * (1 - beta_ratio))

    # Moisture damping coefficient
    rm = fuel_moisture / Mx
    eta_M = 1 - 2.59 * rm + 5.11 * rm**2 - 3.52 * rm**3
    eta_M = max(0.0, eta_M)

    # Mineral damping (assume standard mineral content 0.0555)
    eta_s = 0.174 * 0.0555**(-0.19)
    eta_s = min(1.0, eta_s)

    IR = gamma * w0 * h * eta_M * eta_s

    # Propagating flux ratio
    xi = math.exp((0.792 + 0.681 * sigma**0.5) * (beta + 0.1)) / (192 + 0.2595 * sigma)

    # Wind factor — angle between wind and spread direction
    wind_from_rad = math.radians(wind_dir_deg)
    spread_rad    = math.radians(spread_dir_deg)
    # Wind blows FROM wind_dir_deg, so it pushes fire in opposite direction
    wind_to_rad   = wind_from_rad + math.pi
    angle_diff    = spread_rad - wind_to_rad
    wind_component = max(0.0, math.cos(angle_diff))  # 1 if aligned, 0 if perpendicular

    C = 7.47 * math.exp(-0.133 * sigma**0.55)
    B = 0.02526 * sigma**0.54
    E = 0.715 * math.exp(-3.59e-4 * sigma)
    phi_w = C * (U * wind_component)**B * beta_ratio**(-E)

    # Slope factor — upslope component in spread direction
    # Aspect: direction the slope faces (downhill direction)
    # We pass slope_deg as effective slope in the spread direction
    slope_rad = math.radians(max(0.0, slope_deg))
    tan_phi = math.tan(slope_rad)
    phi_s = 5.275 * beta**(-0.3) * tan_phi**2

    # Effective heating number
    epsilon = math.exp(-138.0 / sigma)

    # Heat of preignition
    Q_ig = 250 + 1116 * fuel_moisture

    # Rate of spread (ft/min)
    rho_b = w0 / delta  # bulk density
    ros_ft = (IR * xi * (1 + phi_w + phi_s)) / (rho_b * epsilon * Q_ig)
    ros_ft = max(0.0, ros_ft)

    # Convert ft/min → m/min
    return ros_ft * 0.3048


def rate_of_spread(fuel_id, slope_deg, aspect_deg, wind_speed_ms, wind_dir_deg,
                   spread_dir_deg, fuel_moisture):
    """
    Public interface. Returns ROS in m/min for spreading from a cell
    in direction spread_dir_deg.

    aspect_deg: direction the slope faces downhill (0=N, 90=E).
    The effective slope seen in spread_dir_deg is slope * cos(spread - aspect).
    """
    # Project slope onto spread direction
    aspect_rad      = math.radians(aspect_deg)
    spread_rad      = math.radians(spread_dir_deg)
    slope_component = math.cos(spread_rad - aspect_rad)
    effective_slope = slope_deg * max(0.0, slope_component)

    return _rothermel_ros(fuel_id, effective_slope, wind_speed_ms,
                          wind_dir_deg, spread_dir_deg, fuel_moisture)


# ---------------------------------------------------------------------------
# ROS → spread probability for CA timestep
# ---------------------------------------------------------------------------

# Neighbour directions (dy, dx) → compass bearing
NEIGHBOR_DIRS = {
    (-1,  0): 0.0,    # N
    (-1,  1): 45.0,   # NE
    ( 0,  1): 90.0,   # E
    ( 1,  1): 135.0,  # SE
    ( 1,  0): 180.0,  # S
    ( 1, -1): 225.0,  # SW
    ( 0, -1): 270.0,  # W
    (-1, -1): 315.0,  # NW
}

# Max ROS we expect (m/min) — used to normalise to probability
MAX_ROS = 30.0


def ros_to_prob(ros_m_min, cell_size_m, timestep_min):
    """
    Convert rate of spread (m/min) to a per-timestep ignition probability.
    P = min(1, ros * timestep / cell_size)
    """
    return min(1.0, ros_m_min * timestep_min / cell_size_m)
