# main.py
# API de Consulta de Leyes Chilenas
# Versión 2.3.0 – Compatibilidad Plugin GPT y /ley_html

import logging
import os
import json
import re
from typing import Optional, List

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from cachetools import TTLCache

# --- Logging ---
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)
logger.info("API v2.3.0 INICIANDO...")

# --- FastAPI & CORS ---
app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    description="Consulta leyes chilenas (XML) y artículos (HTML) de LeyChile.cl",
    version="2.3.0",
    servers=[{"url": "https://consulta-leyes-chile.onrender.com", "description": "Producción"}],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                # o especifica ["https://chat.openai.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Caché ---
cache_id_norma = TTLCache(maxsize=100, ttl=3600)
cache_xml_ley = TTLCache(maxsize=50, ttl=3600)

# --- Fallback IDs ---
try:
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "fallbacks.json"), encoding="utf-8") as f:
        fallback_ids = json.load(f)
    logger.info("Fallbacks cargados.")
except Exception:
    fallback_ids = {}
    logger.warning("No se cargaron fallbacks.")

# --- Constantes ---
ROMAN_TO_INT = { 'i':1, 'ii':2, 'iii':3, 'iv':4, 'v':5, 'vi':6, 'vii':7, 'viii':8, 'ix':9, 'x':10 }
WORDS_TO_INT = { 'primero':'1','segundo':'2','tercero':'3','cuarto':'4','quinto':'5',
                 'sexto':'6','séptimo':'7','octavo':'8','noveno':'9','décimo':'10' }
MAX_TEXT_LENGTH = 10000
MAX_ARTICULOS_RETURNED = 15
TRUNC_TEXT = "\n\n[... texto truncado ...]"
TRUNC_LIST  = f"Mostrando primeros {MAX_ARTICULOS_RETURNED} artículos. La ley tiene más."

# --- Modelos Pydantic ---
class Articulo(BaseModel):
    articulo_display: str
    articulo_id_interno: str
    texto: str
    referencias_legales: List[str] = []
    id_parte_xml: Optional[str] = None
    nota_busqueda: Optional[str] = None

class LeyDetalle(BaseModel):
    ley: str
    id_norma: str
    articulos_totales_en_respuesta: int
    articulos: List[Articulo]
    total_articulos_originales_en_ley: Optional[int] = None
    nota_truncamiento_lista: Optional[str] = None

class ArticuloHTML(BaseModel):
    idNorma: str
    idParte: str
    url_fuente: str
    selector_usado: str
    texto_html_extraido: str

# --- Helpers de normalización y extracción ---
def normalizar_articulo(num_str: Optional[str]) -> str:
    if not num_str: return "s/n"
    s = num_str.lower().strip()
    s = re.sub(r"^(artículo|articulo)\s*", "", s)
    if s in WORDS_TO_INT: return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:  return str(ROMAN_TO_INT[s])
    nums = re.findall(r"(\d+)([a-z]*)", s)
    comps = [n + t for n, t in nums]
    return "".join(comps) or s

def limpiar_texto(txt: str) -> str:
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return "\n".join(line.strip() for line in txt.splitlines()).strip()

def extraer_referencias(txt: str) -> List[str]:
    patrones = [r"ley\s+N[°º]?\s*\d+", r"art[íi]culo\s+\d+"]
    refs = set()
    for pat in patrones:
        for m in re.finditer(pat, txt, re.IGNORECASE):
            refs.add(m.group(0).strip())
    return sorted(refs)

# --- Llamadas asíncronas a LeyChile ---
import asyncio

async def obtener_id_norma(n_ley: str, client: httpx.AsyncClient) -> Optional[str]:
    clave = n_ley.strip().replace(".", "")
    if (cached:=cache_id_norma.get(clave)): return cached
    if clave in fallback_ids:
        cache_id_norma[clave] = fallback_ids[clave]
        return fallback_ids[clave]
    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{clave}"
    for i in range(3):
        try:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "xml")
            for norma in soup.find_all("Norma"):
                num = norma.find_text("Numero") or ""
                if num.replace(".", "") == clave:
                    idn = norma.find_text("IdNorma") or ""
                    cache_id_norma[clave] = idn
                    return idn
            return None
        except Exception:
            await asyncio.sleep(1)
    return None

async def obtener_xml_ley(idn: str, client: httpx.AsyncClient) -> Optional[bytes]:
    if (cached:=cache_xml_ley.get(idn)): return cached
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={idn}&notaPIE=1"
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        cache_xml_ley[idn] = r.content
        return r.content
    except Exception:
        return None

