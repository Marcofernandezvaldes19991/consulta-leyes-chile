# main.py

import logging
import os
import re
import json
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
import xml.etree.ElementTree as ET
from difflib import get_close_matches

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True
)
logger = logging.getLogger("leyes-chile")
logger.info("Iniciando API Consulta Leyes Chilenas")

app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    description="Consulta profesional de artículos de leyes chilenas vía XML/HTML Ley Chile y web BCN. Potenciada para GPT, buscadores y legal tech.",
    version="2.3.0",
    contact={"name": "Tu Nombre", "email": "tucorreo@dominio.cl"},
    terms_of_service="https://www.leychile.cl/portal/pagina/privacidad",
    servers=[{"url": "https://consulta-leyes-chile.onrender.com"}]
)

cache_id_norma = TTLCache(maxsize=200, ttl=3600)
cache_xml_ley = TTLCache(maxsize=50, ttl=3600)

MAX_TEXT_LENGTH = 10000
MAX_ARTICULOS_RETURNED = 15
ROMAN_TO_INT = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15, 'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20,
}
WORDS_TO_INT = {
    'primero': '1', 'segundo': '2', 'tercero': '3', 'cuarto': '4', 'quinto': '5',
    'sexto': '6', 'séptimo': '7', 'octavo': '8', 'noveno': '9', 'décimo': '10',
    'undécimo': '11', 'duodécimo': '12', 'decimotercero': '13', 'decimocuarto': '14', 'decimoquinto': '15',
    'vigésimo': '20', 'trigésimo': '30', 'cuadragésimo': '40', 'quincuagésimo': '50',
    'único': 'unico', 'unico': 'unico', 'final': 'final'
}

# -- Modelos --
class Articulo(BaseModel):
    articulo_display: str
    articulo_id_interno: str
    texto: str
    referencias_legales: List[str] = Field(default_factory=list)
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

# --- Utilidades ---
def limpiar_texto(texto: str) -> str:
    texto = re.sub(r'[ \t]+', ' ', texto)
    texto = re.sub(r'\n\s*\n+', '\n', texto)
    return "\n".join(line.strip() for line in texto.split('\n')).strip()

def normalizar_numero_articulo(num_str: Optional[str]) -> str:
    if not num_str:
        return "s/n"
    s = str(num_str).lower().strip()
    s = re.sub(r"^(artículo|articulo|art\.?|nro\.?|n[º°]|disposición|disp\.?)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.rstrip('.-').strip()
    if s in WORDS_TO_INT:
        return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:
        return str(ROMAN_TO_INT[s])
    prefijo_transitorio = ""
    transitorio_match = re.match(r"^(transitorio|trans\.?|t)\s*(.*)", s, flags=re.IGNORECASE)
    if transitorio_match:
        prefijo_transitorio = "t"
        s = transitorio_match.group(2).strip().rstrip('.-').strip()
    for palabra, digito in WORDS_TO_INT.items():
        if re.search(r'\b' + re.escape(palabra) + r'\b', s):
            s = re.sub(r'\b' + re.escape(palabra) + r'\b', digito, s)
    s = re.sub(r"[º°ª\.,]", "", s)
    s = s.strip().rstrip('-').strip()
    partes_numericas = re.findall(r"(\d+)\s*([a-zA-Z]*)", s)
    componentes_normalizados = []
    texto_restante = s
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part:
            if letra_part in ["bis", "ter", "quater"] or (len(letra_part) == 1 and letra_part.isalpha()):
                componente += letra_part
        componentes_normalizados.append(componente)
        texto_restante = texto_restante.replace(num_part, "", 1).replace(letra_part, "", 1).strip()
    if not componentes_normalizados and texto_restante:
        posible_romano = texto_restante.replace(" ", "")
        if posible_romano in ROMAN_TO_INT:
            componentes_normalizados.append(str(ROMAN_TO_INT[posible_romano]))
            texto_restante = ""
    if not componentes_normalizados and texto_restante:
        componentes_normalizados.append(texto_restante.replace(" ", ""))
    id_final = prefijo_transitorio + "".join(componentes_normalizados)
    if not id_final:
        id_final = re.sub(r"[^a-z0-9]", "", s.replace(" ", "")).strip()
        if not id_final:
            return "s/n_error_normalizacion"
    return id_final

def extraer_referencias_legales(texto: str) -> List[str]:
    refs = re.findall(r'\b[Ll]ey\s+\d{4,6}|\b[Dd][Ss]?\s*N[°º]?\s*\d{3,6}|\b[Dd][Ff][Ll]?\s*N[°º]?\s*\d{1,6}', texto)
    return list(set([limpiar_texto(r) for r in refs]))

async def obtener_id_norma(numero_ley: str, client: Optional[httpx.AsyncClient]=None) -> str:
    if numero_ley in cache_id_norma:
        return cache_id_norma[numero_ley]
    url_busqueda = f"https://www.leychile.cl/Navegar?idLey={numero_ley}"
    _client = client or httpx.AsyncClient()
    r = await _client.get(url_busqueda, follow_redirects=True)
    m = re.search(r"Navegar\?idNorma=(\d+)", r.text)
    if not m:
        raise HTTPException(404, f"No se pudo encontrar el IdNorma para la ley {numero_ley}")
    id_norma = m.group(1)
    cache_id_norma[numero_ley] = id_norma
    return id_norma

async def obtener_xml_ley(id_norma: str, client: Optional[httpx.AsyncClient]=None) -> str:
    if id_norma in cache_xml_ley:
        return cache_xml_ley[id_norma]
    url_xml = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}"
    _client = client or httpx.AsyncClient()
    resp = await _client.get(url_xml)
    if resp.status_code != 200:
        raise HTTPException(503, f"No se pudo obtener XML para la ley con idNorma={id_norma}")
    xml = resp.text
    cache_xml_ley[id_norma] = xml
    return xml

