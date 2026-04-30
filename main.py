from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from typing import Optional
import xml.etree.ElementTree as ET
from pyproj import Transformer
import gpxpy
import gpxpy.gpx
import math
import io
import csv
from datetime import datetime, timedelta, timezone
import os

app = FastAPI(
    title="StreetViewer API",
    description="API para el visor geoespacial 360° de inspección vial.",
    version="1.3.2",
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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

    points: list[tuple[float, float]] = []

    for pm in root.findall(".//{*}Placemark"):
        coord_el = pm.find(".//{*}Point/{*}coordinates")
        if coord_el is not None and coord_el.text:
            first_token = coord_el.text.strip().split()[0]
            parsed = parse_coordinate_token(first_token)
            if parsed is not None:
                lng, lat, _ = parsed
                points.append((lat, lng))
            continue

        coord_el2 = pm.find(".//{*}coordinates")
        if coord_el2 is not None and coord_el2.text:
            tokens = coord_el2.text.strip().split()
            if tokens:
                parsed = parse_coordinate_token(tokens[0])
                if parsed is not None:
                    lng, lat, _ = parsed
                    points.append((lat, lng))

    out = []
    for i, (lat, lng) in enumerate(points, start=1):
        out.append({"id": i, "lat": lat, "lng": lng})
    return out


def build_route_geometry(route_coords: list[dict]) -> list[dict]:
    geometry = []
    cumulative = 0.0

    for i, point in enumerate(route_coords):
        x, y = wgs84_to_utm(point["lat"], point["lng"])
        if i > 0:
            prev = geometry[-1]
            cumulative += math.hypot(x - prev["x"], y - prev["y"])
        geometry.append(
            {
                "index": i,
                "lat": point["lat"],
                "lng": point["lng"],
                "alt": point.get("alt", 0.0),
                "x": x,
                "y": y,
                "cum_dist": cumulative,
            }
        )

    return geometry


def project_point_on_segment_utm(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> tuple[float, float, float]:
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return ax, ay, 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return ax + t * dx, ay + t * dy, t


def project_point_on_route_utm(px: float, py: float, route_geometry: list[dict]) -> dict:
    if len(route_geometry) < 2:
        first = route_geometry[0]
        return {
            "x": first["x"],
            "y": first["y"],
            "route_distance": 0.0,
            "distance_to_route": math.hypot(px - first["x"], py - first["y"]),
            "segment_index": 0,
            "lat": first["lat"],
            "lng": first["lng"],
        }

    best = None
    min_dist = float("inf")

    for i in range(len(route_geometry) - 1):
        a = route_geometry[i]
        b = route_geometry[i + 1]
        sx, sy, t = project_point_on_segment_utm(px, py, a["x"], a["y"], b["x"], b["y"])
        dist = math.hypot(px - sx, py - sy)
        segment_len = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        route_distance = a["cum_dist"] + segment_len * t

        if dist < min_dist:
            min_dist = dist
            best = {
                "x": sx,
                "y": sy,
                "route_distance": route_distance,
                "distance_to_route": dist,
                "segment_index": i,
            }

    if best is None:
        first = route_geometry[0]
        return {
            "x": first["x"],
            "y": first["y"],
            "route_distance": 0.0,
            "distance_to_route": math.hypot(px - first["x"], py - first["y"]),
            "segment_index": 0,
        }

    best_lat, best_lng = utm_to_wgs84(best["x"], best["y"])
    best["lat"] = best_lat
    best["lng"] = best_lng
    return best


def closest_point_on_polyline(lat: float, lng: float, route_coords: list[dict]) -> tuple[tuple[float, float], float]:
    if not route_coords:
        return (lat, lng), 0.0

    if len(route_coords) < 2:
        first = route_coords[0]
        return (first["lat"], first["lng"]), 0.0

    route_geometry = build_route_geometry(route_coords)
    px, py = wgs84_to_utm(lat, lng)
    projected = project_point_on_route_utm(px, py, route_geometry)
    return (projected["lat"], projected["lng"]), projected["distance_to_route"]


def build_cumulative_distance(route_coords: list[dict]) -> list[float]:
    route_geometry = build_route_geometry(route_coords)
    return [point["cum_dist"] for point in route_geometry]


def maybe_reverse_route_by_times(postes: list[Poste], route_coords: list[dict]) -> Optional[bool]:
    if len(postes) < 2 or len(route_coords) < 2:
        return None

    calibrated = sorted([p for p in postes if p.time > 0], key=lambda p: p.time)
    if len(calibrated) < 2:
        return None

    route_geometry = build_route_geometry(route_coords)
    first_x, first_y = calibrated[0].x, calibrated[0].y
    last_x, last_y = calibrated[-1].x, calibrated[-1].y
    first_proj = project_point_on_route_utm(first_x, first_y, route_geometry)
    last_proj = project_point_on_route_utm(last_x, last_y, route_geometry)

    return last_proj["route_distance"] < first_proj["route_distance"]


def maybe_reverse_route_by_poste_ids(postes: list[Poste], route_coords: list[dict]) -> Optional[bool]:
    if len(postes) < 2 or len(route_coords) < 2:
        return None

    route_geometry = build_route_geometry(route_coords)
    ordered = sorted(postes, key=lambda p: p.id)
    first = ordered[0]
    last = ordered[-1]
    first_proj = project_point_on_route_utm(first.x, first.y, route_geometry)
    last_proj = project_point_on_route_utm(last.x, last.y, route_geometry)

    if abs(last_proj["route_distance"] - first_proj["route_distance"]) < 1e-6:
        return None

    return last_proj["route_distance"] < first_proj["route_distance"]


def maybe_reverse_route(postes: list[Poste], route_coords: list[dict]) -> list[dict]:
    decision = maybe_reverse_route_by_times(postes, route_coords)
    if decision is None:
        decision = maybe_reverse_route_by_poste_ids(postes, route_coords)

    if decision:
        return list(reversed(route_coords))
    return route_coords


def interpolate_time_on_route(postes: list[Poste], route_coords: list[dict]) -> list[dict]:
    if not postes or len(postes) < 2 or len(route_coords) < 2:
        return [{**point, "tiempo_video_s": 0.0} for point in route_coords]

    route_coords = maybe_reverse_route(postes, route_coords)
    route_geometry = build_route_geometry(route_coords)

    anchors = []
    for poste in sorted([p for p in postes if p.time > 0], key=lambda p: p.time):
        projected = project_point_on_route_utm(poste.x, poste.y, route_geometry)
        anchors.append(
            {
                "route_distance": projected["route_distance"],
                "time": poste.time,
                "lat": projected["lat"],
                "lng": projected["lng"],
            }
        )

    if len(anchors) < 2:
        return [
            {**point, "tiempo_video_s": round(anchors[0]["time"], 4) if anchors else 0.0}
            for point in route_geometry
        ]

    anchors.sort(key=lambda a: a["route_distance"])

    deduped = []
    for anchor in anchors:
        if not deduped or abs(anchor["route_distance"] - deduped[-1]["route_distance"]) > 1e-9:
            deduped.append(anchor)
        else:
            deduped[-1] = anchor

    enriched = []
    for point in route_geometry:
        d = point["cum_dist"]
        before = [a for a in deduped if a["route_distance"] <= d]
        after = [a for a in deduped if a["route_distance"] > d]

        if before and after:
            a0 = before[-1]
            a1 = after[0]
            span = a1["route_distance"] - a0["route_distance"]
            if span > 0:
                t = a0["time"] + (d - a0["route_distance"]) * (a1["time"] - a0["time"]) / span
            else:
                t = a0["time"]
        elif before:
            t = before[-1]["time"]
        elif after:
            t = after[0]["time"]
        else:
            t = 0.0

        enriched.append(
            {
                "index": point["index"],
                "lat": point["lat"],
                "lng": point["lng"],
                "alt": point["alt"],
                "dist_acum_m": round(point["cum_dist"], 3),
                "tiempo_video_s": round(t, 4),
            }
        )

    return enriched


def parse_video_base_datetime(
    video_datetime: Optional[str],
    video_fecha: Optional[str],
    video_hora: Optional[str],
) -> datetime:
    if video_datetime:
        s = video_datetime.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            raise HTTPException(422, "video_datetime inválido. Usa ISO 8601, ej: 2026-04-30T11:35:00Z")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    if video_fecha and video_hora:
        try:
            dt = datetime.fromisoformat(f"{video_fecha.strip()}T{video_hora.strip()}")
        except Exception:
            raise HTTPException(422, "video_fecha/video_hora inválidos. Ej: video_fecha=2026-04-30, video_hora=11:35:00")
        return dt.replace(tzinfo=timezone.utc)

    return datetime(2000, 1, 1, tzinfo=timezone.utc)


def build_track_export_payload(postes: list[Poste], ruta_coords: list[dict], base_dt: Optional[datetime] = None) -> list[dict]:
    enriched = interpolate_time_on_route(postes, ruta_coords)
    track = []
    base = (base_dt or datetime(2000, 1, 1, tzinfo=timezone.utc)).astimezone(timezone.utc)

    for idx, point in enumerate(enriched, start=1):
        dt = base + timedelta(seconds=float(point.get("tiempo_video_s", 0.0)))
        track.append(
            {
                "track": idx,
                "latitud": round(point["lat"], 8),
                "longitud": round(point["lng"], 8),
                "fecha": dt.date().isoformat(),
                "hora_ms": dt.strftime("%H:%M:%S.%f")[:-3],
                "timestamp_iso": dt.isoformat().replace("+00:00", "Z"),
                "tiempo_video_s": round(float(point.get("tiempo_video_s", 0.0)), 4),
            }
        )

    return track


def nearest_route_index(lat: float, lng: float, route_coords: list[dict]) -> int:
    route_geometry = build_route_geometry(route_coords)
    px, py = wgs84_to_utm(lat, lng)
    projected = project_point_on_route_utm(px, py, route_geometry)
    return projected["segment_index"]


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
        "version": "1.3.2",
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
        raise HTTPException(422, "No se encontraron coordenadas en el KML de postes.")

    route_geometry = build_route_geometry(ruta_coords)
    postes_out = []

    for p in poste_points:
        lat_p, lng_p = p["lat"], p["lng"]
        x_utm, y_utm = wgs84_to_utm(lat_p, lng_p)
        projected = project_point_on_route_utm(x_utm, y_utm, route_geometry)

        postes_out.append(
            {
                "id": p["id"],
                "wgs84": {"lat": round(lat_p, 8), "lng": round(lng_p, 8)},
                "utm": {"x_este": round(x_utm, 3), "y_norte": round(y_utm, 3)},
                "proyectado_en_eje": {"lat": round(projected["lat"], 8), "lng": round(projected["lng"], 8)},
                "distancia_al_eje_m": round(projected["distance_to_route"], 3),
                "time": 0.0,
            }
        )

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


@app.post("/exportar/json", tags=["Exportación"])
async def exportar_json(body: str = Form(...), file_eje: UploadFile = File(...)):
    payload = parse_matriz_body(body)
    content = decode_upload(await file_eje.read())
    ruta_coords = parse_route_coords_from_kml(content)

    if not ruta_coords:
        raise HTTPException(422, "Eje inválido: no se encontró una LineString válida.")

    postes_validos = [p for p in payload.postes if p.time > 0]
    if len(postes_validos) < 2:
        raise HTTPException(400, "Faltan postes calibrados (mínimo 2 con tiempo > 0).")

    return {"puntos": interpolate_time_on_route(postes_validos, ruta_coords)}


@app.post("/exportar/gpx", tags=["Exportación"])
async def exportar_gpx(
    body: str = Form(...),
    file_eje: UploadFile = File(...),
    video_datetime: Optional[str] = Form(None),
    video_fecha: Optional[str] = Form(None),
    video_hora: Optional[str] = Form(None),
):
    payload = parse_matriz_body(body)
    content = decode_upload(await file_eje.read())
    ruta_coords = parse_route_coords_from_kml(content)

    if not ruta_coords:
        raise HTTPException(422, "Eje inválido: no se encontró una LineString válida.")

    base = parse_video_base_datetime(video_datetime, video_fecha, video_hora)
    postes_validos = [p for p in payload.postes if p.time > 0]
    postes_ordenados = sorted(
        postes_validos if postes_validos else payload.postes,
        key=lambda p: (p.time if p.time > 0 else float("inf"), p.id),
    )
    gpx = gpxpy.gpx.GPX()

    for poste in postes_ordenados:
        lat, lng = utm_to_wgs84(poste.x, poste.y)
        dt = base + timedelta(seconds=float(poste.time))
        timestamp = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        gpx.waypoints.append(
            gpxpy.gpx.GPXWaypoint(
                latitude=lat,
                longitude=lng,
                name=str(poste.id),
                description=f"Hora registrada: {timestamp}",
                time=dt,
            )
        )

    return StreamingResponse(
        io.BytesIO(gpx.to_xml().encode("utf-8")),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": 'attachment; filename="ruta.gpx"'},
    )


@app.post("/exportar/csv-postes", tags=["Exportación"])
async def exportar_csv_postes(
    body: str = Form(...),
    video_datetime: Optional[str] = Form(None),
    video_fecha: Optional[str] = Form(None),
    video_hora: Optional[str] = Form(None),
):
    payload = parse_matriz_body(body)

    base = parse_video_base_datetime(video_datetime, video_fecha, video_hora)

    postes_validos = [p for p in payload.postes if p.time > 0]
    source = sorted(
        postes_validos if postes_validos else payload.postes,
        key=lambda p: (p.time if p.time > 0 else float("inf"), p.id),
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Latitud", "Longitud", "Tiempo", "track"])

    for p in source:
        lat, lng = utm_to_wgs84(p.x, p.y)
        dt = base + timedelta(seconds=float(p.time))
        tiempo = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        writer.writerow([round(lat, 8), round(lng, 8), tiempo, p.id])

    content = output.getvalue()
    output.close()

    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="postes_calibrados.csv"'},
    )
