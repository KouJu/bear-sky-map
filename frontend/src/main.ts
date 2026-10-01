import maplibregl from "maplibre-gl";

// Lorenz atlas omits pure-ocean tiles (404). The author's own viewer falls
// back to a black tile there ("ocean = dark sky"), so mirror that behavior
// and keep the console free of 404 noise.
let blackTile: Promise<ArrayBuffer> | null = null;
function getBlackTile(): Promise<ArrayBuffer> {
  blackTile ??= fetch(
    "https://djlorenz.github.io/astronomy/image_tiles/tiles2025/black.png",
  ).then((r) => r.arrayBuffer());
  return blackTile;
}

maplibregl.addProtocol("lp", async (params) => {
  const url = params.url.replace("lp://", "https://");
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status}`);
    return { data: await r.arrayBuffer() };
  } catch {
    return { data: await getBlackTile() };
  }
});

const INITIAL_VIEW = {
  center: [140.6, 38.0] as [number, number], // between Tokyo and Miyagi
  zoom: 6,
  bearing: 0,
  pitch: 0,
};

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    // font glyphs for map labels. NOTE: fonts.openmaptiles.org returns an
    // HTML page to browsers (breaks PBF parsing and silently kills every
    // layer of any source that needs glyphs) — use the maplibre demo fonts.
    // CJK characters are still generated locally (localIdeographFontFamily).
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: {
      // GSI (国土地理院) tiles: explicitly cleared for public reuse with
      // attribution, unlike tile.openstreetmap.org whose volunteer-run servers
      // ask apps not to rely on them. "pale" is their muted basemap, intended
      // for overlaying data on. Japan-only coverage, which is all this app needs.
      base: {
        type: "raster",
        tiles: ["https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"],
        tileSize: 256,
        minzoom: 2,
        maxzoom: 18,
        attribution:
          '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">地理院タイル</a>',
      },
    },
    layers: [
      {
        id: "base",
        type: "raster",
        source: "base",
        // grayscale the basemap so data layers (green safe areas, red risk
        // buffers, LP colors) stand out against it
        paint: { "raster-saturation": -1, "raster-opacity": 0.85 },
      },
    ],
  },
  ...INITIAL_VIEW,
});

// expose for debugging in the browser console
(window as any)._map = map;

// visualizePitch: clicking the compass resets tilt (pitch) as well as bearing
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");

// home view button: fly back to the initial center/zoom with no tilt/rotation
class HomeControl implements maplibregl.IControl {
  private container!: HTMLDivElement;
  onAdd(m: maplibregl.Map): HTMLElement {
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl maplibregl-ctrl-group";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.title = "最初の表示に戻る";
    btn.setAttribute("aria-label", "最初の表示に戻る");
    btn.style.fontSize = "18px";
    btn.textContent = "⌂";
    btn.addEventListener("click", () => m.flyTo({ ...INITIAL_VIEW }));
    this.container.appendChild(btn);
    return this.container;
  }
  onRemove(): void {
    this.container.remove();
  }
}
map.addControl(new HomeControl(), "top-right");

/** Diagonal stripes on a transparent background, for use as a fill-pattern.
 *
 * Kept thin and widely spaced: this layer covers exactly the places a user
 * wants to read on the basemap — the peak and valley names they would drive
 * to — so it has to mark the area without hiding what is underneath.
 */
function makeHatchImage(color: string, size = 14): ImageData {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  // draw the stripe three times so it tiles seamlessly across edges
  for (const offset of [-size, 0, size]) {
    ctx.beginPath();
    ctx.moveTo(offset, size);
    ctx.lineTo(offset + size, 0);
    ctx.stroke();
  }
  return ctx.getImageData(0, 0, size, size);
}

type FeatureCollection = {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    geometry: { type: "Point"; coordinates: [number, number] };
    properties: Record<string, unknown>;
  }>;
};

// "2026/8/12" -> epoch ms (0 when missing/unparsable, which "全て" still shows)
function addTimestamps(fc: FeatureCollection): FeatureCollection {
  for (const f of fc.features) {
    const raw = f.properties.date;
    const ts = typeof raw === "string" ? Date.parse(raw) : NaN;
    f.properties.ts = Number.isNaN(ts) ? 0 : ts;
  }
  return fc;
}

// --- Light pollution (Lorenz Light Pollution Atlas 2025) -----------------
// Binary tiles: 5°x5°, 600x600 points at 1/120° resolution. Value decoding
// ported from djlorenz.github.io/astronomy/lp/overlay/dark.html.
const LP_YEAR = 2025;
const lpTileCache = new Map<string, Promise<Int8Array | null>>();

function fetchLpBinaryTile(tilex: number, tiley: number): Promise<Int8Array | null> {
  const key = `${tilex}_${tiley}`;
  if (!lpTileCache.has(key)) {
    const url = `https://djlorenz.github.io/astronomy/binary_tiles/${LP_YEAR}/binary_tile_${tilex}_${tiley}.dat.gz`;
    lpTileCache.set(
      key,
      fetch(url)
        .then((r) => {
          if (!r.ok || !r.body) return null;
          const ds = new DecompressionStream("gzip");
          return new Response(r.body.pipeThrough(ds))
            .arrayBuffer()
            .then((buf) => new Int8Array(buf));
        })
        .catch(() => null),
    );
  }
  return lpTileCache.get(key)!;
}