def extraer_articulos(xml: str) -> List[Articulo]:
    ns = {'n': 'http://www.leychile.cl/esquemas'}
    root = ET.fromstring(xml)
    articulos = []
    for art in root.findall('.//n:EstructuraFuncional[@tipoParte="Artículo"]', ns):
        id_parte = art.get('idParte')
        numero = art.findtext('n:Metadatos/n:TituloParte', default="", namespaces=ns)
        display = f"Artículo {numero or id_parte}"
        texto = art.findtext('n:Texto', default="", namespaces=ns)
        texto = limpiar_texto(texto)
        if len(texto) > MAX_TEXT_LENGTH:
            texto = texto[:MAX_TEXT_LENGTH].rsplit(" ", 1)[0] + "\n[Texto truncado]"
        articulos.append(Articulo(
            articulo_display=display,
            articulo_id_interno=normalizar_numero_articulo(numero),
            texto=texto,
            referencias_legales=extraer_referencias_legales(texto),
            id_parte_xml=id_parte
        ))
    return articulos

def buscar_articulo_avanzado(articulos: List[Articulo], articulo: str) -> List[Articulo]:
    art_normalizado = normalizar_numero_articulo(articulo)
    # 1. Por id interno
    resultados = [a for a in articulos if a.articulo_id_interno == art_normalizado]
    if resultados:
        return resultados
    # 2. Por display exacto
    posibles = [a for a in articulos if articulo.strip().lower() == a.articulo_display.strip().lower()]
    if posibles:
        for r in posibles: r.nota_busqueda = "Búsqueda por display exacto"
        return posibles
    # 3. Por id_parte_xml
    posibles = [a for a in articulos if articulo == str(a.id_parte_xml)]
    if posibles:
        for r in posibles: r.nota_busqueda = "Búsqueda por id_parte_xml"
        return posibles
    # 4. Búsqueda textual difusa
    posibles = [
        a for a in articulos
        if articulo.lower() in a.texto.lower() or articulo.lower() in a.articulo_display.lower()
    ]
    for r in posibles:
        r.nota_busqueda = "Búsqueda textual (no coincidió identificador exacto)"
    if posibles:
        return posibles
    # Sugerencias inteligentes
    ids_internos = [a.articulo_id_interno for a in articulos]
    displays = [a.articulo_display for a in articulos]
    match_cercanos = get_close_matches(art_normalizado, ids_internos, n=5, cutoff=0.6)
    raise HTTPException(status_code=404, detail={
        "error": f"Artículo '{articulo}' (buscado como '{art_normalizado}') no encontrado.",
        "sugerencia": f"Quizá quiso decir: {match_cercanos}",
        "ids_disponibles": ids_internos[:15],
        "displays_disponibles": displays[:15]
    })

# --- ENDPOINTS ---

@app.get("/ley", response_model=LeyDetalle, summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley a consultar (ej. '21595')."),
    articulo: Optional[str] = Query(None, description="Número o identificador del artículo a consultar.")
):
    """
    Consulta una ley chilena por número. Si se indica el artículo, retorna solo ese artículo.
    Si el artículo no existe, entrega sugerencias.
    Si la ley es extensa, la respuesta se trunca (máx 15 artículos).
    """
    async with httpx.AsyncClient() as client:
        id_norma = await obtener_id_norma(numero_ley, client)
        xml = await obtener_xml_ley(id_norma, client)
    articulos = extraer_articulos(xml)
    total_original = len(articulos)
    nota_trunc = None
    articulos_respuesta = articulos

    if articulo:
        articulos_respuesta = buscar_articulo_avanzado(articulos, articulo)
    elif len(articulos) > MAX_ARTICULOS_RETURNED:
        articulos_respuesta = articulos[:MAX_ARTICULOS_RETURNED]
        nota_trunc = f"Mostrando los primeros {MAX_ARTICULOS_RETURNED} artículos. Para un artículo específico, especifique el número."

    return LeyDetalle(
        ley=numero_ley,
        id_norma=id_norma,
        articulos_totales_en_respuesta=len(articulos_respuesta),
        articulos=articulos_respuesta,
        total_articulos_originales_en_ley=total_original if not articulo else None,
        nota_truncamiento_lista=nota_trunc
    )

