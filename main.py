# main.py
# API de Consulta de Leyes Chilenas — Versión del Usuario 2025-05-19
# Implementa dos endpoints:
#   1) /ley     → Devuelve el XML completo de la norma desde el web service de Ley Chile (opt=7)
#   2) /ley_html → Extrae el texto de un artículo específico desde el visualizador de la BCN

import logging
import os
import re
import json
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse # Usado para el exception_handler global
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
from cachetools import TTLCache
import xml.etree.ElementTree as ET # Usado en la nueva función extraer_articulos
from difflib import get_close_matches # Usado en la nueva función buscar_articulo_avanzado

logging.basicConfig(
    level=logging.INFO, # Cambiado a INFO, considera DEBUG para desarrollo
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True
)
logger = logging.getLogger("leyes-chile")
logger.info("Iniciando API Consulta Leyes Chilenas (Versión Usuario 2025-05-19)")

app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    description="Consulta profesional de artículos de leyes chilenas vía XML/HTML Ley Chile y web BCN. Potenciada para GPT, buscadores y legal tech.",
    version="2.3.0", # La versión que indicaste
    contact={"name": "Marco Fernandez Valdes", "email": "marco.fernandez.valdes@gmail.com"}, # Ejemplo, puedes cambiarlo
    terms_of_service="https://www.bcn.cl/portal/pagina/privacidad", # Enlace genérico, idealmente uno propio
    servers=[{"url": "https://consulta-leyes-chile.onrender.com", "description": "Servidor de Producción"}]
)

cache_id_norma = TTLCache(maxsize=200, ttl=3600)
cache_xml_ley = TTLCache(maxsize=50, ttl=3600)

MAX_TEXT_LENGTH = 20000
MAX_ARTICULOS_RETURNED = 15
TRUNCATION_SUFFIX = "\n\n[Texto truncado por longitud excesiva]" # Renombrado desde TRUNCATION_MESSAGE_TEXT

ROMAN_TO_INT = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 15, 'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20,
}
WORDS_TO_INT = {
    'primero': '1', 'segundo': '2', 'tercero': '3', 'cuarto': '4', 'quinto': '5',
    'sexto': '6', 'séptimo': '7', 'octavo': '8', 'noveno': '9', 'décimo': '10',
    'undécimo': '11', 'duodécimo': '12', 'decimotercero': '13', 'decimocuarto': '14', 'decimoquinto': '15',
    'vigésimo': '20', 'trigésimo': '30', 'cuadragésimo': '40', 'quincuagésimo': '50',
    'único': 'unico', 'unico': 'unico', 'final': 'final'
}

# -- Modelos --
class ArticuloHTML(BaseModel):
    idNorma: str
    idParte: str
    url_fuente: str # Renombrado desde url
    selector_usado: str
    texto_html_extraido: str

class Articulo(BaseModel):
    articulo_display: str
    articulo_id_interno: str
    texto: str
    referencias_legales: List[str] = Field(default_factory=list)
    id_parte_xml: Optional[str] = None
    nota_busqueda: Optional[str] = None

class LeyDetalle(BaseModel):
    ley: str # Renombrado desde numero_ley para consistencia con el modelo anterior
    id_norma: str
    # titulo: str # Eliminado ya que no se usa en el return del endpoint /ley
    # fecha_publicacion: str # Eliminado ya que no se usa
    articulos_totales_en_respuesta: int # Añadido para claridad
    articulos: List[Articulo]
    total_articulos_originales_en_ley: Optional[int] = None # Añadido
    nota_truncamiento_lista: Optional[str] = None # Añadido


# --- Utilidades ---
def limpiar_texto(texto: str) -> str: # Renombrado desde limpiar_texto_articulo
    """Normaliza espacios y líneas vacías."""
    if not texto:
        return ""
    txt = re.sub(r"[ \t]+", " ", texto)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return "\n".join(line.strip() for line in txt.split("\n")).strip()

