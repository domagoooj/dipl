"""
Data fetching module for fire spread simulation.

Sources:
  - Elevation/slope/aspect : OpenTopography SRTMGL1 (30m, free API key)
  - NDVI / fuel model      : Sentinel Hub Sentinel-2 L2A (real satellite data)
  - Weather                : Open-Meteo (free, no key)
  - Background map         : OpenStreetMap tiles
"""

import io
import json
import math
import tempfile
import os
import urllib.request
from datetime import datetime, timedelta

import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image

from sentinelhub import (
    SHConfig, SentinelHubRequest, DataCollection,
    MimeType, BBox, CRS, bbox_to_dimensions,
)

from rothermel import DEFAULT_FUEL
try:
    from config import OPENTOPO_API_KEY, SENTINELHUB_CLIENT_ID, SENTINELHUB_CLIENT_SECRET
except ImportError:
    OPENTOPO_API_KEY = None
    SENTINELHUB_CLIENT_ID = None
    SENTINELHUB_CLIENT_SECRET = None


# ---------------------------------------------------------------------------
# Sentinel Hub config (built once)
# ---------------------------------------------------------------------------

def _sh_config():
    cfg = SHConfig()
    cfg.sh_client_id     = SENTINELHUB_CLIENT_ID
    cfg.sh_client_secret = SENTINELHUB_CLIENT_SECRET
    cfg.sh_base_url      = "https://services.sentinel-hub.com"
    return cfg


# ---------------------------------------------------------------------------
# OSM tile background
# ---------------------------------------------------------------------------

def _deg2tile(lat, lon, zoom):
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) +
             1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def _tile2deg(x, y, zoom):
    n = 2 ** zoom
    lon = x / n * 360 - 180
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    return math.degrees(lat_rad), lon


def _best_zoom(west, south, east, north, grid_w, grid_h, max_zoom=18):
    """
    Find the highest zoom level where the raw tile pixels covering the bbox
    are >= the target display size, so we always downscale (sharp) not upscale (blurry).
    Caps at max_zoom=18 to avoid fetching thousands of tiles.
    """
    for zoom in range(max_zoom, 1, -1):
        tx_min, ty_max = _deg2tile(south, west, zoom)
        tx_max, ty_min = _deg2tile(north, east, zoom)
        tile_px_w = (tx_max - tx_min + 1) * 256
        tile_px_h = (ty_max - ty_min + 1) * 256
        if tile_px_w >= grid_w and tile_px_h >= grid_h:
            # Also cap total tiles to avoid huge downloads (max ~25 tiles)
            total_tiles = (tx_max - tx_min + 1) * (ty_max - ty_min + 1)
            if total_tiles <= 25:
                return zoom
    return 13  # fallback


def fetch_osm_background(west, south, east, north, grid_w, grid_h, zoom=None):
    if zoom is None:
        zoom = _best_zoom(west, south, east, north, grid_w, grid_h)
        print(f"[osm] auto zoom={zoom}")
    tx_min, ty_max = _deg2tile(south, west, zoom)
    tx_max, ty_min = _deg2tile(north, east, zoom)
    tile_size = 256
    cols = tx_max - tx_min + 1
    rows = ty_max - ty_min + 1
    mosaic = Image.new("RGB", (cols * tile_size, rows * tile_size))
    headers = {"User-Agent": "FireSpreadSimulator/1.0"}
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                tile_img = Image.open(io.BytesIO(resp.read())).convert("RGB")
            mosaic.paste(tile_img, ((tx - tx_min) * tile_size, (ty - ty_min) * tile_size))
    lat_top, lon_left = _tile2deg(tx_min, ty_min, zoom)
    lat_bot, lon_right = _tile2deg(tx_max + 1, ty_max + 1, zoom)
    total_lon = lon_right - lon_left
    total_lat = lat_top - lat_bot
    crop_x0 = max(0, int((west  - lon_left) / total_lon * mosaic.width))
    crop_y0 = max(0, int((lat_top - north)  / total_lat * mosaic.height))
    crop_x1 = min(mosaic.width,  int((east  - lon_left) / total_lon * mosaic.width))
    crop_y1 = min(mosaic.height, int((lat_top - south)  / total_lat * mosaic.height))
    return mosaic.crop((crop_x0, crop_y0, crop_x1, crop_y1)).resize(
        (grid_w * 8, grid_h * 8), Image.LANCZOS)  # store at full canvas resolution


# ---------------------------------------------------------------------------
# OpenTopography — elevation, slope, aspect
# ---------------------------------------------------------------------------

