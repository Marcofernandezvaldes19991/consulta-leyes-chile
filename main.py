# main.py
# API de Consulta de Leyes Chilenas - Versión 2.2.0 "potente"
# Compatible OpenAPI y GPT personalizados

import logging
import os
import re
import json
from typing import Optional, List, Dict
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
import xml.etree.ElementTree as ET

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True
)
logger = logging.getLogger("leyes-chile")
logger.info("Iniciando API Consulta Leyes Chilenas v2.2.0")

app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    description="Permite consultar artículos de leyes chilenas obteniendo datos desde LeyChile.cl (XML y HTML).",
    version="2.2.0",
    servers=[
        {"url": "https://consulta-leyes-chile.onrender.com", "description": "Servidor de Producción en Render"}
    ]
)

cache_id_norma = TTLCache(maxsize=200, ttl=3600)
cache_xml_ley = TTLCache(maxsize=50, ttl=3600)

MAX_TEXT_LENGTH = 10000
MAX_ARTICULOS_RETURNED = 15
TRUNCATION_MESSAGE_TEXT = (
    "\n\n[... texto completo del artículo truncado por exceder el límite de longitud para esta API. Consulte la fuente original para el texto íntegro ...]"
)
TRUNCATION_MESSAGE_LIST = (
    f"Mostrando los primeros {MAX_ARTICULOS_RETURNED} artículos. "
    "La ley contiene más artículos. Para ver un artículo específico, por favor especifíquelo en la consulta."
)
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

# --- Fallbacks por archivo externo (opcional) ---
try:
    fallback_ids = {}
    fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fallbacks.json")
    if os.path.exists(fallback_path):
        with open(fallback_path, encoding="utf-8") as f:
            fallback_ids = json.load(f)
        logger.info("Fallbacks.json de IDs cargado correctamente.")
except Exception as e:
    logger.warning(f"No se pudo cargar fallbacks.json: {e}")

# -----------------------------
# Modelos de datos Pydantic
# -----------------------------
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

# -----------------------------
# Funciones utilitarias robustas
# -----------------------------
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

# -----------------------------
# Obtención de IdNorma
# -----------------------------
async def obtener_id_norma(numero_ley: str) -> str:
    if numero_ley in cache_id_norma:
        return cache_id_norma[numero_ley]
    if numero_ley in fallback_ids:
        id_norma = fallback_ids[numero_ley]
        cache_id_norma[numero_ley] = id_norma
        return id_norma
    url_busqueda = f"https://www.leychile.cl/Navegar?idLey={numero_ley}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url_busqueda, follow_redirects=True)
        m = re.search(r"Navegar\?idNorma=(\d+)", r.text)
        if not m:
            raise HTTPException(404, f"No se pudo encontrar el IdNorma para la ley {numero_ley}")
        id_norma = m.group(1)
        cache_id_norma[numero_ley] = id_norma
        return id_norma

# -----------------------------
# Parseo de XML y búsqueda de artículos
# -----------------------------
def parsear_articulos(xml: str) -> List[Articulo]:
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
            texto = texto[:MAX_TEXT_LENGTH].rsplit(" ", 1)[0] + TRUNCATION_MESSAGE_TEXT
        articulos.append(Articulo(
            articulo_display=display,
            articulo_id_interno=normalizar_numero_articulo(numero),
            texto=texto,
            referencias_legales=extraer_referencias_legales(texto),
            id_parte_xml=id_parte
        ))
    return articulos

def buscar_articulo(articulos: List[Articulo], articulo: str) -> List[Articulo]:
    # Normaliza para buscar con robustez
    art_normalizado = normalizar_numero_articulo(articulo)
    resultados = [a for a in articulos if a.articulo_id_interno == art_normalizado]
    if resultados:
        return resultados
    # Búsqueda textual en display/texto si no se encontró por ID
    resultados_texto = [a for a in articulos if articulo.lower() in a.texto.lower() or articulo.lower() in a.articulo_display.lower()]
    for r in resultados_texto:
        r.nota_busqueda = "Búsqueda textual (no coincidió identificador exacto)"
    return resultados_texto