def normalizar_numero_articulo(num_str: Optional[str]) -> str: # Renombrado
    if not num_str:
        return "s/n"
    s = str(num_str).lower().strip() # 's' se define aquí, ANTES de cualquier uso
    logger.debug(f"Normalizando (vUsuario): '{num_str}' -> '{s}' (inicial)")
    
    s = re.sub(r"^(artículo|articulo|art\.?|nro\.?|n[º°]|disposición|disp\.?)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.rstrip('.-').strip() 

    if s in WORDS_TO_INT: 
        logger.debug(f"Normalizado por WORDS_TO_INT (vUsuario): '{s}' -> '{WORDS_TO_INT[s]}'")
        return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:
        logger.debug(f"Normalizado por ROMAN_TO_INT (vUsuario): '{s}' -> '{str(ROMAN_TO_INT[s])}'")
        return str(ROMAN_TO_INT[s])
    
    prefijo_transitorio = ""
    transitorio_match = re.match(r"^(transitorio|trans\.?|t)\s*(.*)", s, flags=re.IGNORECASE)
    if transitorio_match:
        prefijo_transitorio = "t" 
        s_antes_trans = s
        s = transitorio_match.group(2).strip().rstrip('.-').strip() 
        logger.debug(f"Detectado transitorio (vUsuario): '{s_antes_trans}' -> prefijo='{prefijo_transitorio}', s='{s}'")
    
    s_antes_palabras = s
    for palabra, digito in WORDS_TO_INT.items(): 
        if re.search(r'\b' + re.escape(palabra) + r'\b', s): 
             s = re.sub(r'\b' + re.escape(palabra) + r'\b', digito, s)
    if s != s_antes_palabras: logger.debug(f"Después de reemplazar palabras numéricas (vUsuario): '{s_antes_palabras}' -> '{s}'")
    
    s_antes_ord = s
    s = re.sub(r"[º°ª\.,]", "", s) 
    if s != s_antes_ord: logger.debug(f"Después de quitar ordinales/puntuación (vUsuario): '{s_antes_ord}' -> '{s}'")
    
    s = s.strip().rstrip('-').strip() 

    partes_numericas = re.findall(r"(\d+)\s*([a-zA-Z]*)", s)
    logger.debug(f"Partes numéricas encontradas en '{s}' (vUsuario): {partes_numericas}")
    componentes_normalizados = []
    texto_restante = s 
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part: 
            if letra_part in ["bis", "ter", "quater"] or (len(letra_part) == 1 and letra_part.isalpha()):
                componente += letra_part
        componentes_normalizados.append(componente)
        texto_restante = texto_restante.replace(num_part, "", 1).replace(letra_part, "", 1).strip() 
    
    logger.debug(f"Componentes normalizados de partes numéricas (vUsuario): {componentes_normalizados}, texto restante: '{texto_restante}'")
    if not componentes_normalizados and texto_restante: 
        posible_romano = texto_restante.replace(" ", "") 
        if posible_romano in ROMAN_TO_INT: 
            componentes_normalizados.append(str(ROMAN_TO_INT[posible_romano]))
            logger.debug(f"Componente romano añadido (vUsuario): '{str(ROMAN_TO_INT[posible_romano])}' desde '{posible_romano}'")
            texto_restante = ""
    if not componentes_normalizados and texto_restante: 
        componentes_normalizados.append(texto_restante.replace(" ", ""))
        logger.debug(f"Componente de texto restante añadido (vUsuario): '{texto_restante.replace(' ', '')}'")
    
    id_final = prefijo_transitorio + "".join(componentes_normalizados) # Corregido para añadir prefijo_transitorio
    if not id_final.strip(prefijo_transitorio): # Verificar si id_final (sin el prefijo) está vacío
        id_final_sin_prefijo = re.sub(r"[^a-z0-9]", "", s.replace(" ", "")).strip()
        logger.debug(f"ID final (sin prefijo) estaba vacío. s='{s}', id_final_sin_prefijo='{id_final_sin_prefijo}'")
        if not id_final_sin_prefijo: 
            logger.warning(f"Error de normalización para '{num_str}' (vUsuario). No se pudo extraer un ID limpio.")
            return "s/n_error_normalizacion"
        id_final = prefijo_transitorio + id_final_sin_prefijo
    
    logger.debug(f"Normalización final para '{num_str}' (vUsuario): '{id_final}'")
    return id_final if id_final else "s/n"


def extraer_referencias_legales(texto: str) -> List[str]: # Renombrado
    refs = re.findall(r'\b[Ll]ey\s+N?(?:[°ºªo]|\.?)?\s*[\d\.]+|\b[Dd][Ss]?\s*N[°º]?\s*\d{3,6}|\b[Dd][Ff][Ll]?\s*N[°º]?\s*\d{1,6}', texto)
    return list(set([limpiar_texto(r) for r in refs]))

async def obtener_id_norma(numero_ley: str, client: Optional[httpx.AsyncClient]=None) -> str:
    # Esta función es diferente a la que desarrollamos, usa scraping en lugar de XML para el ID.
    # Puede ser menos fiable. Mantenemos la lógica del usuario.
    logger.info(f"Obteniendo idNorma para ley {numero_ley} (método usuario)")
    if numero_ley in cache_id_norma:
        logger.info(f"idNorma para {numero_ley} encontrado en caché: {cache_id_norma[numero_ley]}")
        return cache_id_norma[numero_ley]
    
    # Intenta con fallback_ids primero si el número de ley es puramente numérico
    norm_numero_ley_buscado = numero_ley.strip().replace(".", "").replace(",", "")
    if norm_numero_ley_buscado in FALLBACK_IDS: # FALLBACK_IDS es el nombre usado en el código del usuario
        id_norma_fallback = FALLBACK_IDS[norm_numero_ley_buscado]
        logger.info(f"Usando ID de fallback para ley '{norm_numero_ley_buscado}': {id_norma_fallback}")
        cache_id_norma[numero_ley] = id_norma_fallback # Usar numero_ley original como clave de caché
        return id_norma_fallback

    url_busqueda = f"https://www.leychile.cl/Navegar?idLey={norm_numero_ley_buscado}" # Usar número normalizado
    
    _client = client
    close_client = False
    if _client is None:
        _client = httpx.AsyncClient(follow_redirects=True, timeout=15) # Aumentar timeout y seguir redirecciones
        close_client = True
    
    try:
        logger.info(f"Consultando {url_busqueda} para extraer idNorma")
        r = await _client.get(url_busqueda)
        r.raise_for_status() # Verificar errores HTTP
        
        # El idNorma suele estar en la URL final después de redirecciones, o en el HTML
        final_url = str(r.url)
        logger.debug(f"URL final después de redirecciones: {final_url}")
        m_url = re.search(r"idNorma=(\d+)", final_url)
        if m_url:
            id_norma = m_url.group(1)
            logger.info(f"idNorma '{id_norma}' extraído de URL final para ley {numero_ley}")
            cache_id_norma[numero_ley] = id_norma
            return id_norma

        # Si no está en la URL, buscar en el HTML (menos fiable)
        soup = BeautifulSoup(r.text, "html.parser")
        # Buscar patrones comunes donde podría estar el idNorma
        # Ejemplo: <meta name="keywords" content="LEY NUM. XXX, IDNORMA=YYYYY">
        # O en enlaces dentro de la página. Esto es muy específico del sitio y frágil.
        # Por ahora, nos basamos en la URL. Si falla, el siguiente método lo intentará por XML.
        # Si se quiere mejorar, se necesitaría inspeccionar el HTML de bcn.cl para varios casos.
        logger.warning(f"No se pudo extraer idNorma de la URL final para ley {numero_ley}. HTML (inicio): {r.text[:500]}")
        raise HTTPException(404, f"No se pudo encontrar el IdNorma para la ley {numero_ley} mediante navegación. Pruebe el fallback o búsqueda XML.")

    except httpx.HTTPStatusError as e:
        logger.error(f"Error HTTP {e.response.status_code} al obtener idNorma para ley {numero_ley} desde {url_busqueda}")
        raise HTTPException(status_code=e.response.status_code, detail=f"Error al conectar con LeyChile para obtener IdNorma: {e.response.status_code}")
    except Exception as e:
        logger.exception(f"Error inesperado al obtener idNorma para ley {numero_ley}")
        raise HTTPException(500, f"Error inesperado al obtener IdNorma para la ley {numero_ley}")
    finally:
        if close_client and _client:
            await _client.aclose()


async def obtener_xml_ley(id_norma: str, client: Optional[httpx.AsyncClient]=None) -> str:
    if id_norma in cache_xml_ley:
        logger.info(f"XML para idNorma {id_norma} encontrado en caché.")
        return cache_xml_ley[id_norma]
    url_xml = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1" # Añadido notaPIE=1
    logger.info(f"Consultando XML para idNorma {id_norma} desde {url_xml}")
    
    _client = client
    close_client = False
    if _client is None:
        _client = httpx.AsyncClient(timeout=20) # Aumentar timeout
        close_client = True
    
    try:
        resp = await _client.get(url_xml)
        resp.raise_for_status() # Verificar errores HTTP
        xml_content = resp.text
        cache_xml_ley[id_norma] = xml_content
        logger.info(f"XML para idNorma {id_norma} obtenido y cacheado.")
        return xml_content
    except httpx.HTTPStatusError as e:
        logger.error(f"Error HTTP {e.response.status_code} al obtener XML para idNorma {id_norma} desde {url_xml}")
        raise HTTPException(status_code=e.response.status_code, detail=f"No se pudo obtener XML para la ley con idNorma={id_norma}. Error: {e.response.status_code}")
    except Exception as e:
        logger.exception(f"Error inesperado al obtener XML para idNorma {id_norma}")
        raise HTTPException(500, f"Error inesperado al obtener XML para la ley con idNorma={id_norma}")
    finally:
        if close_client and _client:
            await _client.aclose()


def extraer_articulos(xml_content: str) -> List[Articulo]: # Cambiado xml a xml_content para claridad
    # Esta función usa xml.etree.ElementTree como en el código del usuario.
    # La versión anterior usaba BeautifulSoup. Ambas son válidas.
    logger.info(f"Iniciando extracción de artículos desde XML. Longitud XML: {len(xml_content)} chars.")
    ns = {'n': 'http://www.leychile.cl/esquemas'} # Namespace definido por LeyChile
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        logger.error(f"Error al parsear XML: {e}. XML (inicio): {xml_content[:1000]}")
        # Devolver lista vacía o lanzar excepción, dependiendo de cómo se quiera manejar.
        # Si el XML está malformado, no se pueden extraer artículos.
        raise HTTPException(status_code=500, detail="El XML recibido de LeyChile está malformado o no es válido.")

    articulos_extraidos = []
    
    # Buscar todas las EstructuraFuncional que son tipo Artículo
    for ef_tag in root.findall('.//n:EstructuraFuncional[@tipoParte="Artículo"]', ns):
        id_parte_xml = ef_tag.get('idParte')
        
        # El número del artículo suele estar en Metadatos/TituloParte o Metadatos/NombreParte dentro de EstructuraFuncional
        # O al inicio del tag Texto.
        # Priorizar Metadatos si existe.
        metadatos_tag = ef_tag.find('n:Metadatos', ns)
        numero_display_extraido = "S/N"
        if metadatos_tag is not None:
            titulo_parte_tag = metadatos_tag.find('n:TituloParte', ns)
            if titulo_parte_tag is not None and titulo_parte_tag.text and titulo_parte_tag.text.strip():
                numero_display_extraido = limpiar_texto(titulo_parte_tag.text)
            else: # Fallback a NombreParte si TituloParte no está o está vacío
                nombre_parte_tag = metadatos_tag.find('n:NombreParte', ns)
                if nombre_parte_tag is not None and nombre_parte_tag.text and nombre_parte_tag.text.strip():
                    numero_display_extraido = limpiar_texto(nombre_parte_tag.text)
        
        texto_tag = ef_tag.find('n:Texto', ns)
        texto_completo_articulo = texto_tag.text.strip() if texto_tag is not None and texto_tag.text else ""
        
        texto_neto_articulo = texto_completo_articulo
        # Si el numero_display_extraido aún es S/N o no se pudo extraer bien de metadatos,
        # intentar extraerlo del inicio del texto_completo_articulo
        if numero_display_extraido == "S/N" or not normalizar_numero_articulo(numero_display_extraido).isdigit(): # Si no es un número claro
            match_numero_en_texto = re.match(r"^\s*((?:art(?:ículo|iculo)?s?\.?\s*)?[\w\d\s]+(?:bis|ter|quater)?)\s*([\.\-\–\—:]\s*)?(.*)", texto_completo_articulo, re.IGNORECASE | re.DOTALL)
            if match_numero_en_texto:
                display_temp = match_numero_en_texto.group(1).strip()
                # Limpiar "Artículo " del display si está presente
                numero_display_extraido = re.sub(r"^(artículo|articulo|art\.?)\s*", "", display_temp, flags=re.IGNORECASE).strip().rstrip('.-').strip()
                if match_numero_en_texto.group(3) and match_numero_en_texto.group(3).strip():
                     texto_neto_articulo = match_numero_en_texto.group(3).strip()
                elif numero_display_extraido != texto_completo_articulo:
                     texto_neto_articulo = "" 
        
        # Si después de todo, numero_display_extraido sigue siendo S/N, usar el id_parte_xml si existe
        if numero_display_extraido == "S/N" and id_parte_xml:
            numero_display_extraido = f"IDParte-{id_parte_xml}" # Placeholder display

        numero_id_interno = normalizar_numero_articulo(numero_display_extraido)

        if numero_id_interno == "s/n_error_normalizacion":
            logger.warning(f"Error normalizando display '{numero_display_extraido}' para idParte '{id_parte_xml}'. Omitiendo.")
            continue

        texto_final_limpio = limpiar_texto(texto_neto_articulo)
        if len(texto_final_limpio) > MAX_TEXT_LENGTH:
            logger.warning(f"Artículo (display='{numero_display_extraido}', id_interno='{numero_id_interno}') texto truncado.")
            texto_final_limpio = texto_final_limpio[:MAX_TEXT_LENGTH].rsplit(' ', 1)[0] + TRUNCATION_SUFFIX
        
        referencias = extraer_referencias_legales(texto_final_limpio) # Usar la función del usuario

        articulos_extraidos.append(Articulo(
            articulo_display=numero_display_extraido,
            articulo_id_interno=numero_id_interno,
            texto=texto_final_limpio,
            referencias_legales=referencias,
            id_parte_xml=id_parte_xml
        ))
    logger.info(f"Extracción de artículos finalizada. {len(articulos_extraidos)} artículos procesados.")
    return articulos_extraidos


def buscar_articulo_avanzado(articulos: List[Articulo], articulo_buscado: str) -> List[Articulo]:
    # Esta función es del código del usuario, se mantiene su lógica.
    art_normalizado = normalizar_numero_articulo(articulo_buscado)
    logger.info(f"Búsqueda avanzada para artículo normalizado: '{art_normalizado}' (original: '{articulo_buscado}')")
    
    # 1. Por id interno exacto
    resultados = [a for a in articulos if a.articulo_id_interno == art_normalizado]
    if resultados:
        logger.info(f"Encontrado por ID interno exacto: {len(resultados)} coincidencias.")
        return resultados
    
    # 2. Por display exacto (ignorando mayúsculas/minúsculas y espacios extra)
    articulo_buscado_lower_strip = articulo_buscado.strip().lower()
    posibles = [a for a in articulos if articulo_buscado_lower_strip == a.articulo_display.strip().lower()]
    if posibles:
        logger.info(f"Encontrado por display exacto: {len(posibles)} coincidencias.")
        for r in posibles: r.nota_busqueda = "Búsqueda por display exacto"
        return posibles
    
    # 3. Por id_parte_xml (si el artículo buscado es un número que podría ser un idParte)
    if articulo_buscado.isdigit(): # Solo intentar si el input es numérico
        posibles = [a for a in articulos if str(a.id_parte_xml) == articulo_buscado]
        if posibles:
            logger.info(f"Encontrado por id_parte_xml: {len(posibles)} coincidencias.")
            for r in posibles: r.nota_busqueda = "Búsqueda por id_parte_xml"
            return posibles
            
    # 4. Búsqueda textual difusa en el display o en el texto
    logger.info(f"Intentando búsqueda textual para '{articulo_buscado_lower_strip}'...")
    posibles = [
        a for a in articulos
        if articulo_buscado_lower_strip in a.texto.lower() or \
           articulo_buscado_lower_strip in a.articulo_display.lower() or \
           (art_normalizado != "s/n" and art_normalizado != "s/n_error_normalizacion" and art_normalizado in a.texto.lower()) # buscar también el normalizado en el texto
    ]
    if posibles:
        logger.info(f"Encontrado por búsqueda textual: {len(posibles)} coincidencias.")
        for r in posibles:
            r.nota_busqueda = "Búsqueda textual (no coincidió identificador exacto)"
        return posibles
        
    # Si no se encuentra nada, lanzar excepción con sugerencias
    ids_internos = [a.articulo_id_interno for a in articulos]
    displays = [a.articulo_display for a in articulos]
    match_cercanos_ids = get_close_matches(art_normalizado, ids_internos, n=3, cutoff=0.7)
    match_cercanos_disp = get_close_matches(articulo_buscado_lower_strip, displays, n=3, cutoff=0.6)
    
    sugerencias = list(set(match_cercanos_ids + match_cercanos_disp))
    detalle_error = {
        "error": f"Artículo '{articulo_buscado}' (buscado como '{art_normalizado}') no encontrado.",
        "sugerencia": f"Quizá quiso decir: {sugerencias}" if sugerencias else "No se encontraron coincidencias cercanas.",
        "ids_disponibles_muestra": ids_internos[:15],
        "displays_disponibles_muestra": displays[:15]
    }
    logger.warning(f"Artículo no encontrado: {detalle_error}")
    raise HTTPException(status_code=404, detail=detalle_error)

# --- ENDPOINTS ---

@app.get("/ley", response_model=LeyDetalle, summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley a consultar (ej. '21595')."),
    articulo: Optional[str] = Query(None, description="Número o identificador del artículo a consultar.")
):
    logger.info(f"INICIO /ley | numero_ley={numero_ley}, articulo={articulo or 'Todos'}")
    async with httpx.AsyncClient() as client:
        try:
            id_norma = await obtener_id_norma(numero_ley, client) # Usando la función del usuario
            logger.info(f"ID NORMA obtenido: {id_norma}")
        except HTTPException as e: # Capturar HTTPException de obtener_id_norma
            logger.error(f"Error obteniendo id_norma para ley {numero_ley}: {e.detail}")
            raise e # Re-lanzar la excepción
        except Exception as e:
            logger.exception(f"Error inesperado en obtener_id_norma para ley {numero_ley}")
            raise HTTPException(status_code=500, detail=f"Error interno obteniendo ID de norma para ley {numero_ley}.")

        if not id_norma: # Esto no debería ocurrir si obtener_id_norma lanza excepción en error
             logger.error(f"No se encontró ID para la ley {numero_ley} (retorno None de obtener_id_norma)")
             raise HTTPException(status_code=404, detail=f"No se encontró ID para la ley {numero_ley}.")

        try:
            xml_content = await obtener_xml_ley(id_norma, client) # Usando la función del usuario
            logger.info(f"XML recibido para id_norma {id_norma}: {'Sí' if xml_content else 'No'}")
        except HTTPException as e:
            logger.error(f"Error obteniendo XML para id_norma {id_norma}: {e.detail}")
            raise e
        except Exception as e:
            logger.exception(f"Error inesperado en obtener_xml_ley para id_norma {id_norma}")
            raise HTTPException(status_code=500, detail=f"Error interno obteniendo XML para id_norma {id_norma}.")

        if not xml_content:
            logger.error(f"No se pudo obtener XML para ley {numero_ley} (ID Norma: {id_norma})")
            raise HTTPException(status_code=503, detail=f"No se pudo obtener XML para ley {numero_ley} (ID Norma: {id_norma}).")
    
    articulos = extraer_articulos(xml_content) # Usando la función del usuario
    logger.info(f"Artículos extraídos de XML: {len(articulos)}")
    if not articulos: 
        logger.error(f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma}) desde el XML.")
        raise HTTPException(status_code=404, detail=f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma}). El XML podría estar vacío o no tener artículos en el formato esperado.")
    
    total_original = len(articulos)
    nota_trunc_lista = None
    articulos_respuesta = articulos

    if articulo:
        logger.info(f"Buscando artículo específico: '{articulo}'")
        articulos_respuesta = buscar_articulo_avanzado(articulos, articulo) # Usa la función de búsqueda del usuario
        logger.info(f"Artículos encontrados tras búsqueda avanzada: {len(articulos_respuesta)}")
    elif len(articulos) > MAX_ARTICULOS_RETURNED:
        logger.info(f"Ley {numero_ley} tiene {total_original} artículos. Devolviendo los primeros {MAX_ARTICULOS_RETURNED}.")
        articulos_respuesta = articulos[:MAX_ARTICULOS_RETURNED]
        nota_trunc_lista = f"Mostrando los primeros {len(articulos_respuesta)} de {total_original} artículos. Para un artículo específico, especifique el número."
    
    # El modelo LeyDetalle del usuario no tiene titulo ni fecha_publicacion a nivel de Ley, se omiten.
    return LeyDetalle(
        ley=numero_ley,
        id_norma=id_norma,
        articulos_totales_en_respuesta=len(articulos_respuesta),
        articulos=articulos_respuesta,
        total_articulos_originales_en_ley=total_original if nota_trunc_lista or not articulo else None,
        nota_truncamiento_lista=nota_trunc_lista
    )

