from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import xml.etree.ElementTree as ET
from pyproj import Transformer
import gpxpy
import gpxpy.gpx
import json
import math
import io
from datetime import datetime, timedelta
import os

app = FastAPI(
    title="StreetViewer API",
    description="API para el visor geoespacial 360° de inspección vial.",
    version="1.0.0",
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

class BuscarUTMRequest(BaseModel):
    x: float
    y: float
    postes: list[Poste]

def utm_to_wgs84(x: float, y: float) -> tuple[float, float]:
    lng, lat = UTM_TO_WGS84.transform(x, y)
    return lat, lng

def wgs84_to_utm(lat: float, lng: float) -> tuple[float, float]:
    x, y = WGS84_TO_UTM.transform(lng, lat)
    return x, y

def haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def project_point_on_segment(p, p1, p2):
    ax, ay = p
    bx, by = p1
    cx, cy = p2
    dx, dy = cx - bx, cy - by
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return p1
    t = max(0, min(1, ((ax - bx) * dx + (ay - by) * dy) / len_sq))
    return (bx + t * dx, by + t * dy)

def closest_point_on_polyline(lat, lng, coords_wgs84: list[tuple]) -> tuple:
    best_pt = coords_wgs84[0]
    best_dist = float("inf")
    for i in range(len(coords_wgs84) - 1):
        proj = project_point_on_segment((lat, lng), coords_wgs84[i], coords_wgs84[i + 1])
        d = haversine_m(lat, lng, proj[0], proj[1])
        if d < best_dist:
            best_dist = d
            best_pt = proj
    return best_pt, best_dist

def parse_kml_coords(kml_text: str) -> list[dict]:
    kml_text_clean = kml_text.replace('xmlns="http://www.opengis.net/kml/2.2"', "")
    root = ET.fromstring(kml_text_clean)
    points = []
    idx = 0
    for coords_el in root.iter("coordinates"):
        if coords_el.text:
            raw = coords_el.text.strip()
            tokens = raw.split()
            for token in tokens:
                parts = token.split(",")
                if len(parts) >= 2:
                    try:
                        lng = float(parts[0].strip())
                        lat = float(parts[1].strip())
                        alt = float(parts[2].strip()) if len(parts) > 2 else 0.0
                        points.append({"index": idx, "lat": lat, "lng": lng, "alt": alt})
                        idx += 1
                    except ValueError:
                        continue
    return points

def interpolate_time_on_route(postes: list[Poste], route_coords: list[dict]) -> list[dict]:
    if not postes or len(postes) < 2:
        return route_coords
    cum_dist = [0.0]
    for i in range(1, len(route_coords)):
        d = haversine_m(
            route_coords[i - 1]["lat"], route_coords[i - 1]["lng"],
            route_coords[i]["lat"],     route_coords[i]["lng"]
        )
        cum_dist.append(cum_dist[-1] + d)

    route_latlng = [(p["lat"], p["lng"]) for p in route_coords]
    anchors = []
    for poste in sorted(postes, key=lambda p: p.time):
        lat_p, lng_p = utm_to_wgs84(poste.x, poste.y)
        min_d = float("inf")
        best_idx = 0
        for i, c in enumerate(route_coords):
            d = haversine_m(lat_p, lng_p, c["lat"], c["lng"])
            if d < min_d:
                min_d = d
                best_idx = i
        anchors.append({"route_idx": best_idx, "cum_dist": cum_dist[best_idx], "time": poste.time})

    enriched = []
    for i, pt in enumerate(route_coords):
        d = cum_dist[i]
        before = [a for a in anchors if a["cum_dist"] <= d]
        after  = [a for a in anchors if a["cum_dist"] >  d]
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

@app.get("/", tags=["Info"])
def root():
    return {
        "api": "StreetViewer Geo-Espacial",
        "version": "1.0.0",
        "utm_zona": "18 Sur (EPSG:32718)",
        "datum": "WGS84"
    }

@app.post("/coords/utm-to-wgs84", tags=["Coordenadas"])
def convertir_utm_a_wgs84(coord: CoordUTM):
    try:
        lat, lng = utm_to_wgs84(coord.x, coord.y)
        return {
            "entrada": {"x_este": coord.x, "y_norte": coord.y},
            "salida":  {"lat": lat, "lng": lng},
        }
    except Exception as e:
        raise HTTPException(400, f"Error: {e}")

@app.post("/coords/wgs84-to-utm", tags=["Coordenadas"])
def convertir_wgs84_a_utm(coord: CoordWGS84):
    try:
        x, y = wgs84_to_utm(coord.lat, coord.lng)
        return {
            "entrada": {"lat": coord.lat, "lng": coord.lng},
            "salida":  {"x_este": round(x, 3), "y_norte": round(y, 3)},
        }
    except Exception as e:
        raise HTTPException(400, f"Error: {e}")

@app.post("/kml/parse-ruta", tags=["KML"])
async def parsear_kml_ruta(file: UploadFile = File(...)):
    content = await file.read()
    try:
        kml_text = content.decode("utf-8")
    except UnicodeDecodeError:
        kml_text = content.decode("latin-1")

    puntos = parse_kml_coords(kml_text)
    if not puntos:
        raise HTTPException(422, "No se encontraron coordenadas.")

    dist_acum = [0.0]
    for i in range(1, len(puntos)):
        d = haversine_m(puntos[i-1]["lat"], puntos[i-1]["lng"], puntos[i]["lat"], puntos[i]["lng"])
        dist_acum.append(dist_acum[-1] + d)

    for i, p in enumerate(puntos):
        p["dist_acumulada_m"] = round(dist_acum[i], 3)

    return {
        "archivo": file.filename,
        "total_puntos": len(puntos),
        "distancia_total_m": round(dist_acum[-1], 2),
        "puntos": puntos
    }

@app.post("/kml/parse-postes", tags=["KML"])
async def parsear_kml_postes(file_postes: UploadFile = File(...), file_eje: UploadFile = File(...)):
    content_postes = (await file_postes.read()).decode("utf-8", errors="replace")
    content_eje = (await file_eje.read()).decode("utf-8", errors="replace")

    ruta_coords = parse_kml_coords(content_eje)
    if not ruta_coords:
        raise HTTPException(422, "Eje inválido.")

    kml_clean = content_postes.replace('xmlns="http://www.opengis.net/kml/2.2"', "")
    root = ET.fromstring(kml_clean)
    postes_out = []
    idx = 1
    for pm in root.iter("Placemark"):
        coord_el = pm.find(".//coordinates")
        if coord_el is None or not coord_el.text:
            continue
        
        parts = coord_el.text.strip().split()
        if not parts: continue
        subparts = parts[0].split(",")
        if len(subparts) < 2: continue

        lng_p, lat_p = float(subparts[0]), float(subparts[1])
        x_utm, y_utm = wgs84_to_utm(lat_p, lng_p)
        route_latlng = [(p["lat"], p["lng"]) for p in ruta_coords]
        proj_pt, dist_eje = closest_point_on_polyline(lat_p, lng_p, route_latlng)
        x_proj, y_proj = wgs84_to_utm(proj_pt[0], proj_pt[1])

        postes_out.append({
            "id": idx,
            "wgs84": {"lat": round(lat_p, 8), "lng": round(lng_p, 8)},
            "utm": {"x_este": round(x_utm, 3), "y_norte": round(y_utm, 3)},
            "proyectado_en_eje": {"lat": round(proj_pt[0], 8), "lng": round(proj_pt[1], 8)},
            "distancia_al_eje_m": round(dist_eje, 3),
            "time": 0.0,
        })
        idx += 1

    return {"postes": postes_out}

@app.post("/matriz/generar", tags=["Matriz"])
async def generar_matriz(body: MatrizRequest, file_eje: UploadFile = File(...)):
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)
    postes_validos = [p for p in body.postes if p.time > 0]
    if len(postes_validos) < 2:
        raise HTTPException(400, "Faltan postes calibrados.")

    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)
    return {"puntos": enriquecidos}

