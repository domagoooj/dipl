"""
Slippy map widget for tkinter.

- Left drag        : pan
- Scroll wheel     : zoom (keeps point under cursor fixed)
- Tap (no drag)    : reported through on_click(lat, lon)

The map fills its canvas and supports live resizing via resize(). A separate
overlay (the fire simulation) is drawn on top of the same canvas and kept in
sync through the on_redraw hook, which fires after every tile redraw.
"""

import io
import math
import threading
import urllib.request
from PIL import Image, ImageTk

TILE_SIZE = 256
USER_AGENT = "FireSpreadSimulator/1.0"


def deg2tile_f(lat, lon, zoom):
    """Return fractional tile coordinates."""
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    lat_r = math.radians(lat)
    y = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
    return x, y


def tile2deg(tx, ty, zoom):
    """Return (lat, lon) for the top-left corner of tile (tx, ty)."""
    n = 2 ** zoom
    lon = tx / n * 360 - 180
    lat_r = math.atan(math.sinh(math.pi * (1 - 2 * ty / n)))
    return math.degrees(lat_r), lon


class TileCache:
    """Thread-safe LRU-ish tile cache."""

    def __init__(self, max_size=256):
        self._cache = {}
        self._max = max_size

    def get(self, key):
        return self._cache.get(key)

    def put(self, key, img):
        if len(self._cache) >= self._max:
            # Drop oldest quarter
            drop = list(self._cache.keys())[:self._max // 4]
            for k in drop:
                del self._cache[k]
        self._cache[key] = img


class SlippyMap:
    """
    Embeds a pannable/zoomable OSM map into a tkinter Canvas.

    Usage:
        sm = SlippyMap(canvas, width, height, lat=45.8, lon=15.97, zoom=11)
        sm.on_click  = lambda lat, lon: ...   # tap without dragging
        sm.on_redraw = lambda: ...            # after every tile redraw
    """

    def __init__(self, canvas, width, height, lat=45.8, lon=15.97, zoom=11):
        self.canvas = canvas
        self.width = width
        self.height = height
        self.zoom = zoom
        self.on_click = None         # callback(lat, lon) on a tap (no drag)
        self.on_status = None        # callback(str)
        self.on_redraw = None        # callback() after each tile redraw

        self._cache = TileCache()
        self._pending = set()        # tiles currently being fetched
        self._lock = threading.Lock()

        # Centre of view in fractional tile coords
        cx, cy = deg2tile_f(lat, lon, zoom)
        self._cx = cx  # tile-space centre x
        self._cy = cy  # tile-space centre y

        # Pan / tap state
        self._pan_start = None
        self._moved = False

        # Tile image items on canvas (key -> canvas item id)
        self._tile_items = {}

        self._bind_events()
        self.redraw()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def center_latlon(self):
        return tile2deg(self._cx, self._cy, self.zoom)

    def redraw(self):
        self._draw_tiles()

    def resize(self, width, height):
        """Update the widget size (called on canvas <Configure>)."""
        if width <= 1 or height <= 1:
            return
        if width == self.width and height == self.height:
            return
        self.width = width
        self.height = height
        self._draw_tiles()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _canvas_to_tile(self, cx_px, cy_px):
        """Canvas pixel -> fractional tile coord."""
        tx = self._cx + (cx_px - self.width / 2) / TILE_SIZE
        ty = self._cy + (cy_px - self.height / 2) / TILE_SIZE
        return tx, ty

    def _tile_to_canvas(self, tx, ty):
        """Fractional tile coord -> canvas pixel."""
        cx_px = (tx - self._cx) * TILE_SIZE + self.width / 2
        cy_px = (ty - self._cy) * TILE_SIZE + self.height / 2
        return cx_px, cy_px

    def canvas_to_latlon(self, cx_px, cy_px):
        tx, ty = self._canvas_to_tile(cx_px, cy_px)
        return tile2deg(tx, ty, self.zoom)

    def latlon_to_canvas(self, lat, lon):
        """Geographic coordinate -> canvas pixel (inverse of canvas_to_latlon)."""
        tx, ty = deg2tile_f(lat, lon, self.zoom)
        return self._tile_to_canvas(tx, ty)

    # ------------------------------------------------------------------
    # Tile drawing
    # ------------------------------------------------------------------

    def _draw_tiles(self):
        # Tile range visible on canvas
        tx_min = int(self._cx - (self.width / 2) / TILE_SIZE) - 1
        ty_min = int(self._cy - (self.height / 2) / TILE_SIZE) - 1
        tx_max = int(self._cx + (self.width / 2) / TILE_SIZE) + 1
        ty_max = int(self._cy + (self.height / 2) / TILE_SIZE) + 1

        n_tiles = 2 ** self.zoom

        for tx in range(tx_min, tx_max + 1):
            for ty in range(ty_min, ty_max + 1):
                if ty < 0 or ty >= n_tiles:
                    continue
                wtx = tx % n_tiles  # wrap longitude
                key = (self.zoom, wtx, ty)

                img = self._cache.get(key)
                if img is None:
                    self._fetch_tile(key)
                    continue

                # Position on canvas
                px, py = self._tile_to_canvas(tx, ty)
                item_key = (tx, ty, self.zoom)
                if item_key in self._tile_items:
                    self.canvas.coords(self._tile_items[item_key], px, py)
                else:
                    photo = ImageTk.PhotoImage(img)
                    item = self.canvas.create_image(px, py, anchor="nw", image=photo)
                    # Keep reference so GC doesn't collect it
                    self.canvas._tile_photos = getattr(self.canvas, '_tile_photos', {})
                    self.canvas._tile_photos[item_key] = photo
                    self._tile_items[item_key] = item

        # Remove tiles from other zoom levels
        stale = [k for k in self._tile_items if k[2] != self.zoom]
        for k in stale:
            self.canvas.delete(self._tile_items.pop(k))

        # Let the overlay (fire) reposition itself on top of the fresh tiles
        if self.on_redraw:
            self.on_redraw()

    def _fetch_tile(self, key):
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)

        zoom, tx, ty = key

        def run():
            url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    img = Image.open(io.BytesIO(resp.read())).convert("RGB")
                self._cache.put(key, img)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._pending.discard(key)
            self.canvas.after(0, self._draw_tiles)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # Event binding
    # ------------------------------------------------------------------

    def _bind_events(self):
        c = self.canvas
        c.bind("<ButtonPress-1>",   self._on_press)
        c.bind("<B1-Motion>",       self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<MouseWheel>",      self._on_scroll)       # Windows
        c.bind("<Button-4>",        self._on_scroll)       # Linux scroll up
        c.bind("<Button-5>",        self._on_scroll)       # Linux scroll down

    def _on_press(self, event):
        self._pan_start = (event.x, event.y, self._cx, self._cy)
        self._moved = False

    def _on_drag(self, event):
        if not self._pan_start:
            return
        sx, sy, ocx, ocy = self._pan_start
        if abs(event.x - sx) > 3 or abs(event.y - sy) > 3:
            self._moved = True
        dx = (event.x - sx) / TILE_SIZE
        dy = (event.y - sy) / TILE_SIZE
        self._cx = ocx - dx
        self._cy = ocy - dy
        self._draw_tiles()

    def _on_release(self, event):
        was_pan = self._pan_start is not None
        moved = self._moved
        self._pan_start = None
        self._moved = False
        # A tap (press + release without meaningful movement) is a click.
        if was_pan and not moved and self.on_click:
            lat, lon = self.canvas_to_latlon(event.x, event.y)
            self.on_click(lat, lon)

    def _on_scroll(self, event):
        # Determine zoom direction
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            delta = 1
        else:
            delta = -1

        new_zoom = max(2, min(18, self.zoom + delta))
        if new_zoom == self.zoom:
            return

        # Keep the point under the cursor fixed
        mx, my = event.x, event.y
        lat, lon = self.canvas_to_latlon(mx, my)

        self.zoom = new_zoom
        # Clear old tile items (zoom changed)
        for item in self._tile_items.values():
            self.canvas.delete(item)
        self._tile_items.clear()
        if hasattr(self.canvas, '_tile_photos'):
            self.canvas._tile_photos.clear()

        # Recentre so the cursor point stays under the mouse
        cx_new, cy_new = deg2tile_f(lat, lon, new_zoom)
        self._cx = cx_new + (self.width / 2 - mx) / TILE_SIZE
        self._cy = cy_new + (self.height / 2 - my) / TILE_SIZE

        if self.on_status:
            self.on_status(f"Zoom {self.zoom}")

        self._draw_tiles()