def fetch_dem(west, south, east, north, grid_w, grid_h):
    """
    Fetch SRTMGL1 30m DEM from OpenTopography.
    Returns (elevation, slope, aspect) as np.float32 arrays (grid_h, grid_w).
    """
    if not OPENTOPO_API_KEY:
        print("[dem] No API key — returning flat terrain")
        z = np.zeros((grid_h, grid_w), dtype=np.float32)
        return z, z.copy(), z.copy()

    url = (f"https://portal.opentopography.org/API/globaldem"
           f"?demtype=SRTMGL1&south={south}&north={north}&west={west}&east={east}"
           f"&outputFormat=GTiff&API_Key={OPENTOPO_API_KEY}")
    req = urllib.request.Request(url, headers={"User-Agent": "FireSpreadSimulator/1.0"})

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(tmp_path, "wb") as f:
                f.write(resp.read())
        with rasterio.open(tmp_path) as ds:
            elevation = ds.read(1, out_shape=(grid_h, grid_w),
                                resampling=Resampling.bilinear).astype(np.float32)
        elevation[elevation < -1000] = 0.0
        # Estimate cell size in metres for slope calculation
        cell_m = estimate_cell_size(west, south, east, north, grid_w, grid_h)
        slope, aspect = _compute_slope_aspect(elevation, cell_m)
        print(f"[dem] elevation {elevation.min():.0f}–{elevation.max():.0f} m  "
              f"slope max={slope.max():.1f}°")
        return elevation, slope, aspect
    finally:
        os.unlink(tmp_path)


def _compute_slope_aspect(elevation, cell_size=30.0):
    dz_dy, dz_dx = np.gradient(elevation, cell_size)
    slope_rad = np.arctan(np.sqrt(dz_dx**2 + dz_dy**2))
    slope_deg = np.degrees(slope_rad).astype(np.float32)
    aspect_rad = np.arctan2(-dz_dy, dz_dx)
    aspect_deg = (90.0 - np.degrees(aspect_rad)) % 360.0
    return slope_deg, aspect_deg.astype(np.float32)


# ---------------------------------------------------------------------------
# Sentinel Hub — NDVI → fuel model
# ---------------------------------------------------------------------------

# NDVI thresholds → Scott & Burgan fuel model
# Based on: Chuvieco et al. (2004), Riaño et al. (2002)
_NDVI_FUEL = [
    (-1.0,  0.05, "NB1"),   # water / bare rock / snow
    ( 0.05, 0.15, "NB1"),   # bare soil / urban (very low vegetation)
    ( 0.15, 0.25, "GR1"),   # sparse dry grass
    ( 0.25, 0.35, "GR4"),   # moderate grass / cropland
    ( 0.35, 0.50, "GS1"),   # grass-shrub mix
    ( 0.50, 0.60, "SH5"),   # dense shrubland
    ( 0.60, 0.75, "TU1"),   # open woodland / timber-grass
    ( 0.75, 1.01, "TL5"),   # dense forest
]


def _ndvi_to_fuel(ndvi_arr):
    fuel = np.full(ndvi_arr.shape, DEFAULT_FUEL, dtype=object)
    for lo, hi, fid in _NDVI_FUEL:
        mask = (ndvi_arr >= lo) & (ndvi_arr < hi)
        fuel[mask] = fid
    return fuel


def fetch_ndvi_fuel(west, south, east, north, grid_w, grid_h):
    """
    Fetch NDVI from Sentinel-2 L2A via Sentinel Hub and convert to fuel models.
    Uses the least-cloudy image from the past 90 days.
    Falls back to OSM colour classification if credentials are missing.
    After NDVI classification, applies OSM colour overrides to catch
    water bodies and urban areas that NDVI misses (e.g. parks in cities).
    """
    if not SENTINELHUB_CLIENT_ID or not SENTINELHUB_CLIENT_SECRET:
        print("[ndvi] No Sentinel Hub credentials — using OSM colour fallback")
        return None  # caller will use fallback

    cfg = _sh_config()
    bbox = BBox((west, south, east, north), crs=CRS.WGS84)
    size = bbox_to_dimensions(bbox, resolution=max(10, int(
        estimate_cell_size(west, south, east, north, grid_w, grid_h))))
    # Cap size to avoid huge requests
    size = (min(size[0], grid_w * 2), min(size[1], grid_h * 2))

    # Time window: last 90 days
    end   = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

    evalscript = """
//VERSION=3
function setup() {
  return {
    input: ["B04","B08","dataMask"],
    output: { bands: 1, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(s) {
  if (s.dataMask == 0) return [-1.0];
  let ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 1e-6);
  return [ndvi];
}
"""
    try:
        request = SentinelHubRequest(
            evalscript=evalscript,
            input_data=[SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL2_L2A,
                time_interval=(start, end),
                mosaicking_order="leastCC",
            )],
            responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
            bbox=bbox,
            size=size,
            config=cfg,
        )
        result = request.get_data()
        ndvi_raw = result[0]
        # Handle both (H,W) and (H,W,1) shapes
        if ndvi_raw.ndim == 3:
            ndvi_raw = ndvi_raw[:, :, 0]
        ndvi_raw = ndvi_raw.astype(np.float32)

        # Resample to grid size
        ndvi_img = Image.fromarray(
            np.clip((ndvi_raw + 1) / 2 * 255, 0, 255).astype(np.uint8))
        ndvi_img = ndvi_img.resize((grid_w, grid_h), Image.LANCZOS)
        ndvi = np.array(ndvi_img, dtype=np.float32) / 255.0 * 2 - 1

        fuel = _ndvi_to_fuel(ndvi)
        unique, counts = np.unique(fuel, return_counts=True)
        dist = "  ".join(f"{u}={c}" for u, c in zip(unique, counts))
        print(f"[ndvi] NDVI {ndvi.min():.2f}–{ndvi.max():.2f}  fuel: {dist}")
        return fuel

    except Exception as e:
        print(f"[ndvi] Sentinel Hub failed: {e}")
        return None


