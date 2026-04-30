# StreetViewer API

API FastAPI para visor geoespacial 360°.

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET  | `/` | Estado de la API |
| POST | `/coords/utm-to-wgs84` | UTM 18S → WGS84 |
| POST | `/coords/wgs84-to-utm` | WGS84 → UTM 18S |
| POST | `/coords/utm-batch` | Conversión masiva UTM → WGS84 |
| POST | `/kml/parse-ruta` | Parsea KML de eje de vía |
| POST | `/kml/parse-postes` | Parsea KML de postes y los proyecta sobre el eje |
| POST | `/matriz/generar` | Tabla de interpolación tiempo↔posición |
| POST | `/matriz/utm-a-tiempo` | UTM → tiempo estimado |
| POST | `/matriz/tiempo-a-posicion` | Tiempo → posición geográfica |
| POST | `/exportar/json` | Exporta datos en JSON |
| POST | `/exportar/gpx` | Exporta postes en GPX (waypoints + línea por id/track) |
| POST | `/exportar/csv-postes` | Exporta postes en CSV |

Para `/exportar/csv-postes` y `/exportar/gpx`, puedes enviar `video_datetime` o `video_fecha` + `video_hora` para fijar los timestamps.


