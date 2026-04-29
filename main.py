from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, ValidationError
from typing import Optional
import xml.etree.ElementTree as ET
from pyproj import Transformer
import gpxpy
import gpxpy.gpx
import math
import io
import csv
from datetime import datetime, timedelta
import os


app = FastAPI(
    title="StreetViewer API",
    description="API para el visor geoespacial 360° de inspección vial.",
    version="1.3.0",
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:4200",
        "http://localhost:4300",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

UTM_TO_WGS84 = Transformer.from_crs("EPSG:32718", "EPSG:4326", always_xy=True)
WGS84_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32718", always_xy=True)


class CoordUTM(BaseModel):
    x: float
    y: float


class CoordWGS84(BaseModel):
    lng: float
    lat: float


class Poste(BaseModel):
    id: int
    x: float
    y: float
    time: float
    descripcion: Optional[str] = None


class MatrizRequest(BaseModel):
    postes: list[Poste]


class AsistidoRequest(BaseModel):
    current_time: float
    postes: list[Poste]


def parse_matriz_body(body_raw: str) -> MatrizRequest:
    try:
        return MatrizRequest.model_validate_json(body_raw)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    except Exception:
        raise HTTPException(status_code=422, detail="El campo 'body' no contiene JSON válido.")


def parse_asistido_body(body_raw: str) -> AsistidoRequest:
    try:
        return AsistidoRequest.model_validate_json(body_raw)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())
    except Exception:
        raise HTTPException(status_code=422, detail="El campo 'body' no contiene JSON válido.")