# ---------------------------------------------------------------------------
# OSM colour fallback for fuel model
# ---------------------------------------------------------------------------

def classify_fuel_from_image(bg_image, grid_w, grid_h):
    """Fallback: classify fuel from OSM tile colours when Sentinel Hub unavailable."""
    # Resize to grid dimensions for classification (not canvas size)
    img = bg_image.resize((grid_w, grid_h), Image.LANCZOS).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    R, G, B = arr[:,:,0], arr[:,:,1], arr[:,:,2]
    fuel = np.full((grid_h, grid_w), "GR4", dtype=object)
    water = (B > R + 15) & (B > G + 8) & (B > 120)
    fuel[water] = "NB1"
    urban = (~water) & (R > 185) & (G > 185) & (B > 165) & ((G - R) < 15)
    fuel[urban] = "NB1"
    veg = (~water) & (~urban) & (G > R + 8) & (G > B + 8)
    fuel[veg & (G < 195)] = "TL5"
    fuel[veg & (G >= 195) & (G < 215)] = "SH5"
    print(f"[landcover] OSM fallback — water={water.sum()}  urban={urban.sum()}  "
          f"forest={np.sum(veg & (G < 195))}  shrub={np.sum(veg & (G >= 195) & (G < 215))}  "
          f"grass={np.sum(fuel == 'GR4')}")
    return fuel


# ---------------------------------------------------------------------------
# Open-Meteo weather
# ---------------------------------------------------------------------------

def fetch_weather(lat, lon):
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&current=wind_speed_10m,wind_direction_10m,temperature_2m,relative_humidity_2m"
           f"&wind_speed_unit=ms")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        c = data["current"]
        wind_speed  = float(c["wind_speed_10m"])
        wind_dir    = float(c["wind_direction_10m"])
        temperature = float(c["temperature_2m"])
        humidity    = float(c["relative_humidity_2m"])
        fm = _estimate_fuel_moisture(temperature, humidity)
        print(f"[weather] wind={wind_speed:.1f} m/s @ {wind_dir:.0f}°  "
              f"T={temperature:.1f}°C  RH={humidity:.0f}%  FM={fm*100:.1f}%")
        return dict(wind_speed=wind_speed, wind_dir=wind_dir,
                    temperature=temperature, humidity=humidity, fuel_moisture=fm)
    except Exception as e:
        print(f"[weather] failed: {e}")
        return dict(wind_speed=0.0, wind_dir=0.0, temperature=20.0,
                    humidity=40.0, fuel_moisture=0.08)


def _estimate_fuel_moisture(temp_c, rh_pct):
    rh = rh_pct / 100.0
    if rh_pct < 10:
        emc = 0.03229 + 0.281073 * rh - 0.000578 * temp_c * rh
    elif rh_pct < 50:
        emc = 2.22749 + 0.160107 * rh - 0.014784 * temp_c
    else:
        emc = 21.0606 + 0.005565 * rh**2 - 0.00035 * rh * temp_c - 0.483199 * rh
    return float(np.clip(emc / 100.0, 0.02, 0.25))


# ---------------------------------------------------------------------------
# Cell size
# ---------------------------------------------------------------------------

def estimate_cell_size(west, south, east, north, grid_w, grid_h):
    lat_mid = (south + north) / 2.0
    m_per_deg_lon = 111320 * math.cos(math.radians(lat_mid))
    m_per_deg_lat = 110540
    cw = (east - west)  / grid_w * m_per_deg_lon
    ch = (north - south) / grid_h * m_per_deg_lat
    return (cw + ch) / 2.0


