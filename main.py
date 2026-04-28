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
    description="API para el visor geoespacial 360° de inspección vial. "
                "Procesa KML, convierte coordenadas UTM/WGS84, interpola "
                "postes en el tiempo y exporta a GPX/JSON.",
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
        raw = coords_el.text.strip()
        for token in raw.split():
            parts = token.split(",")
            if len(parts) >= 2:
                lng, lat = float(parts[0]), float(parts[1])
                alt = float(parts[2]) if len(parts) > 2 else 0.0
                points.append({"index": idx, "lat": lat, "lng": lng, "alt": alt})
                idx += 1
    return points


def interpolate_time_on_route(
    postes: list[Poste],
    route_coords: list[dict]
) -> list[dict]:
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
        _, dist = closest_point_on_polyline(lat_p, lng_p, route_latlng)
       
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


# Verifica que la API está activa y devuelve metadata básica
@app.get("/", tags=["Info"])
def root():
    return {
        "api": "StreetViewer Geo-Espacial",
        "version": "1.0.0",
        "descripcion": "Backend para visor 360° de inspección vial",
        "utm_zona": "18 Sur (EPSG:32718)",
        "datum": "WGS84",
        "endpoints": "/docs"
    }



# Convierte una coordenada UTM zona 18 Sur a latitud/longitud WGS84
@app.post("/coords/utm-to-wgs84", tags=["Coordenadas"])
def convertir_utm_a_wgs84(coord: CoordUTM):
    """
    Recibe Este (X) y Norte (Y) en UTM zona 18 Sur y devuelve lat/lng WGS84.
    Equivalente a la función convertUTMToLatLng() del frontend.
    """
    try:
        lat, lng = utm_to_wgs84(coord.x, coord.y)
        return {
            "entrada": {"x_este": coord.x, "y_norte": coord.y, "zona": "18S", "datum": "WGS84"},
            "salida":  {"lat": lat, "lng": lng},
        }
    except Exception as e:
        raise HTTPException(400, f"Error en conversión: {e}")


# Convierte una coordenada WGS84 (lat/lng) a UTM zona 18 Sur
@app.post("/coords/wgs84-to-utm", tags=["Coordenadas"])
def convertir_wgs84_a_utm(coord: CoordWGS84):
    
    try:
        x, y = wgs84_to_utm(coord.lat, coord.lng)
        return {
            "entrada": {"lat": coord.lat, "lng": coord.lng},
            "salida":  {"x_este": round(x, 3), "y_norte": round(y, 3), "zona": "18S", "datum": "WGS84"},
        }
    except Exception as e:
        raise HTTPException(400, f"Error en conversión: {e}")


# Convierte múltiples coordenadas UTM a WGS84 en lote
@app.post("/coords/utm-batch", tags=["Coordenadas"])
def convertir_utm_lote(coords: list[CoordUTM]):
    
    resultados = []
    for c in coords:
        lat, lng = utm_to_wgs84(c.x, c.y)
        resultados.append({
            "x_este": c.x, "y_norte": c.y,
            "lat": round(lat, 8), "lng": round(lng, 8)
        })
    return {"total": len(resultados), "puntos": resultados}



# Parsea un archivo KML de ruta/eje de vía y devuelve todas sus coordenadas
@app.post("/kml/parse-ruta", tags=["KML"])
async def parsear_kml_ruta(file: UploadFile = File(...)):
   
    content = await file.read()
    try:
        kml_text = content.decode("utf-8")
    except UnicodeDecodeError:
        kml_text = content.decode("latin-1")

    puntos = parse_kml_coords(kml_text)
    if not puntos:
        raise HTTPException(422, "No se encontraron coordenadas en el KML.")

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
        "distancia_total_km": round(dist_acum[-1] / 1000, 4),
        "bbox": {
            "lat_min": min(p["lat"] for p in puntos),
            "lat_max": max(p["lat"] for p in puntos),
            "lng_min": min(p["lng"] for p in puntos),
            "lng_max": max(p["lng"] for p in puntos),
        },
        "puntos": puntos
    }