@app.get("/ley_articulos_ids", summary="Lista de IDs y displays de artículos de una ley")
async def listar_articulos_ids(
    numero_ley: str = Query(..., description="Número de la ley")
):
    """Devuelve lista de todos los identificadores de artículos de la ley."""
    async with httpx.AsyncClient() as client:
        id_norma = await obtener_id_norma(numero_ley, client)
        xml = await obtener_xml_ley(id_norma, client)
    articulos = extraer_articulos(xml)
    return [
        {"display": art.articulo_display, "id_interno": art.articulo_id_interno, "id_parte_xml": art.id_parte_xml}
        for art in articulos
    ]

@app.get("/ley_articulo_idparte", response_model=Articulo, summary="Consultar artículo por idNorma e idParte")
async def consultar_articulo_por_idparte(
    numero_ley: str = Query(...),
    idParte: str = Query(...)
):
    """Consulta directo por idParte, útil para integrar con BCN."""
    async with httpx.AsyncClient() as client:
        id_norma = await obtener_id_norma(numero_ley, client)
        xml = await obtener_xml_ley(id_norma, client)
    articulos = extraer_articulos(xml)
    for art in articulos:
        if art.id_parte_xml == idParte:
            return art
    raise HTTPException(status_code=404, detail="No se encontró artículo con ese idParte en la ley indicada.")

@app.get("/buscar_global", summary="Buscar frase en artículos de las leyes más recientes")
async def buscar_global(
    consulta: str = Query(..., description="Palabra o frase a buscar"),
    max_leyes: int = Query(10, ge=1, le=50, description="Cantidad máxima de leyes recientes donde buscar."),
    max_resultados: int = Query(10, ge=1, le=50, description="Máximo de resultados a retornar.")
):
    """Busca la frase indicada en los textos de artículos de las últimas N leyes publicadas."""
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=3&cantidad={max_leyes}"
    resultados = []
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        root = ET.fromstring(resp.text)
        for norma in root.findall(".//Norma"):
            num_ley = norma.findtext("Identificadores/Numero", default="")
            if not num_ley:
                continue
            try:
                id_norma = await obtener_id_norma(num_ley, client)
                xml = await obtener_xml_ley(id_norma, client)
                articulos = extraer_articulos(xml)
                for art in articulos:
                    if consulta.lower() in art.texto.lower():
                        resultados.append({
                            "ley": num_ley,
                            "id_norma": id_norma,
                            "articulo_display": art.articulo_display,
                            "id_parte_xml": art.id_parte_xml,
                            "fragmento": art.texto[:200] + "..." if len(art.texto) > 200 else art.texto
                        })
                        if len(resultados) >= max_resultados:
                            return resultados
            except Exception as e:
                logger.warning(f"Fallo al buscar en ley {num_ley}: {e}")
                continue
    return resultados

@app.get("/ley_html", response_model=ArticuloHTML, summary="Extraer texto HTML de un artículo específico")
async def consultar_articulo_html(
    idNorma: str = Query(..., description="Identificador único de la norma (idNorma)."),
    idParte: str = Query(..., description="Identificador de la parte/artículo (idParte).")
):
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            raise HTTPException(503, f"No se pudo obtener HTML de BCN.")
    soup = BeautifulSoup(resp.text, "html.parser")
    selectores = [
        f"#{idParte}",
        f"#p{idParte}",
        f"div#p{idParte}",
        f"article[id='{idParte}']",
        f"div.textoNorma[id*='{idParte}']"
    ]
    extraido = ""
    selector_usado = None
    for sel in selectores:
        elemento = soup.select_one(sel)
        if elemento:
            extraido = elemento.get_text(separator="\n", strip=True)
            selector_usado = sel
            break
    if not extraido:
        raise HTTPException(404, detail=f"No se pudo extraer el texto HTML para idNorma={idNorma}, idParte={idParte}")
    return ArticuloHTML(
        idNorma=idNorma,
        idParte=idParte,
        url_fuente=url,
        selector_usado=selector_usado or "desconocido",
        texto_html_extraido=extraido
    )

# --- Manejo de errores globales ---

@app.exception_handler(Exception)
async def exception_handler(request, exc):
    logger.error(f"Error global: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Error interno en el servidor", "detalles": str(exc)}
    )