# ---------------------------------------------------------------------------
# MapData container
# ---------------------------------------------------------------------------

class MapData:
    def __init__(self, elevation, slope, aspect, fuel_model, fuel_moisture_grid,
                 background_image, weather, west, south, east, north, grid_w, grid_h):
        self.elevation          = elevation
        self.slope              = slope
        self.aspect             = aspect
        self.fuel_model         = fuel_model
        self.fuel_moisture_grid = fuel_moisture_grid
        self.background_image   = background_image
        self.weather            = weather
        self.wind_speed         = weather["wind_speed"]
        self.wind_dir           = weather["wind_dir"]
        self.west  = west;  self.south = south
        self.east  = east;  self.north = north
        self.grid_w = grid_w;  self.grid_h = grid_h
        self.cell_size_m = estimate_cell_size(west, south, east, north, grid_w, grid_h)

    def pixel_to_latlon(self, px, py):
        lon = self.west  + (px / self.grid_w) * (self.east  - self.west)
        lat = self.north - (py / self.grid_h) * (self.north - self.south)
        return lat, lon

    def latlon_to_pixel(self, lat, lon):
        px = int((lon - self.west)  / (self.east  - self.west)  * self.grid_w)
        py = int((self.north - lat) / (self.north - self.south) * self.grid_h)
        return px, py


# ---------------------------------------------------------------------------
# OSM colour override — mark water and urban as NB1 regardless of NDVI
# ---------------------------------------------------------------------------

def _apply_osm_nonburnable_override(bg_image, fuel_model, grid_w, grid_h):
    """
    Use OSM tile colours to detect water and urban/road pixels and force them
    to NB1 (non-burnable), overriding whatever NDVI assigned.

    OSM Carto colour signatures:
      Water:  blue-dominant (B > R+15, B > G+8, B > 120)
      Urban/roads: grey/beige — all channels high, green only slightly above red
      Roads (pink/salmon): R dominant, moderate G and B
    """
    # Resize to grid dimensions for pixel-level classification
    img = bg_image.resize((grid_w, grid_h), Image.LANCZOS).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    R, G, B = arr[:,:,0], arr[:,:,1], arr[:,:,2]

    # Water: blue clearly dominant
    water = (B > R + 15) & (B > G + 8) & (B > 120)

    # Urban/built: grey/asphalt — all channels high and similar (low saturation)
    # Exclude beige/limestone (high R, lower B) and vegetation
    urban = (~water) & (R > 175) & (G > 175) & (B > 155) & ((G - R) < 15) & ((R - B) < 25)

    # Roads (OSM renders major roads as salmon/pink/orange):
    # R is highest, G moderate, B lower
    roads = (~water) & (~urban) & (R > 180) & (R > G + 20) & (R > B + 30) & (B < 180)

    nb_mask = water | urban | roads

    result = fuel_model.copy()
    result[nb_mask] = "NB1"

    nb_count = nb_mask.sum()
    print(f"[osm-override] forced NB1 on {nb_count} cells "
          f"(water={water.sum()}, urban={urban.sum()}, roads={roads.sum()})")
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_all(west, south, east, north, grid_w, grid_h, zoom=None):
    """
    Fetch all data for the bbox. Uses real APIs for all layers.
    """
    print("[fetch] OSM background...")
    bg = fetch_osm_background(west, south, east, north, grid_w, grid_h, zoom)

    center_lat = (south + north) / 2
    center_lon = (west  + east)  / 2

    print("[fetch] Weather (Open-Meteo)...")
    weather = fetch_weather(center_lat, center_lon)

    print("[fetch] Elevation (OpenTopography)...")
    elevation, slope, aspect = fetch_dem(west, south, east, north, grid_w, grid_h)

    print("[fetch] NDVI / fuel model (Sentinel Hub)...")
    fuel_model = fetch_ndvi_fuel(west, south, east, north, grid_w, grid_h)
    if fuel_model is None:
        print("[fetch] Falling back to OSM colour classification...")
        fuel_model = classify_fuel_from_image(bg, grid_w, grid_h)
    else:
        # Override with OSM colour detection for water and urban areas.
        # NDVI alone misclassifies urban parks and rivers as burnable vegetation.
        print("[fetch] Applying OSM colour overrides for water/urban...")
        fuel_model = _apply_osm_nonburnable_override(bg, fuel_model, grid_w, grid_h)

    fuel_moisture_grid = np.full((grid_h, grid_w),
                                 weather["fuel_moisture"], dtype=np.float32)

    return MapData(elevation, slope, aspect, fuel_model, fuel_moisture_grid,
                   bg, weather, west, south, east, north, grid_w, grid_h)
