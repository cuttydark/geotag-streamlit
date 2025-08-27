import io, os, tempfile, zipfile
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

import streamlit as st
from PIL import Image
import piexif

# --- Config ---
SUPPORTED_WRITE = (".jpg", ".jpeg", ".tif", ".tiff")
SUPPORTED_READ = SUPPORTED_WRITE + (".webp",)

# --- Utilidades EXIF ---
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

# --- Inserción EXIF robusta (modo archivo -> evita el error de 3er argumento) ---
def write_exif_to_image_bytes(img: Image.Image, exif_dict: Dict[str, Any]) -> bytes:
    exif_bytes = piexif.dump(exif_dict)

    # Siempre exportamos primero a JPEG temporal
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.jpg")
        out_path = os.path.join(td, "out.jpg")

        # Guardar imagen a JPEG (sin EXIF) en disco
        img.save(in_path, format="JPEG", quality=95)

        # Insertar EXIF usando la versión de 3 argumentos (archivo→archivo)
        piexif.insert(exif_bytes, in_path, out_path)

        # Leer resultado a bytes y devolver
        with open(out_path, "rb") as f:
            return f.read()

def process_file(uploaded_file, lat: float, lon: float, alt: Optional[float], when: Optional[datetime]):
    name = uploaded_file.name
    raw_bytes = uploaded_file.read()
    img = Image.open(io.BytesIO(raw_bytes))

    # EXIF antes (si existía)
    exif_before = load_exif_from_bytes(raw_bytes)
    before = parse_gps(exif_before)

    # Construir EXIF nuevo
    exif_dict = exif_before if exif_before else {"0th":{}, "Exif":{}, "GPS":{}, "1st":{}}
    exif_dict["GPS"] = build_gps_ifd(lat, lon, alt, when)

    # Escribir EXIF de forma robusta
    out_bytes = write_exif_to_image_bytes(img, exif_dict)

    # Validar EXIF después
    exif_after = load_exif_from_bytes(out_bytes)
    after = parse_gps(exif_after)

    out_name = name.rsplit(".",1)[0] + "_geo.jpg"
    return out_name, before, after, out_bytes

# --- UI Streamlit ---
st.set_page_config(page_title="Geoetiquetador EXIF", page_icon="📍", layout="centered")
st.title("📍 Geoetiquetador de Imágenes (EXIF)")
st.caption("JPEG/TIFF directo. WEBP se convierte a JPEG con EXIF embebido.")

with st.expander("⚙️ Parámetros", expanded=True):
    col1, col2 = st.columns(2)
    with col1:
        lat = st.number_input("Latitud (grados decimales)", value=50.8291246, step=0.0000001, format="%.7f")
        alt = st.number_input("Altitud (m, opcional)", value=0.0, step=0.1)
        use_alt = st.checkbox("Escribir altitud", value=False)
    with col2:
        lon = st.number_input("Longitud (grados decimales)", value=4.3705335, step=0.0000001, format="%.7f")
        use_date = st.checkbox("Escribir fecha/hora GPS (UTC)", value=False)
        if use_date:
            fecha = st.date_input("Fecha", value=datetime.now().date())
            hora = st.time_input("Hora", value=datetime.now().time())
            # Combinar fecha y hora en un datetime
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
- Escribe EXIF GPS en JPEG/TIFF. Si subes WEBP, se convierte a JPEG con EXIF.
- La fecha/hora se guarda en UTC si la marcas.
""")