const mod = (n: number, m: number) => ((n % m) + m) % m;

// artificial/natural brightness ratio at zenith
function compressed2ratio(x: number): number {
  return (5.0 / 195.0) * (Math.exp(0.0195 * x) - 1.0);
}

async function predictedSqm(lng: number, lat: number): Promise<number | null> {
  const lonFromDateLine = mod(lng + 180.0, 360.0);
  const latFromStart = lat + 65.0;
  const tilex = Math.floor(lonFromDateLine / 5.0) + 1;
  const tiley = Math.floor(latFromStart / 5.0) + 1;
  if (tiley < 1 || tiley > 28) return null; // atlas covers lat -65..+75

  const data = await fetchLpBinaryTile(tilex, tiley);
  if (!data) return null;

  const ix = Math.round(120 * (lonFromDateLine - 5.0 * (tilex - 1) + 1 / 240));
  const iy = Math.round(120 * (latFromStart - 5.0 * (tiley - 1) + 1 / 240));

  // first point is a 2-byte value; the rest are 1-byte deltas along the grid
  const first = 128 * Number(data[0]) + Number(data[1]);
  let change = 0;
  for (let i = 1; i < iy; i++) change += Number(data[600 * i + 1]);
  for (let i = 1; i < ix; i++) change += Number(data[600 * (iy - 1) + 1 + i]);

  const ratio = compressed2ratio(first + change);
  return 22.0 - 5.0 * Math.log(1.0 + ratio) / Math.log(100.0);
}

function updateCounts(tokyo: FeatureCollection, miyagi: FeatureCollection, cutoff: number) {
  const count = (fc: FeatureCollection) =>
    cutoff === 0
      ? fc.features.length
      : fc.features.filter((f) => (f.properties.ts as number) >= cutoff).length;
  document.getElementById("count-tokyo")!.textContent = `${count(tokyo)}件`;
  document.getElementById("count-miyagi")!.textContent = `${count(miyagi)}件`;
}