def extraer_articulos(xml_data: bytes) -> List[Articulo]:
    soup = BeautifulSoup(xml_data, "lxml-xml")
    arts = []
    for ef in soup.find_all("EstructuraFuncional"):
        if "artículo" not in (ef.get("tipoParte") or "").lower(): continue
        idp = ef.get("idParte")
        txt = ef.find_text("Texto") or ""
        m = re.match(r"^\s*([\w\s]+?)[:\-\.\n](.*)$", txt, re.DOTALL)
        disp = m.group(1).strip() if m else txt[:20]
        body = m.group(2).strip() if m else ""
        norm = normalizar_articulo(disp)
        limpio = limpiar_texto(body or disp)
        if len(limpio) > MAX_TEXT_LENGTH:
            limpio = limpio[:MAX_TEXT_LENGTH] + TRUNC_TEXT
        refs = extraer_referencias(limpio)
        arts.append(Articulo(
            articulo_display=disp,
            articulo_id_interno=norm,
            texto=limpio,
            referencias_legales=refs,
            id_parte_xml=idp
        ))
    return arts

# --- Endpoints ---

@app.get("/ley", response_model=LeyDetalle, summary="Consultar ley (XML)")
async def consultar_ley(
    numero_ley: str = Query(..., alias="numeroLey", description="Número de la ley (ej. 21595)"),
    articulo: Optional[str] = Query(None, alias="articulo", description="Artículo (ej. 15, 1 bis)")
):
    async with httpx.AsyncClient() as client:
        idn = await obtener_id_norma(numero_ley, client)
        if not idn:
            raise HTTPException(404, f"No hallé IDNorma para ley {numero_ley}")
        xml = await obtener_xml_ley(idn, client)
        if not xml:
            raise HTTPException(503, "No pude obtener el XML de la ley.")
    lista = extraer_articulos(xml)
    if not lista:
        raise HTTPException(404, "No extraí artículos de la ley.")
    # Si piden un artículo concreto
    if articulo:
        norm = normalizar_articulo(articulo)
        for art in lista:
            if art.articulo_id_interno == norm:
                return LeyDetalle(
                    ley=numero_ley, id_norma=idn,
                    articulos_totales_en_respuesta=1,
                    articulos=[art],
                    total_articulos_originales_en_ley=len(lista)
                )
        raise HTTPException(404, f"No hallé artículo {articulo}")
    # Lista completa (o truncada)
    out = lista if len(lista) <= MAX_ARTICULOS_RETURNED else lista[:MAX_ARTICULOS_RETURNED]
    nota = None
    if len(lista) > MAX_ARTICULOS_RETURNED:
        nota = TRUNC_LIST
    return LeyDetalle(
        ley=numero_ley, id_norma=idn,
        articulos_totales_en_respuesta=len(out),
        articulos=out,
        total_articulos_originales_en_ley=len(lista),
        nota_truncamiento_lista=nota
    )

@app.get("/ley_html", response_model=ArticuloHTML, summary="Consultar artículo (HTML scraping)")
async def consultar_articulo_html(
    id_norma: str = Query(..., alias="idNorma", description="IDNorma de la ley"),
    id_parte: str = Query(..., alias="idParte", description="IdParte del artículo")
):
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={id_norma}&idParte={id_parte}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        if r.status_code != 200:
            raise HTTPException(502, "Error al obtener HTML de la BCN")
        soup = BeautifulSoup(r.text, "html.parser")
        sel = soup.select_one(f"div.textoNorma[id*='{id_parte}'], div[id*='{id_parte}']")
        if not sel:
            return ArticuloHTML(
                idNorma=id_norma, idParte=id_parte,
                url_fuente=url, selector_usado="ninguno",
                texto_html_extraido=f"⚠️ No encontré el artículo. Sigue este enlace:\n{url}"
            )
        txt = limpiar_texto(sel.get_text("\n"))
        if len(txt) > MAX_TEXT_LENGTH:
            txt = txt[:MAX_TEXT_LENGTH] + TRUNC_TEXT
        return ArticuloHTML(
            idNorma=id_norma, idParte=id_parte,
            url_fuente=url, selector_usado=sel.name + ("#"+sel.get("id") if sel.get("id") else ""),
            texto_html_extraido=txt
        )

# --- Healthcheck ---
@app.get("/health", summary="Estado del servicio")
def health():
    return {"status": "ok"}