# Parsea un KML de postes de referencia y los proyecta sobre el eje de vía
@app.post("/kml/parse-postes", tags=["KML"])
async def parsear_kml_postes(
    file_postes: UploadFile = File(...),
    file_eje:    UploadFile = File(...),
):
    content_postes = (await file_postes.read()).decode("utf-8", errors="replace")
    content_eje    = (await file_eje.read()).decode("utf-8", errors="replace")

    ruta_coords = parse_kml_coords(content_eje)
    if not ruta_coords:
        raise HTTPException(422, "El KML del eje no tiene coordenadas válidas.")
    kml_clean = content_postes.replace('xmlns="http://www.opengis.net/kml/2.2"', "")
    root = ET.fromstring(kml_clean)
    postes_out = []
    idx = 1
    for pm in root.iter("Placemark"):
        name_el = pm.find("name")
        nombre = name_el.text.strip() if name_el is not None else f"Poste {idx}"
        coord_el = pm.find(".//coordinates")
        if coord_el is None:
            continue
        parts = coord_el.text.strip().split(",")
        if len(parts) < 2:
            continue
        lng_p, lat_p = float(parts[0]), float(parts[1])
        alt_p = float(parts[2]) if len(parts) > 2 else 0.0
        x_utm, y_utm = wgs84_to_utm(lat_p, lng_p)
        route_latlng = [(p["lat"], p["lng"]) for p in ruta_coords]
        proj_pt, dist_eje = closest_point_on_polyline(lat_p, lng_p, route_latlng)
        x_proj, y_proj = wgs84_to_utm(proj_pt[0], proj_pt[1])

        postes_out.append({
            "id": idx,
            "nombre": nombre,
            "wgs84": {"lat": round(lat_p, 8), "lng": round(lng_p, 8), "alt": alt_p},
            "utm": {"x_este": round(x_utm, 3), "y_norte": round(y_utm, 3), "zona": "18S"},
            "proyectado_en_eje": {
                "lat": round(proj_pt[0], 8),
                "lng": round(proj_pt[1], 8),
                "x_este": round(x_proj, 3),
                "y_norte": round(y_proj, 3),
            },
            "distancia_al_eje_m": round(dist_eje, 3),
            "time": 0.0,
        })
        idx += 1

    return {
        "archivo_postes": file_postes.filename,
        "total_postes": len(postes_out),
        "postes": postes_out
    }


# Genera la matriz de interpolación tiempo-posición para toda la ruta
@app.post("/matriz/generar", tags=["Matriz"])
async def generar_matriz(
    body: MatrizRequest,
    file_eje: UploadFile = File(...),
):
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)
    if not ruta_coords:
        raise HTTPException(422, "El KML del eje no tiene coordenadas válidas.")

    postes_validos = [p for p in body.postes if p.time > 0]
    if len(postes_validos) < 2:
        raise HTTPException(400, "Se necesitan al menos 2 postes con tiempo asignado para interpolar.")

    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)

    return {
        "total_puntos_ruta": len(enriquecidos),
        "postes_usados": len(postes_validos),
        "rango_tiempo_s": {
            "inicio": enriquecidos[0]["tiempo_video_s"],
            "fin": enriquecidos[-1]["tiempo_video_s"],
        },
        "puntos": enriquecidos
    }


# Dado un tiempo del video, devuelve la posición estimada en la ruta
@app.post("/matriz/tiempo-a-posicion", tags=["Matriz"])
async def tiempo_a_posicion(
    tiempo_s: float = Query(..., description="Tiempo del video en segundos"),
    body: MatrizRequest = ...,
    file_eje: UploadFile = File(...),
):
    """
    Función inversa a la búsqueda UTM.
    Dado un tiempo del video, interpola y devuelve la posición geográfica.
    Útil para mover el marcador en el mapa automáticamente al reproducir el video.
    """
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)
    postes_validos = [p for p in body.postes if p.time > 0]
    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords)

    best = min(enriquecidos, key=lambda p: abs(p["tiempo_video_s"] - tiempo_s))
    lat, lng = best["lat"], best["lng"]
    x, y = wgs84_to_utm(lat, lng)

    return {
        "tiempo_buscado_s": tiempo_s,
        "tiempo_interpolado_s": best["tiempo_video_s"],
        "wgs84": {"lat": lat, "lng": lng},
        "utm": {"x_este": round(x, 3), "y_norte": round(y, 3), "zona": "18S"},
        "indice_en_ruta": best["index"],
    }