# -----------------------------
# ENDPOINT: /ley
# -----------------------------
@app.get("/ley", response_model=LeyDetalle, summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley a consultar (ej. '21595')."),
    articulo: Optional[str] = Query(None, description="Número o identificador del artículo a consultar (ej. '15', '1 bis', 'Primero Transitorio').")
):
    id_norma = await obtener_id_norma(numero_ley)
    url_xml = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}"
    if id_norma in cache_xml_ley:
        xml = cache_xml_ley[id_norma]
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url_xml)
            if resp.status_code != 200:
                raise HTTPException(503, f"No se pudo obtener XML para la ley {numero_ley}")
            xml = resp.text
            cache_xml_ley[id_norma] = xml

    articulos = parsear_articulos(xml)
    total_original = len(articulos)
    articulos_respuesta = articulos
    nota_trunc = None

    if articulo:
        articulos_respuesta = buscar_articulo(articulos, articulo)
        if not articulos_respuesta:
            raise HTTPException(404, f"Artículo '{articulo}' no encontrado en la ley.")
    elif len(articulos) > MAX_ARTICULOS_RETURNED:
        articulos_respuesta = articulos[:MAX_ARTICULOS_RETURNED]
        nota_trunc = TRUNCATION_MESSAGE_LIST

    return LeyDetalle(
        ley=numero_ley,
        id_norma=id_norma,
        articulos_totales_en_respuesta=len(articulos_respuesta),
        articulos=articulos_respuesta,
        total_articulos_originales_en_ley=total_original if not articulo else None,
        nota_truncamiento_lista=nota_trunc
    )

# -----------------------------
# ENDPOINT: /ley_html
# -----------------------------
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
        f"div.textoNorma[id*='{idParte}']",
        f"div.textoArticulo[id*='{idParte}']",
        f"div[id='{idParte}']",
        f"div[id^='p{idParte}']",
    ]
    nodo = None
    sel_usado = ""
    for sel in selectores:
        tmp = soup.select_one(sel)
        if tmp:
            nodo = tmp
            sel_usado = sel
            break
    if not nodo:
        raise HTTPException(
            404,
            detail=f"No se encontró contenido para idParte '{idParte}' en norma '{idNorma}'."
        )
    texto = limpiar_texto(nodo.get_text(separator="\n", strip=True))
    if len(texto) > MAX_TEXT_LENGTH:
        texto = texto[:MAX_TEXT_LENGTH].rsplit(" ", 1)[0] + TRUNCATION_MESSAGE_TEXT
    return ArticuloHTML(
        idNorma=idNorma,
        idParte=idParte,
        url_fuente=url,
        selector_usado=sel_usado,
        texto_html_extraido=texto
    )

@app.get("/buscar", summary="Buscar leyes y artículos por palabra clave")
async def buscar_articulos(
    consulta: str = Query(..., description="Palabra o frase para buscar en el texto de artículos."),
    limite: int = Query(10, ge=1, le=50, description="Límite de resultados.")
):
    resultados = []
    # Búsqueda rápida en las últimas 50 leyes publicadas:
    url = "https://www.leychile.cl/Consulta/obtxml?opt=3&cantidad=50"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        root = ET.fromstring(resp.text)
        for n in root.findall(".//Norma"):
            numero_ley = n.findtext("Identificadores/Numero", default="")
            try:
                id_norma = await obtener_id_norma(numero_ley)
                url_xml = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}"
                xml_resp = await client.get(url_xml)
                articulos = parsear_articulos(xml_resp.text)
                for art in articulos:
                    if consulta.lower() in art.texto.lower():
                        resultados.append({
                            "ley": numero_ley,
                            "id_norma": id_norma,
                            "articulo_display": art.articulo_display,
                            "fragmento": art.texto[:200] + "..." if len(art.texto) > 200 else art.texto
                        })
                        if len(resultados) >= limite:
                            return resultados
            except Exception:
                continue
    return resultados

# --- FIN ---
# Para desarrollo local:
# uvicorn main:app --reload --host 0.0.0.0 --port 8000