map.on("load", async () => {
  // BASE_URL is "/" locally and "/bear-sky-map/" on GitHub Pages, so data
  // paths must be built from it rather than hard-coded to the site root
  const base = import.meta.env.BASE_URL;
  const [tokyo, miyagi, safeArea, bearBuffer] = await Promise.all([
    fetch(`${base}data/tokyo_kuma.geojson`).then((r) => r.json() as Promise<FeatureCollection>),
    fetch(`${base}data/miyagi_kuma.geojson`).then((r) => r.json() as Promise<FeatureCollection>),
    fetch(`${base}data/safe_area.geojson`).then((r) => r.json()),
    fetch(`${base}data/bear_buffer.geojson`).then((r) => r.json()),
  ]);
  addTimestamps(tokyo);
  addTimestamps(miyagi);

  map.addSource("lp", {
    type: "raster",
    tiles: [
      `lp://djlorenz.github.io/astronomy/image_tiles/tiles${LP_YEAR}/tile_{z}_{x}_{y}.png`,
    ],
    tileSize: 1024,
    maxzoom: 6, // tiles exist up to URL z=6 only; MapLibre overzooms beyond
    attribution: "Light Pollution Atlas 2025 (David J. Lorenz)",
  });
  map.addLayer({
    id: "lp-overlay",
    type: "raster",
    source: "lp",
    paint: { "raster-opacity": 0.5 },
  });

  // A diagonal hatch distinguishes the safe area from the light-pollution
  // overlay, which also uses green bands. Relying on texture rather than hue
  // alone keeps the two readable together (and for colour-blind viewers).
  map.addImage("safe-hatch", makeHatchImage("#1a7f37"));

  // core layers: dark-sky minus bear buffers (hatched) and the risk buffers (red)
  map.addSource("safe-area", { type: "geojson", data: safeArea });
  map.addLayer({
    id: "safe-area-fill",
    type: "fill",
    source: "safe-area",
    paint: { "fill-pattern": "safe-hatch", "fill-opacity": 0.5 },
  });
  map.addLayer({
    id: "safe-area-line",
    type: "line",
    source: "safe-area",
    paint: { "line-color": "#1a7f37", "line-width": 1 },
  });

  map.addSource("bear-buffer", { type: "geojson", data: bearBuffer });
  map.addLayer({
    id: "bear-buffer-fill",
    type: "fill",
    source: "bear-buffer",
    paint: { "fill-color": "#cf222e", "fill-opacity": 0.18 },
  });
  map.addLayer({
    id: "bear-buffer-line",
    type: "line",
    source: "bear-buffer",
    paint: { "line-color": "#cf222e", "line-width": 1, "line-dasharray": [2, 2] },
  });

  map.addSource("tokyo", { type: "geojson", data: tokyo as any });
  map.addSource("miyagi", { type: "geojson", data: miyagi as any });

  // Sightings older than the risk window are shown but excluded from the
  // computation (the backend buffer uses the same window), so give them a
  // muted style and an age tag instead of the normal colours.
  // The split is inclusive/exclusive on purpose: exactly RISK_WINDOW_DAYS old
  // still counts as current, and the tags read "より前" to match.
  const DAY = 24 * 60 * 60 * 1000;
  const RISK_WINDOW_DAYS = 90;
  const now = Date.now();
  const OLD_CUTOFF = now - RISK_WINDOW_DAYS * DAY;
  const RECENT_F: any = [">=", ["get", "ts"], OLD_CUTOFF];
  const OLD_F: any = ["<", ["get", "ts"], OLD_CUTOFF];

  // Staged age tags. "step" needs its stops ascending, and older timestamps are
  // smaller, so the oldest bucket is the default and stops count upward.
  const ageTagExpression: any = [
    "step",
    ["get", "ts"],
    "3年より前",
    now - 1095 * DAY, "2年より前",
    now - 730 * DAY, "1年より前",
    now - 365 * DAY, "90日より前",
  ];

  const pointRadius: any = ["interpolate", ["linear"], ["zoom"], 5, 4, 12, 9];
  const recentPaint = (color: string): any => ({
    "circle-radius": pointRadius,
    "circle-color": color,
    "circle-opacity": 0.75,
    "circle-stroke-width": 1,
    "circle-stroke-color": "#ffffff",
  });
  const oldPaint: any = {
    "circle-radius": pointRadius,
    "circle-color": "#9a9fa5",
    "circle-opacity": 0.55,
    "circle-stroke-width": 1,
    // Sightings from the last year carry a black outline so "recent but no
    // longer counted" stands apart from genuinely old history at a glance,
    // even at zoom levels where the age tags are hidden.
    "circle-stroke-color": [
      "step",
      ["get", "ts"],
      "#ffffff",
      now - 365 * DAY, "#000000",
    ],
  };

  map.addLayer({ id: "tokyo-points-old", type: "circle", source: "tokyo", filter: OLD_F, paint: oldPaint });
  map.addLayer({ id: "miyagi-points-old", type: "circle", source: "miyagi", filter: OLD_F, paint: oldPaint });
  map.addLayer({ id: "tokyo-points", type: "circle", source: "tokyo", filter: RECENT_F, paint: recentPaint("#e63946") });
  map.addLayer({ id: "miyagi-points", type: "circle", source: "miyagi", filter: RECENT_F, paint: recentPaint("#457b9d") });

  for (const [id, source] of [["tokyo-old-label", "tokyo"], ["miyagi-old-label", "miyagi"]] as const) {
    map.addLayer({
      id,
      type: "symbol",
      source,
      filter: OLD_F,
      // Only label once zoomed into a specific area. Across a whole prefecture
      // there are hundreds of old sightings and the labels bury the map; the
      // grey colour plus the legend already carries the meaning at that scale.
      minzoom: 12.5,
      layout: {
        "text-field": ageTagExpression,
        "text-font": ["Noto Sans Regular"],
        "text-size": 10,
        "text-anchor": "bottom",
        "text-offset": [0, -0.8],
        // labels collide-hide automatically when points are dense
      },
      paint: {
        "text-color": "#57606a",
        "text-halo-color": "#ffffff",
        "text-halo-width": 1.2,
      },
    });
  }

  // single click: bear point details only
  map.on("click", (e) => {
    const hits = map.queryRenderedFeatures(e.point, {
      layers: ["tokyo-points", "miyagi-points", "tokyo-points-old", "miyagi-points-old"],
    });
    if (hits.length === 0) return;
    const f = hits[0];
    const props = f.properties ?? {};
    const html = Object.entries(props)
      .filter(([k]) => k !== "ts")
      .map(([k, v]) => `<b>${k}</b>: ${v}`)
      .join("<br/>");
    new maplibregl.Popup()
      .setLngLat((f.geometry as any).coordinates)
      .setHTML(html)
      .addTo(map);
  });

  // double click: predicted sky brightness (SQM) popup
  // (preventDefault stops the default double-click zoom)
  map.on("dblclick", async (e) => {
    e.preventDefault();
    const { lng, lat } = e.lngLat;
    const sqm = await predictedSqm(lng, lat);
    const body =
      sqm === null
        ? "SQMデータを取得できませんでした"
        : `<b>予測SQM</b>: ${sqm.toFixed(2)} mag/arcsec²<br/>` +
          (sqm >= 20
            ? '<span style="color:#1a7f37"><b>暗空条件 (SQM≥20) を満たす</b></span>'
            : '<span style="color:#cf222e">暗空条件 (SQM≥20) を満たさない</span>');
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(`${body}<br/><small>${lat.toFixed(4)}, ${lng.toFixed(4)} / Atlas ${LP_YEAR}</small>`)
      .addTo(map);
  });

  for (const layerId of ["tokyo-points", "miyagi-points", "tokyo-points-old", "miyagi-points-old"]) {
    map.on("mouseenter", layerId, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", layerId, () => (map.getCanvas().style.cursor = ""));
  }

  // light pollution overlay controls
  document.getElementById("lp-toggle")!.addEventListener("change", (ev) => {
    const on = (ev.target as HTMLInputElement).checked;
    map.setLayoutProperty("lp-overlay", "visibility", on ? "visible" : "none");
  });

  // safe-area / bear-buffer layer toggles
  const layerToggles: Array<[string, string[]]> = [
    ["safe-toggle", ["safe-area-fill", "safe-area-line"]],
    ["buffer-toggle", ["bear-buffer-fill", "bear-buffer-line"]],
  ];
  for (const [inputId, layerIds] of layerToggles) {
    document.getElementById(inputId)!.addEventListener("change", (ev) => {
      const on = (ev.target as HTMLInputElement).checked;
      for (const id of layerIds) {
        map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      }
    });
  }
  document.getElementById("lp-opacity")!.addEventListener("input", (ev) => {
    const v = Number((ev.target as HTMLInputElement).value) / 100;
    map.setPaintProperty("lp-overlay", "raster-opacity", v);
  });

  // A prefecture whose feed has stopped updating produces no risk buffers at
  // all, which would read as "hardly any bears here". Say so explicitly rather
  // than letting the empty result speak for itself.
  const staleRegions = (safeArea.features as any[])
    .map((f) => f.properties)
    .filter((p) => p.stale);
  if (staleRegions.length > 0) {
    const banner = document.getElementById("stale-warning")!;
    banner.innerHTML =
      "⚠️ " +
      staleRegions
        .map(
          (p) =>
            `<b>${p.prefecture}</b>の出没情報は${p.age_days}日前(${p.latest})が最新で、` +
            `直近${p.days}日の記録が${p.recent_points}件です。` +
            `熊がいないのではなく情報が更新されていない可能性が高く、` +
            `この県の「低頻出エリア」は信頼できません。`,
        )
        .join("<br/>");
    banner.hidden = false;
  }

  updateCounts(tokyo, miyagi, 0);

  // period filter buttons: "all" clears the filter, otherwise keep features
  // whose timestamp is within the last N days
  const buttons = document.querySelectorAll<HTMLButtonElement>(".filter-btn");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");

      const days = btn.dataset.days!;
      let cutoff = 0;
      if (days !== "all") {
        cutoff = Date.now() - Number(days) * 24 * 60 * 60 * 1000;
      }
      // keep the recent/old split intact and AND the period condition onto it
      const withCutoff = (base: any) =>
        cutoff === 0 ? base : ["all", base, [">=", ["get", "ts"], cutoff]];
      for (const id of ["tokyo-points", "miyagi-points"]) {
        map.setFilter(id, withCutoff(RECENT_F));
      }
      for (const id of ["tokyo-points-old", "miyagi-points-old", "tokyo-old-label", "miyagi-old-label"]) {
        map.setFilter(id, withCutoff(OLD_F));
      }
      updateCounts(tokyo, miyagi, cutoff);
    });
  });
});
