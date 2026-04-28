# StreetViewer API

Backend FastAPI para el visor geoespacial 360° de inspección vial.

## Instalación

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Documentación interactiva

- Swagger UI: http://localhost:8000/docs
- ReDoc:       http://localhost:8000/redoc

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET  | `/` | Info y estado de la API |
| POST | `/coords/utm-to-wgs84` | Convierte UTM 18S → WGS84 |
| POST | `/coords/wgs84-to-utm` | Convierte WGS84 → UTM 18S |
| POST | `/coords/utm-batch` | Conversión masiva UTM → WGS84 |
| POST | `/kml/parse-ruta` | Parsea KML de eje de vía |
| POST | `/kml/parse-postes` | Parsea KML de postes y los proyecta sobre el eje |
| POST | `/matriz/generar` | Genera tabla de interpolación tiempo↔posición |
| POST | `/matriz/utm-a-tiempo` | UTM → tiempo estimado en video |
| POST | `/matriz/tiempo-a-posicion` | Tiempo video → posición geográfica |
| POST | `/exportar/json` | Exporta todos los datos técnicos en JSON |
| POST | `/exportar/gpx` | Exporta ruta + postes en GPX estándar |

## Conexión con el frontend Angular

En `app.component.ts` o un servicio Angular, apunta a:

```typescript
const API = 'http://localhost:8000';


const res = await fetch(`${API}/coords/utm-to-wgs84`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ x: 587395.42, y: 8504073.80 })
});
const data = await res.json();


## Sistema de referencia

- UTM: **EPSG:32718** — Zona 18 Sur, datum WGS84
- Geográfico: **EPSG:4326** — WGS84 lat/lng