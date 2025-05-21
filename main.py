# main.py
# API de Consulta de Leyes Chilenas
# Versión 1.9.0 (Restaurada con /ley_html y correcciones de errores previos)

import logging
# Configurar logging ANTES de cualquier otra cosa para asegurar que se aplique
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)
logger = logging.getLogger(__name__)
logger.info("API v19.HTML_DIAG (Restaurada con /ley_html) INICIANDO...")  # Log distintivo

from fastapi import FastAPI, HTTPException, Query
from typing import Optional, List, Any
import httpx
from bs4 import BeautifulSoup
import re
import json
import os
from pydantic import BaseModel

app = FastAPI(title="API de Consulta de Leyes Chilenas", version="1.9.0")

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

# --- Constantes y configuración ---
MAX_TEXT_LENGTH = 20000
TRUNCATION_MESSAGE_TEXT = "\n\n[Texto truncado por longitud excesiva]"

# Archivo de fallbacks para normalización de numeración transitoria
FALLBACKS_PATH = os.path.join(os.path.dirname(__file__), "fallbacks.json")
try:
    with open(FALLBACKS_PATH, encoding="utf-8") as f:
        FALLBACK_IDS = json.load(f)
except Exception:
    FALLBACK_IDS = {}
    logger.exception(f"Ocurrió un error inesperado al cargar 'fallbacks.json'.")

# Funciones auxiliares
def normalizar_numero(num_str: str) -> str:
    # Implementación de normalización (romanos, palabras, prefijos transitorios...)
    ...

def limpiar_texto_articulo(texto: str) -> str:
    if not texto:
        return ""
    texto_limpio = re.sub(r'[ \t]+', ' ', texto)
    texto_limpio = re.sub(r'\n\s*\n+', '\n', texto_limpio)
    texto_limpio = "\n".join([line.strip() for line in texto_limpio.split('\n')])
    return texto_limpio.strip()

def extraer_referencias_legales_mejorado(texto_articulo: str) -> List[str]:
    # Implementación de extracción de referencias legales
    ...

async def obtener_id_norma_async(numero_ley: str, client: httpx.AsyncClient) -> Optional[str]:
    # Lógica para llamar al endpoint /ley y extraer el idNorma
    ...

async def obtener_xml_ley_async(id_norma: str, client: httpx.AsyncClient) -> Optional[str]:
    # Lógica para descargar y retornar el XML completo de la ley
    ...

async def parsear_xml_a_articulos(xml_content: str) -> List[Articulo]:
    # Implementación de parsing de XML a lista de Articulo
    ...

# --- Endpoints de la API ---
@app.get("/ley", response_model=LeyDetalle, summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley a consultar (ej. '21595')."),
    articulo: Optional[str] = Query(None, description="Número o texto del artículo a consultar (ej. '15', '1 bis', 'Primero Transitorio').")
):
    logger.info(f"INICIO /ley | numero_ley={numero_ley}, articulo={articulo or 'Todos'}")
    async with httpx.AsyncClient() as client:
        id_norma = await obtener_id_norma_async(numero_ley, client)
        if not id_norma:
            raise HTTPException(status_code=404, detail=f"No se encontró ID para la ley {numero_ley}.")
        xml_content = await obtener_xml_ley_async(id_norma, client)
        if not xml_content:
            raise HTTPException(status_code=503, detail=f"No se pudo obtener XML para ley {numero_ley} (ID Norma: {id_norma}).")
        articulos = await parsear_xml_a_articulos(xml_content)
        return LeyDetalle(
            idNorma=id_norma,
            numero_ley=numero_ley,
            titulo="",
            fecha_publicacion="",
            articulos=articulos
        )

@app.get("/ley_html", response_model=ArticuloHTML, summary="Consultar Artículo por idNorma e idParte (HTML)")
async def consultar_articulo_html(
    idNorma: str = Query(..., description="El IdNorma de la ley."),
    idParte: str = Query(..., description="El idParte específico del artículo o sección.")
):
    logger.info(f"Consultando HTML desde bcn.cl para idNorma: {idNorma}, idParte: {idParte}")
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    async with httpx.AsyncClient() as client:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'
            }
            response = await client.get(url, timeout=10, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail=f"Timeout al obtener HTML para idNorma {idNorma}, idParte {idParte}.")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"No se pudo obtener el contenido de {url}. Error: {e}")

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        selectores_posibles = [
            f"#{idParte}",
            f"#p{idParte}",
            f"div#p{idParte}",
            f"article[id='{idParte}']",
            f"div.textoNorma[id*='{idParte}']",
            f"div.textoArticulo[id*='{idParte}']",
            f"div[id='{idParte}']",
            f"div[id^='p{idParte}']",
        ]
        div_contenido = None
        selector_usado = "Ninguno"
        for selector in selectores_posibles:
            div_temp = soup.select_one(selector)
            if div_temp:
                div_contenido = div_temp
                selector_usado = selector
                break

        if not div_contenido:
            raise HTTPException(
                status_code=404,
                detail=f"No se encontró contenido para idParte '{idParte}' con los selectores probados en BCN."
            )

        texto_extraido = div_contenido.get_text(separator="\n", strip=True)
        texto_limpio = limpiar_texto_articulo(texto_extraido)
        if len(texto_limpio) > MAX_TEXT_LENGTH:
            texto_limpio = texto_limpio[:MAX_TEXT_LENGTH].rsplit(" ", 1)[0] + TRUNCATION_MESSAGE_TEXT

        return ArticuloHTML(
            idNorma=idNorma,
            idParte=idParte,
            url=url,
            selector_usado=selector_usado,
            texto_html_extraido=texto_limpio
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception(f"Error al procesar HTML para idNorma {idNorma}, idParte {idParte}.")
        raise HTTPException(status_code=500, detail=str(e))

# Ejemplo para ejecutar con Uvicorn:
# uvicorn main:app --reload --log-level debug