@app.get("/ley_html", response_model=ArticuloHTML, summary="Extraer texto HTML de un artículo específico")
async def consultar_articulo_html(
    idNorma: str = Query(..., description="Identificador único de la norma (idNorma)."),
    idParte: str = Query(..., description="Identificador de la parte/artículo (idParte).")
):
    # Esta es la implementación del usuario para /ley_html
    logger.info(f"Consultando HTML desde bcn.cl para idNorma: {idNorma}, idParte: {idParte}")
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    async with httpx.AsyncClient(timeout=10) as client: # Timeout general para el cliente
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}
            resp = await client.get(url, headers=headers)
            logger.debug(f"Respuesta de /ley_html para {url} - Status: {resp.status_code}")
            resp.raise_for_status()
        except httpx.TimeoutException:
            logger.error(f"Timeout al obtener HTML para idNorma {idNorma}, idParte {idParte} desde {url}")
            raise HTTPException(status_code=504, detail=f"Timeout al obtener HTML para idNorma {idNorma}, idParte {idParte}.")
        except httpx.HTTPStatusError as e:
            logger.error(f"Error HTTP {e.response.status_code} al obtener HTML para idNorma {idNorma}, idParte {idParte} desde {url}")
            raise HTTPException(status_code=e.response.status_code, detail=f"Error al conectar con BCN ({e.response.status_code}) para idNorma {idNorma}, idParte {idParte}.")
        except httpx.RequestError as e:
            logger.error(f"Error de red al obtener HTML para idNorma {idNorma}, idParte {idParte} desde {url}: {e}")
            raise HTTPException(status_code=502, detail=f"No se pudo obtener el contenido de {url}. Error de red.")

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        # Selectores tal como los proporcionó el usuario
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
        sel_usado = "Ninguno"
        for idx, sel in enumerate(selectores):
            logger.debug(f"Intentando selector HTML #{idx+1}: '{sel}'")
            tmp = soup.select_one(sel)
            if tmp:
                nodo = tmp
                sel_usado = sel
                logger.info(f"Contenido encontrado para idParte '{idParte}' usando selector CSS '{sel}'")
                break
            else:
                logger.debug(f"Selector '{sel}' no encontró nada.")
        
        if not nodo:
            logger.error(f"No se encontró contenido para idParte '{idParte}' en norma '{idNorma}' con los selectores probados en {url}.")
            # Loggear el HTML si no se encuentra el contenido, para depuración
            html_snippet = resp.text[:2000] if resp.text else "HTML vacío"
            logger.debug(f"HTML recibido donde no se encontró el contenido (primeros 2000 chars):\n{html_snippet}")
            raise HTTPException(
                status_code=404,
                detail=f"No se encontró contenido para idParte '{idParte}' con los selectores probados en BCN."
            )

        texto_extraido = nodo.get_text(separator="\n", strip=True)
        texto_limpio = limpiar_texto(texto_extraido) # Usar la función de limpieza del usuario
        
        if len(texto_limpio) > MAX_TEXT_LENGTH:
            logger.warning(f"Texto HTML para idNorma {idNorma}, idParte {idParte} truncado. Longitud original: {len(texto_limpio)}")
            texto_limpio = texto_limpio[:MAX_TEXT_LENGTH].rsplit(" ", 1)[0] + TRUNCATION_SUFFIX

        return ArticuloHTML(
            idNorma=idNorma,
            idParte=idParte,
            url_fuente=url, # Corregido de url a url_fuente
            selector_usado=sel_usado,
            texto_html_extraido=texto_limpio
        )
    except HTTPException as he: # Re-lanzar HTTPExceptions para que FastAPI las maneje
        raise he
    except Exception as e:
        logger.exception(f"Error al procesar HTML para idNorma {idNorma}, idParte {idParte}.")
        raise HTTPException(status_code=500, detail=f"Error interno al procesar HTML: {str(e)}")


# --- Manejo de errores globales ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Any, exc: Exception): # request debe ser tipado con starlette.requests.Request si se usa
    logger.error(f"Error global no capturado en la ruta {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Error interno del servidor", "detalles": "Ocurrió un error inesperado."} # No exponer str(exc) directamente en producción
    )

# Para desarrollo local:
# uvicorn main:app --reload --host 0.0.0.0 --port 8000