@app.post("/exportar/json", tags=["Exportación"])
async def exportar_json(body: MatrizRequest, file_eje: UploadFile = File(...)):
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)
    postes_validos = sorted([p for p in body.postes if p.time > 0], key=lambda p: p.time)
    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)
    
    doc = {
        "metadata": {"generado_en": datetime.utcnow().isoformat()},
        "postes": [p.dict() for p in body.postes],
        "ruta": enriquecidos
    }
    return JSONResponse(content=doc)

@app.post("/exportar/gpx", tags=["Exportación"])
async def exportar_gpx(body: MatrizRequest, file_eje: UploadFile = File(...)):
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)
    postes_validos = sorted([p for p in body.postes if p.time > 0], key=lambda p: p.time)
    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)

    gpx = gpxpy.gpx.GPX()
    track = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(track)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)

    t0 = datetime(2000, 1, 1)
    for pt in enriquecidos:
        segment.points.append(gpxpy.gpx.GPXTrackPoint(
            pt["lat"], pt["lng"], elevation=pt.get("alt", 0),
            time=t0 + timedelta(seconds=pt.get("tiempo_video_s", 0))
        ))

    return StreamingResponse(
        io.BytesIO(gpx.to_xml().encode("utf-8")),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": 'attachment; filename="ruta.gpx"'}
    )