# Busca el tiempo en el video correspondiente a unas coordenadas UTM dadas
@app.post("/matriz/utm-a-tiempo", tags=["Matriz"])
def utm_a_tiempo(body: BuscarUTMRequest):
    
    postes_validos = sorted([p for p in body.postes if p.time > 0], key=lambda p: p.time)
    if len(postes_validos) < 2:
        raise HTTPException(400, "Se necesitan al menos 2 postes calibrados.")

    lat_target, lng_target = utm_to_wgs84(body.x, body.y)

    def dist_poste(p: Poste):
        lat_p, lng_p = utm_to_wgs84(p.x, p.y)
        return haversine_m(lat_target, lng_target, lat_p, lng_p)

    dists = [(p, dist_poste(p)) for p in postes_validos]
    dists.sort(key=lambda d: d[1])

    p0, d0 = dists[0]
    p1, d1 = dists[1] if len(dists) > 1 else dists[0]
    total = d0 + d1
    tiempo_est = (p0.time * d1 + p1.time * d0) / total if total > 0 else p0.time

    return {
        "punto_utm": {"x_este": body.x, "y_norte": body.y},
        "punto_wgs84": {"lat": round(lat_target, 8), "lng": round(lng_target, 8)},
        "tiempo_estimado_s": round(tiempo_est, 4),
        "poste_mas_cercano": {"id": p0.id, "distancia_m": round(d0, 2), "time_s": p0.time},
        "segundo_poste":     {"id": p1.id, "distancia_m": round(d1, 2), "time_s": p1.time},
    }