def decode_upload(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


def utm_to_wgs84(x: float, y: float) -> tuple[float, float]:
    lng, lat = UTM_TO_WGS84.transform(x, y)
    return lat, lng


def wgs84_to_utm(lat: float, lng: float) -> tuple[float, float]:
    x, y = WGS84_TO_UTM.transform(lng, lat)
    return x, y


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def project_point_on_segment(p, p1, p2):
    ax, ay = p
    bx, by = p1
    cx, cy = p2
    dx, dy = cx - bx, cy - by
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return p1
    t = max(0, min(1, ((ax - bx) * dx + (ay - by) * dy) / len_sq))
    return bx + t * dx, by + t * dy


def closest_point_on_polyline(lat, lng, coords_wgs84: list[tuple]) -> tuple:
    if len(coords_wgs84) < 2:
        return coords_wgs84[0], 0.0
    best_pt = coords_wgs84[0]
    best_dist = float("inf")
    for i in range(len(coords_wgs84) - 1):
        proj = project_point_on_segment((lat, lng), coords_wgs84[i], coords_wgs84[i + 1])
        d = haversine_m(lat, lng, proj[0], proj[1])
        if d < best_dist:
            best_dist = d
            best_pt = proj
    return best_pt, best_dist


def parse_coordinate_token(token: str) -> Optional[tuple[float, float, float]]:
    parts = token.split(",")
    if len(parts) < 2:
        return None
    try:
        lng = float(parts[0].strip())
        lat = float(parts[1].strip())
        alt = float(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else 0.0
        return lng, lat, alt
    except ValueError:
        return None


def parse_route_coords_from_kml(kml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError:
        raise HTTPException(422, "KML inválido (no se pudo parsear XML).")

    candidate_lines: list[list[dict]] = []
    for coords_el in root.findall(".//{*}LineString/{*}coordinates"):
        if not coords_el.text:
            continue
        raw = coords_el.text.strip()
        tokens = raw.split()
        line_points = []
        for token in tokens:
            parsed = parse_coordinate_token(token)
            if parsed is None:
                continue
            lng, lat, alt = parsed
            line_points.append({"lat": lat, "lng": lng, "alt": alt})
        if len(line_points) >= 2:
            candidate_lines.append(line_points)

    if not candidate_lines:
        return []

    longest = max(candidate_lines, key=len)
    points = []
    for idx, p in enumerate(longest):
        points.append({"index": idx, "lat": p["lat"], "lng": p["lng"], "alt": p["alt"]})
    return points


def parse_poste_points_from_kml(kml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError:
        raise HTTPException(422, "KML de postes inválido (no se pudo parsear XML).")

    out = []
    idx = 1
    for pm in root.findall(".//{*}Placemark"):
        coord_el = pm.find(".//{*}Point/{*}coordinates")
        if coord_el is None or not coord_el.text:
            continue
        first_token = coord_el.text.strip().split()[0]
        parsed = parse_coordinate_token(first_token)
        if parsed is None:
            continue
        lng, lat, _ = parsed
        out.append({"id": idx, "lat": lat, "lng": lng})
        idx += 1

    return out


def build_cumulative_distance(route_coords: list[dict]) -> list[float]:
    cum_dist = [0.0]
    for i in range(1, len(route_coords)):
        d = haversine_m(
            route_coords[i - 1]["lat"], route_coords[i - 1]["lng"],
            route_coords[i]["lat"], route_coords[i]["lng"]
        )
        cum_dist.append(cum_dist[-1] + d)
    return cum_dist


def nearest_route_index(lat: float, lng: float, route_coords: list[dict]) -> int:
    min_d = float("inf")
    best_idx = 0
    for i, c in enumerate(route_coords):
        d = haversine_m(lat, lng, c["lat"], c["lng"])
        if d < min_d:
            min_d = d
            best_idx = i
    return best_idx


def maybe_reverse_route_by_times(postes: list[Poste], route_coords: list[dict]) -> list[dict]:
    if len(postes) < 2 or len(route_coords) < 2:
        return route_coords

    ordered = sorted(postes, key=lambda p: p.time)
    cum_dist = build_cumulative_distance(route_coords)

    first_lat, first_lng = utm_to_wgs84(ordered[0].x, ordered[0].y)
    last_lat, last_lng = utm_to_wgs84(ordered[-1].x, ordered[-1].y)
    first_idx = nearest_route_index(first_lat, first_lng, route_coords)
    last_idx = nearest_route_index(last_lat, last_lng, route_coords)

    if cum_dist[last_idx] < cum_dist[first_idx]:
        reversed_route = list(reversed(route_coords))
        for i, p in enumerate(reversed_route):
            p["index"] = i
        return reversed_route

    return route_coords


def interpolate_time_on_route(postes: list[Poste], route_coords: list[dict]) -> list[dict]:
    if not postes or len(postes) < 2 or len(route_coords) < 2:
        return route_coords

    route_coords = maybe_reverse_route_by_times(postes, route_coords)
    cum_dist = build_cumulative_distance(route_coords)

    anchors = []
    for poste in sorted(postes, key=lambda p: p.time):
        lat_p, lng_p = utm_to_wgs84(poste.x, poste.y)
        idx = nearest_route_index(lat_p, lng_p, route_coords)
        anchors.append({"route_idx": idx, "cum_dist": cum_dist[idx], "time": poste.time})

    anchors.sort(key=lambda a: a["cum_dist"])

    enriched = []
    for i, pt in enumerate(route_coords):
        d = cum_dist[i]
        before = [a for a in anchors if a["cum_dist"] <= d]
        after = [a for a in anchors if a["cum_dist"] > d]

        if before and after:
            a0 = before[-1]
            a1 = after[0]
            span = a1["cum_dist"] - a0["cum_dist"]
            t = a0["time"] + (d - a0["cum_dist"]) / span * (a1["time"] - a0["time"]) if span > 0 else a0["time"]
        elif before:
            t = before[-1]["time"]
        elif after:
            t = after[0]["time"]
        else:
            t = 0.0

        enriched.append({**pt, "tiempo_video_s": round(t, 4)})

    return enriched


def find_poste_asistido(current_time: float, postes: list[Poste], ruta_coords: list[dict]) -> int:
    if not postes:
        raise HTTPException(400, "No hay postes para sugerir.")

    calibrados = [p for p in postes if p.time > 0]
    sin_calibrar = [p for p in postes if p.time <= 0]

    if not sin_calibrar:
        raise HTTPException(400, "Todos los postes ya están calibrados.")

    if len(calibrados) < 2 or len(ruta_coords) < 2:
        return sin_calibrar[0].id

    ruta_tiempo = interpolate_time_on_route(calibrados, ruta_coords)
    if not ruta_tiempo:
        return sin_calibrar[0].id

    best_pt = min(ruta_tiempo, key=lambda r: abs(r["tiempo_video_s"] - current_time))

    best_id = sin_calibrar[0].id
    best_dist = float("inf")
    for p in sin_calibrar:
        lat_p, lng_p = utm_to_wgs84(p.x, p.y)
        d = haversine_m(lat_p, lng_p, best_pt["lat"], best_pt["lng"])
        if d < best_dist:
            best_dist = d
            best_id = p.id

    return best_id


@app.get("/", tags=["Info"])
def root():
    return {
        "api": "StreetViewer Geo-Espacial",
        "version": "1.3.0",
        "utm_zona": "18 Sur (EPSG:32718)",
        "datum": "WGS84",
    }


@app.post("/coords/utm-to-wgs84", tags=["Coordenadas"])
def convertir_utm_a_wgs84(coord: CoordUTM):
    try:
        lat, lng = utm_to_wgs84(coord.x, coord.y)
        return {
            "entrada": {"x_este": coord.x, "y_norte": coord.y},
            "salida": {"lat": lat, "lng": lng},
        }
    except Exception as e:
        raise HTTPException(400, f"Error: {e}")


@app.post("/coords/wgs84-to-utm", tags=["Coordenadas"])
def convertir_wgs84_a_utm(coord: CoordWGS84):
    try:
        x, y = wgs84_to_utm(coord.lat, coord.lng)
        return {
            "entrada": {"lat": coord.lat, "lng": coord.lng},
            "salida": {"x_este": round(x, 3), "y_norte": round(y, 3)},
        }
    except Exception as e:
        raise HTTPException(400, f"Error: {e}")


@app.post("/kml/parse-ruta", tags=["KML"])
async def parsear_kml_ruta(file: UploadFile = File(...)):
    content = await file.read()
    kml_text = decode_upload(content)
    puntos = parse_route_coords_from_kml(kml_text)

    if not puntos:
        raise HTTPException(422, "No se encontraron LineString/coordinates válidas en la ruta.")

    dist_acum = build_cumulative_distance(puntos)
    for i, p in enumerate(puntos):
        p["dist_acumulada_m"] = round(dist_acum[i], 3)

    return {
        "archivo": file.filename,
        "total_puntos": len(puntos),
        "distancia_total_m": round(dist_acum[-1], 2),
        "puntos": puntos,
    }


@app.post("/kml/parse-postes", tags=["KML"])
async def parsear_kml_postes(file_postes: UploadFile = File(...), file_eje: UploadFile = File(...)):
    content_postes = decode_upload(await file_postes.read())
    content_eje = decode_upload(await file_eje.read())

    ruta_coords = parse_route_coords_from_kml(content_eje)
    if not ruta_coords:
        raise HTTPException(422, "Eje inválido: no se encontró una LineString válida.")

    poste_points = parse_poste_points_from_kml(content_postes)
    if not poste_points:
        raise HTTPException(422, "No se encontraron postes tipo Point en el KML de postes.")

    route_latlng = [(p["lat"], p["lng"]) for p in ruta_coords]
    postes_out = []

    for p in poste_points:
        lat_p, lng_p = p["lat"], p["lng"]
        x_utm, y_utm = wgs84_to_utm(lat_p, lng_p)
        proj_pt, dist_eje = closest_point_on_polyline(lat_p, lng_p, route_latlng)

        postes_out.append({
            "id": p["id"],
            "wgs84": {"lat": round(lat_p, 8), "lng": round(lng_p, 8)},
            "utm": {"x_este": round(x_utm, 3), "y_norte": round(y_utm, 3)},
            "proyectado_en_eje": {"lat": round(proj_pt[0], 8), "lng": round(proj_pt[1], 8)},
            "distancia_al_eje_m": round(dist_eje, 3),
            "time": 0.0,
        })

    return {"postes": postes_out}


@app.post("/asistido/sugerir-poste", tags=["Asistido"])
async def sugerir_poste_asistido(body: str = Form(...), file_eje: UploadFile = File(...)):
    payload = parse_asistido_body(body)
    content = decode_upload(await file_eje.read())
    ruta_coords = parse_route_coords_from_kml(content)

    if not ruta_coords:
        raise HTTPException(422, "Eje inválido: no se encontró una LineString válida.")

    poste_id = find_poste_asistido(payload.current_time, payload.postes, ruta_coords)
    return {"poste_id": poste_id}


@app.post("/matriz/generar", tags=["Matriz"])
async def generar_matriz(body: str = Form(...), file_eje: UploadFile = File(...)):
    payload = parse_matriz_body(body)
    content = decode_upload(await file_eje.read())
    ruta_coords = parse_route_coords_from_kml(content)

    if not ruta_coords:
        raise HTTPException(422, "Eje inválido: no se encontró una LineString válida.")

    postes_validos = [p for p in payload.postes if p.time > 0]
    if len(postes_validos) < 2:
        raise HTTPException(400, "Faltan postes calibrados (mínimo 2 con tiempo > 0).")

    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)
    return {"puntos": enriquecidos}


@app.post("/exportar/gpx", tags=["Exportación"])
async def exportar_gpx(body: str = Form(...), file_eje: UploadFile = File(...)):
    payload = parse_matriz_body(body)
    content = decode_upload(await file_eje.read())
    ruta_coords = parse_route_coords_from_kml(content)

    if not ruta_coords:
        raise HTTPException(422, "Eje inválido: no se encontró una LineString válida.")

    postes_validos = sorted([p for p in payload.postes if p.time > 0], key=lambda p: p.time)
    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)

    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(track)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)

    t0 = datetime(2000, 1, 1)
    for pt in enriquecidos:
        segment.points.append(
            gpxpy.gpx.GPXTrackPoint(
                pt["lat"],
                pt["lng"],
                elevation=pt.get("alt", 0),
                time=t0 + timedelta(seconds=pt.get("tiempo_video_s", 0)),
            )
        )

    return StreamingResponse(
        io.BytesIO(gpx.to_xml().encode("utf-8")),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": 'attachment; filename="ruta.gpx"'},
    )


@app.post("/exportar/csv-postes", tags=["Exportación"])
async def exportar_csv_postes(body: str = Form(...)):
    payload = parse_matriz_body(body)

    postes = sorted(payload.postes, key=lambda p: p.time if p.time > 0 else float("inf"))
    calibrados = [p for p in postes if p.time > 0]
    source = calibrados if calibrados else postes

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Latitud", "Longitud", "Tiempo", "track"])

    t0 = datetime(2000, 1, 1)
    for idx, p in enumerate(source, start=1):
        lat, lng = utm_to_wgs84(p.x, p.y)
        ts = (t0 + timedelta(seconds=float(p.time))).isoformat() + "Z"
        writer.writerow([round(lat, 7), round(lng, 7), ts, idx])

    content = output.getvalue()
    output.close()

    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="postes_calibrados.csv"'},
    )