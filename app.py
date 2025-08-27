import io, os, re, tempfile, zipfile
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

import streamlit as st
from PIL import Image
import piexif

# ---------------- Config ----------------
SUPPORTED_WRITE = (".jpg", ".jpeg", ".tif", ".tiff")
SUPPORTED_READ = SUPPORTED_WRITE + (".webp",)

# ---------------- Parsers coordenadas ----------------
def _parse_decimal_pair(text: str):
    # Ej: 50.8291246, 4.3705335  |  "50.8291246 4.3705335"
    m = re.search(r'([-+]?\d+(?:\.\d+)?)\s*[, ]\s*([-+]?\d+(?:\.\d+)?)', text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None

def _parse_google_maps_url(text: str):
    # @lat,lon,zoom
    m = re.search(r'@([-+]?\d+(?:\.\d+)?),\s*([-+]?\d+(?:\.\d+)?)', text)
    if m:
        return float(m.group(1)), float(m.group(2))
    # q=lat,lon
    m = re.search(r'[?&]q=([-+]?\d+(?:\.\d+)?),\s*([-+]?\d+(?:\.\d+)?)', text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None

def _dms_token(tok: str):
    # 50°49'44.9"N  -> (50,49,44.9,'N')
    tok = tok.strip()
    m = re.match(r'(\d+(?:\.\d+)?)°\s*(\d+(?:\.\d+)?)\'\s*(\d+(?:\.\d+)?)"?\s*([NSEW])?', tok, re.IGNORECASE)
    if m:
        d = float(m.group(1)); mnt = float(m.group(2)); sec = float(m.group(3))
        ref = (m.group(4) or '').upper()
        return d, mnt, sec, ref
    return None

def _dms_to_decimal(d, m, s, ref):
    val = d + m/60 + s/3600
    if ref in ('S','W'):
        val = -val
    return val

def _parse_dms(text: str):
    # Formato típico: 50°49'44.9"N 4°22'13.9"E
    parts = re.split(r'\s*[;,]\s*|\s{2,}', text.strip())
    candidates = [p for p in parts if _dms_token(p)]
    if len(candidates) >= 2:
        lat_d, lat_m, lat_s, lat_ref = _dms_token(candidates[0])
        lon_d, lon_m, lon_s, lon_ref = _dms_token(candidates[1])
        if not lat_ref and lon_ref:
            lat_ref = 'N'
        if not lon_ref and lat_ref:
            lon_ref = 'E'
        lat = _dms_to_decimal(lat_d, lat_m, lat_s, lat_ref or 'N')
        lon = _dms_to_decimal(lon_d, lon_m, lon_s, lon_ref or 'E')
        return lat, lon
    return None

def smart_parse_coords(text: str):
    if not text or not text.strip():
        raise ValueError("Cadena vacía.")
    # 1) URL de Google Maps
    p = _parse_google_maps_url(text)
    if p: return p
    # 2) Par decimal
    p = _parse_decimal_pair(text)
    if p: return p
    # 3) DMS
    p = _parse_dms(text)
    if p: return p
    raise ValueError("No se pudieron extraer coordenadas. Pega 'lat, lon', DMS o URL de Google Maps.")

# ---------------- EXIF helpers ----------------
def deg_to_dms_rational(deg: float):
    d = int(abs(deg))
    m_float = (abs(deg) - d) * 60
    m = int(m_float)
    s = round((m_float - m) * 60 * 1000000)
    return ((d, 1), (m, 1), (s, 1000000))

def build_gps_ifd(lat: float, lon: float, alt: Optional[float], when: Optional[datetime]) -> Dict[int, Any]:
    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: deg_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: deg_to_dms_rational(lon),
    }
    if alt is not None:
        gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt >= 0 else 1
        gps_ifd[piexif.GPSIFD.GPSAltitude] = (int(abs(alt) * 100), 100)
    if when is not None:
        when_utc = when.astimezone(timezone.utc)
        gps_ifd[piexif.GPSIFD.GPSDateStamp] = when_utc.strftime("%Y:%m:%d")
        gps_ifd[piexif.GPSIFD.GPSTimeStamp] = (
            (when_utc.hour, 1),
            (when_utc.minute, 1),
            (int(when_utc.second), 1),
        )
    return gps_ifd

def load_exif_from_bytes(data: bytes) -> Dict[str, Any]:
    try:
        return piexif.load(data)
    except Exception:
        return {}

def parse_gps(exif_dict: Dict[str, Any]) -> Dict[str, Any]:
    gps = exif_dict.get("GPS", {})
    if not gps:
        return {"lat": None, "lon": None, "alt": None, "date": None, "time": None}
    def rat(x):
        return x[0]/x[1] if isinstance(x, tuple) and x[1] else float(x)
    def dms_to_deg(dms, ref):
        if not dms:
            return None
        d = rat(dms[0]); m = rat(dms[1]); s = rat(dms[2])
        deg = d + m/60 + s/3600
        if ref in (b"S", b"W"):
            deg = -deg
        return deg
    lat = dms_to_deg(gps.get(piexif.GPSIFD.GPSLatitude), gps.get(piexif.GPSIFD.GPSLatitudeRef, b"?"))
    lon = dms_to_deg(gps.get(piexif.GPSIFD.GPSLongitude), gps.get(piexif.GPSIFD.GPSLongitudeRef, b"?"))
    alt = gps.get(piexif.GPSIFD.GPSAltitude)
    alt = rat(alt) if alt else None
    date = gps.get(piexif.GPSIFD.GPSDateStamp)
    time = gps.get(piexif.GPSIFD.GPSTimeStamp)
    if isinstance(date, (bytes, bytearray)):
        date = date.decode(errors="ignore")
    return {"lat": lat, "lon": lon, "alt": alt, "date": date, "time": time}

# Inserción EXIF robusta (archivo→archivo) para evitar errores de 'insert'
def write_exif_to_image_bytes(img: Image.Image, exif_dict: Dict[str, Any]) -> bytes:
    exif_bytes = piexif.dump(exif_dict)
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.jpg")
        out_path = os.path.join(td, "out.jpg")
        # Exportar a JPEG (si viene WebP, se convierte)
        img.save(in_path, format="JPEG", quality=95)
        # Insertar EXIF usando 3 argumentos (archivo->archivo)
        piexif.insert(exif_bytes, in_path, out_path)
        with open(out_path, "rb") as f:
            return f.read()

def process_file(uploaded_file, lat: float, lon: float, alt: Optional[float], when: Optional[datetime]):
    name = uploaded_file.name
    raw_bytes = uploaded_file.read()
    img = Image.open(io.BytesIO(raw_bytes))

    # EXIF antes
    exif_before = load_exif_from_bytes(raw_bytes)
    before = parse_gps(exif_before)

    # Construir EXIF GPS
    exif_dict = exif_before if exif_before else {"0th":{}, "Exif":{}, "GPS":{}, "1st":{}}
    exif_dict["GPS"] = build_gps_ifd(lat, lon, alt, when)

    # Escribir EXIF y recoger bytes resultantes
    out_bytes = write_exif_to_image_bytes(img, exif_dict)

    # EXIF después
    exif_after = load_exif_from_bytes(out_bytes)
    after = parse_gps(exif_after)

    out_name = name.rsplit(".",1)[0] + "_geo.jpg"
    return out_name, before, after, out_bytes

# ---------------- UI Streamlit ----------------
st.set_page_config(page_title="Geoetiquetador EXIF", page_icon="📍", layout="centered")
st.title("📍 Geoetiquetador de Imágenes (EXIF)")
st.caption("JPEG/TIFF directo. WEBP se convierte a JPEG con EXIF embebido.")

with st.expander("⚙️ Parámetros", expanded=True):
    st.write("Pega coordenadas o una URL de Google Maps y pulsa **Parse** para fijarlas.")

    # Estado inicial
    if "lat" not in st.session_state: st.session_state.lat = 50.8291246
    if "lon" not in st.session_state: st.session_state.lon = 4.3705335

    paste = st.text_input("Coordenadas/URL (ej: '50.8291246, 4.3705335' o un enlace de Google Maps)", value="")

    cparse1, cparse2, _ = st.columns([1,1,2])
    with cparse1:
        if st.button("Parse"):
            try:
                lat_p, lon_p = smart_parse_coords(paste)
                if not (-90 <= lat_p <= 90 and -180 <= lon_p <= 180):
                    raise ValueError("Fuera de rango: lat∈[-90,90], lon∈[-180,180].")
                st.session_state.lat = round(lat_p, 7)
                st.session_state.lon = round(lon_p, 7)
                st.success(f"OK: lat={st.session_state.lat}, lon={st.session_state.lon}")
            except Exception as e:
                st.error(f"No válido: {e}")
    with cparse2:
        lock = st.checkbox("Bloquear coordenadas", value=True, help="Evita cambios accidentales.")

    col1, col2 = st.columns(2)
    with col1:
        lat = st.number_input("Latitud (grados decimales)", value=float(st.session_state.lat), step=0.0000001, format="%.7f", disabled=lock)
        alt = st.number_input("Altitud (m, opcional)", value=0.0, step=0.1)
        use_alt = st.checkbox("Escribir altitud", value=False)
    with col2:
        lon = st.number_input("Longitud (grados decimales)", value=float(st.session_state.lon), step=0.0000001, format="%.7f", disabled=lock)
        use_date = st.checkbox("Escribir fecha/hora GPS (UTC)", value=False)
        if use_date:
            fecha = st.date_input("Fecha", value=datetime.now().date())
            hora = st.time_input("Hora", value=datetime.now().time())
            dt = datetime.combine(fecha, hora)
        else:
            dt = None

files = st.file_uploader(
    "Sube imágenes (.jpg, .jpeg, .tif, .tiff, .webp)",
    accept_multiple_files=True,
    type=[e.strip(".") for e in SUPPORTED_READ],
)

if files and st.button("Geoetiquetar"):
    results: List[Tuple[str, Dict[str,Any], Dict[str,Any], bytes]] = []
    for uf in files:
        try:
            out_name, before, after, out_bytes = process_file(
                uf,
                lat=lat,
                lon=lon,
                alt=(alt if use_alt else None),
                when=(dt if use_date else None),
            )
            results.append((out_name, before, after, out_bytes))
        except Exception as e:
            st.error(f"Error con {uf.name}: {e}")

    if results:
        for out_name, before, after, out_bytes in results:
            st.write(f"**{out_name}**")
            c1, c2 = st.columns(2)
            with c1:
                st.write("Antes (GPS):")
                st.json(before)
            with c2:
                st.write("Después (GPS):")
                st.json(after)
            st.download_button("Descargar imagen geoetiquetada", data=out_bytes, file_name=out_name, mime="image/jpeg")
        if len(results) > 1:
            memzip = io.BytesIO()
            with zipfile.ZipFile(memzip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for out_name, _, _, out_bytes in results:
                    zf.writestr(out_name, out_bytes)
            memzip.seek(0)
            st.download_button("Descargar todo (.zip)", data=memzip, file_name="geotagged_images.zip", mime="application/zip")

st.markdown("""---
**Notas:**
- Escribe EXIF GPS en JPEG/TIFF. Si subes WEBP, se convierte a **JPEG** con EXIF.
- La fecha/hora se guarda en UTC si la activas.
- El parser acepta: `lat, lon` en decimales, DMS (`50°49'44.9"N 4°22'13.9"E`) o URL de Google Maps.
""")
