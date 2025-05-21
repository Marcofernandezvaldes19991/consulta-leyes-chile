# main.py
# API de Consulta de Leyes Chilenas
# Versión 1.9.0 (Con sección servers para OpenAPI)

import logging
# Configurar logging ANTES de cualquier otra cosa
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)
logger = logging.getLogger(__name__)
logger.info("API v19.HTML_DIAG INICIANDO...")

from fastapi import FastAPI, HTTPException, Query
from typing import Optional, List
import httpx
from bs4 import BeautifulSoup
import re
import json
import os
from pydantic import BaseModel

# ------------------------------
# Aquí agregamos servers para OpenAPI
# ------------------------------
app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    version="1.9.0",
    servers=[{"url": "https://consulta-leyes-chile.onrender.com", "description": "Producción"}]
)

# --- Modelos de datos ---
class ArticuloHTML(BaseModel):
    idNorma: str
    idParte: str
    url: str
    selector_usado: str
    texto_html_extraido: str

class ReferenciaLegal(BaseModel):
    texto: str
    referencia: str

class Articulo(BaseModel):
    articulo_display: str
    articulo_id_interno: str
    texto: str
    referencias_legales: List[str]
    id_parte_xml: str

class LeyDetalle(BaseModel):
    idNorma: str
    numero_ley: str
    titulo: str
    fecha_publicacion: str
    articulos: List[Articulo]

# --- Configuración y utilidades ---
MAX_TEXT_LENGTH = 20000
TRUNCATION_MESSAGE_TEXT = "\n\n[Texto truncado por longitud excesiva]"

FALLBACKS_PATH = os.path.join(os.path.dirname(__file__), "fallbacks.json")
try:
    with open(FALLBACKS_PATH, encoding="utf-8") as f:
        FALLBACK_IDS = json.load(f)
except Exception:
    FALLBACK_IDS = {}
    logger.exception("Error al cargar 'fallbacks.json'.")

def limpiar_texto_articulo(texto: str) -> str:
    if not texto:
        return ""
    txt = re.sub(r'[ \t]+', ' ', texto)
    txt = re.sub(r'\n\s*\n+', '\n', txt)
    return "\n".join(line.strip() for line in txt.split('\n')).strip()

# Placeholder para funciones que ya tenías implementadas...
async def obtener_id_norma_async(numero_ley: str, client: httpx.AsyncClient) -> Optional[str]:
    ...

async def obtener_xml_ley_async(id_norma: str, client: httpx.AsyncClient) -> Optional[str]:
    ...

async def parsear_xml_a_articulos(xml_content: str) -> List[Articulo]:
    ...

# --- Endpoints ---
@app.get("/ley", response_model=LeyDetalle, summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley (ej. '21595')."),
    articulo: Optional[str] = Query(None, description="Artículo a consultar (ej. '15', 'Primero Transitorio').")
):
    logger.info(f"INICIO /ley | ley={numero_ley}, art={articulo or 'Todos'}")
    async with httpx.AsyncClient() as client:
        id_norma = await obtener_id_norma_async(numero_ley, client)
        if not id_norma:
            raise HTTPException(404, f"No se encontró ID para ley {numero_ley}.")
        xml = await obtener_xml_ley_async(id_norma, client)
        if not xml:
            raise HTTPException(503, f"No se pudo obtener XML para ley {numero_ley}.")
        arts = await parsear_xml_a_articulos(xml)
        return LeyDetalle(
            idNorma=id_norma,
            numero_ley=numero_ley,
            titulo="",
            fecha_publicacion="",
            articulos=arts
        )

@app.get("/ley_html", response_model=ArticuloHTML, summary="Consultar Artículo HTML por idNorma e idParte")
async def consultar_articulo_html(
    idNorma: str = Query(..., description="IdNorma de la ley."),
    idParte: str = Query(..., description="idParte del artículo o sección.")
):
    logger.info(f"GET /ley_html | idNorma={idNorma}, idParte={idParte}")
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0"
            })
            resp.raise_for_status()
        except httpx.HTTPError as e:
            code = 504 if isinstance(e, httpx.TimeoutException) else 502
            raise HTTPException(code, f"Error al obtener HTML: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    selectores = [
        f"#{idParte}",
        f"#p{idParte}",
        f"div#p{idParte}",
        f"article[id='{idParte}']",
        f"div.textoNorma[id*='{idParte}']",
        f"div.textoArticulo[id*='{idParte}']",
        f"div[id='{idParte}']",
        f"div[id^='p{idParte}']",
    ]
    nodo = None
    usado = ""
    for sel in selectores:
        tmp = soup.select_one(sel)
        if tmp:
            nodo = tmp
            usado = sel
            break

    if not nodo:
        raise HTTPException(404, f"No se encontró contenido para idParte '{idParte}'.")

    texto = limpiar_texto_articulo(nodo.get_text("\n", strip=True))
    if len(texto) > MAX_TEXT_LENGTH:
        texto = texto[:MAX_TEXT_LENGTH].rsplit(" ",1)[0] + TRUNCATION_MESSAGE_TEXT

    return ArticuloHTML(
        idNorma=idNorma,
        idParte=idParte,
        url=url,
        selector_usado=usado,
        texto_html_extraido=texto
    )

# Para ejecutar localmente:
# uvicorn main:app --reload --host 0.0.0.0 --port 8000