# Exporta toda la información técnica del proyecto en formato JSON estructurado
@app.post("/exportar/json", tags=["Exportación"])
async def exportar_json(
    body: MatrizRequest,
    file_eje: UploadFile = File(...),
):
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)

    postes_validos = sorted([p for p in body.postes if p.time > 0], key=lambda p: p.time)
    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords) if len(postes_validos) >= 2 else [
        {**p, "tiempo_video_s": 0.0} for p in ruta_coords
    ]
    cum_dist = [0.0]
    for i in range(1, len(ruta_coords)):
        d = haversine_m(ruta_coords[i-1]["lat"], ruta_coords[i-1]["lng"],
                        ruta_coords[i]["lat"],   ruta_coords[i]["lng"])
        cum_dist.append(cum_dist[-1] + d)

    dist_total_m = cum_dist[-1]
    dur_total_s  = (postes_validos[-1].time - postes_validos[0].time) if len(postes_validos) >= 2 else 0
    vel_media    = (dist_total_m / dur_total_s * 3.6) if dur_total_s > 0 else 0
    ruta_export = []
    for i, pt in enumerate(enriquecidos):
        x_utm, y_utm = wgs84_to_utm(pt["lat"], pt["lng"])
        ruta_export.append({
            "index": pt["index"],
            "wgs84": {"lat": round(pt["lat"], 8), "lng": round(pt["lng"], 8), "alt": pt.get("alt", 0)},
            "utm":   {"x_este": round(x_utm, 3), "y_norte": round(y_utm, 3), "zona": "18S"},
            "dist_acumulada_m": round(cum_dist[i], 3),
            "tiempo_video_s": pt.get("tiempo_video_s", 0.0),
        })
    postes_export = []
    for i, p in enumerate(body.postes):
        lat_p, lng_p = utm_to_wgs84(p.x, p.y)
        postes_export.append({
            "id": p.id,
            "descripcion": p.descripcion or f"Poste #{p.id}",
            "wgs84": {"lat": round(lat_p, 8), "lng": round(lng_p, 8)},
            "utm": {"x_este": p.x, "y_norte": p.y, "zona": "18S"},
            "tiempo_video_s": p.time,
            "calibrado": p.time > 0,
        })

    doc = {
        "metadata": {
            "generado_en": datetime.utcnow().isoformat() + "Z",
            "sistema_referencia": {
                "utm": "EPSG:32718 — UTM zona 18 Sur",
                "geografico": "EPSG:4326 — WGS84",
                "hemisferio": "Sur",
                "zona": 18,
            },
            "archivo_eje": file_eje.filename,
        },
        "estadisticas": {
            "total_puntos_ruta": len(ruta_export),
            "total_postes": len(body.postes),
            "postes_calibrados": len(postes_validos),
            "longitud_total_m": round(dist_total_m, 2),
            "longitud_total_km": round(dist_total_m / 1000, 4),
            "duracion_video_s": round(dur_total_s, 2),
            "velocidad_media_kmh": round(vel_media, 2),
            "bbox": {
                "lat_min": min(p["lat"] for p in ruta_coords),
                "lat_max": max(p["lat"] for p in ruta_coords),
                "lng_min": min(p["lng"] for p in ruta_coords),
                "lng_max": max(p["lng"] for p in ruta_coords),
            },
        },
        "postes": postes_export,
        "ruta": ruta_export,
    }

    return JSONResponse(content=doc, media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=streetviewer_export.json"})


# Exporta la ruta y los postes en formato GPX estándar
@app.post("/exportar/gpx", tags=["Exportación"])
async def exportar_gpx(
    body: MatrizRequest,
    file_eje: UploadFile = File(...),
    nombre_ruta: str = Query("StreetViewer Route", description="Nombre que tendrá la ruta en el GPX"),
):
    content = (await file_eje.read()).decode("utf-8", errors="replace")
    ruta_coords = parse_kml_coords(content)

    postes_validos = sorted([p for p in body.postes if p.time > 0], key=lambda p: p.time)
    enriquecidos = interpolate_time_on_route(postes_validos, ruta_coords) if len(postes_validos) >= 2 else [
        {**p, "tiempo_video_s": 0.0} for p in ruta_coords
    ]

    gpx = gpxpy.gpx.GPX()
    gpx.name        = nombre_ruta
    gpx.description = "Exportado desde StreetViewer Geo-Espacial — UTM 18S / WGS84"
    gpx.author_name = "StreetViewer API"
    gpx.time        = datetime.utcnow()

    track = gpxpy.gpx.GPXTrack(name=nombre_ruta)
    track.comment = f"Ruta con {len(enriquecidos)} puntos interpolados"
    gpx.tracks.append(track)

    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)

    t0 = datetime(2000, 1, 1, 0, 0, 0)   
    for pt in enriquecidos:
        tp = gpxpy.gpx.GPXTrackPoint(
            latitude  = pt["lat"],
            longitude = pt["lng"],
            elevation = pt.get("alt", 0),
            time      = t0 + timedelta(seconds=pt.get("tiempo_video_s", 0)),
        )
        tp.comment = f"T_video={pt.get('tiempo_video_s', 0):.4f}s idx={pt['index']}"
        segment.points.append(tp)

    for poste in body.postes:
        lat_p, lng_p = utm_to_wgs84(poste.x, poste.y)
        wpt = gpxpy.gpx.GPXWaypoint(
            latitude    = lat_p,
            longitude   = lng_p,
            name        = f"Poste #{poste.id}",
            description = (
                f"ID: {poste.id} | "
                f"UTM Este: {poste.x} | UTM Norte: {poste.y} | "
                f"Tiempo video: {poste.time}s | "
                f"Calibrado: {'Sí' if poste.time > 0 else 'No'}"
            ),
            time = t0 + timedelta(seconds=poste.time) if poste.time > 0 else None,
        )
        gpx.waypoints.append(wpt)

    gpx_xml = gpx.to_xml()
    return StreamingResponse(
        io.BytesIO(gpx_xml.encode("utf-8")),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="streetviewer_ruta.gpx"'},
    )