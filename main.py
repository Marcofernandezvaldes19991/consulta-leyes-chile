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
logger.info("API v19.HTML_DIAG (Restaurada con /ley_html) INICIANDO...") # Log distintivo

from fastapi import FastAPI, HTTPException, Query
from typing import Optional, List, Any 
import httpx 
from bs4 import BeautifulSoup
import re
import json
import os
import time 
from pydantic import BaseModel, Field
from cachetools import TTLCache 

app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    description="Permite consultar artículos de leyes chilenas obteniendo datos desde LeyChile.cl. Esta versión incluye /ley (XML) y /ley_html (HTML scraping).",
    version="1.9.0", 
    servers=[
        {
            "url": "https://consulta-leyes-chile.onrender.com", # Asegúrate que esta sea tu URL de Render
            "description": "Servidor de Producción en Render"
        }
    ]
)

try:
    # Intenta establecer el nivel de los loggers de Uvicorn si es posible
    logging.getLogger("uvicorn").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.error").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.access").setLevel(logging.DEBUG)
    logger.info("Se intentó establecer el nivel de log de Uvicorn a DEBUG desde el código (después de FastAPI init).")
except Exception as e:
    logger.warning(f"No se pudieron configurar los loggers de Uvicorn desde el código: {e}")

# --- Configuración de Caché ---
cache_id_norma = TTLCache(maxsize=100, ttl=3600)
cache_xml_ley = TTLCache(maxsize=50, ttl=3600)


# --- Carga de Fallbacks ---
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    fallback_path = os.path.join(current_dir, "fallbacks.json")
    with open(fallback_path, "r", encoding="utf-8") as f:
        fallback_ids = json.load(f)
    logger.info(f"IDs de fallback cargados exitosamente desde: {fallback_path}")
except FileNotFoundError:
    fallback_ids = {}
    logger.warning(f"Archivo 'fallbacks.json' no encontrado en {fallback_path}. El fallback de IDs no estará disponible.")
except json.JSONDecodeError:
    fallback_ids = {}
    logger.warning(f"Error al decodificar 'fallbacks.json'. El fallback de IDs no estará disponible.")
except Exception as e: 
    fallback_ids = {}
    logger.exception(f"Ocurrió un error inesperado al cargar 'fallbacks.json'.")

# --- Constantes para Normalización y Truncamiento ---
ROMAN_TO_INT = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15, 'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20,
}
WORDS_TO_INT = {
    'primero': '1', 'segundo': '2', 'tercero': '3', 'cuarto': '4', 'quinto': '5',
    'sexto': '6', 'séptimo': '7', 'octavo': '8', 'noveno': '9', 'décimo': '10',
    'undécimo': '11', 'duodécimo': '12', 'decimotercero': '13', 'decimocuarto': '14', 'decimoquinto': '15',
    'vigésimo': '20', 'trigésimo': '30', 'cuadragésimo': '40', 'quincuagésimo': '50',
    'unico': 'unico', 'final': 'final', 
    'único': 'unico', 
}
MAX_TEXT_LENGTH = 10000 
MAX_ARTICULOS_RETURNED = 15 
TRUNCATION_MESSAGE_TEXT = "\n\n[... texto completo del artículo truncado por exceder el límite de longitud para esta API. Consulte la fuente original para el texto íntegro ...]"
TRUNCATION_MESSAGE_LIST = f"Mostrando los primeros {MAX_ARTICULOS_RETURNED} artículos. La ley contiene más artículos. Para ver un artículo específico, por favor especifíquelo en la consulta."

# --- Modelos Pydantic ---
class Articulo(BaseModel):
    articulo_display: str = Field(..., description="El número o identificador del artículo tal como se muestra originalmente (ej. 'Artículo 15', 'Primero Transitorio').")
    articulo_id_interno: str = Field(..., description="El identificador normalizado del artículo usado para búsquedas internas (ej. '15', 't1').")
    texto: str = Field(..., description="El contenido textual del artículo, con formato mejorado. Puede estar truncado si excede el límite de longitud.")
    referencias_legales: List[str] = Field(default_factory=list, description="Lista de referencias precisas a otras normativas encontradas en el texto del artículo.")
    id_parte_xml: Optional[str] = Field(None, description="El atributo 'idParte' de la etiqueta <EstructuraFuncional> en el XML original, si está disponible.")
    nota_busqueda: Optional[str] = Field(None, description="Nota adicional si el artículo fue encontrado por búsqueda textual en lugar de ID exacto.")

class LeyDetalle(BaseModel):
    ley: str = Field(..., description="El número de ley consultado.")
    id_norma: str = Field(..., description="El IdNorma interno de la ley en LeyChile.cl.")
    articulos_totales_en_respuesta: int = Field(..., description="El número total de artículos contenidos en ESTA respuesta (puede estar truncado).")
    articulos: List[Articulo] = Field(..., description="La lista de artículos (puede ser uno solo si se buscó un artículo específico, o una lista truncada si la ley es muy extensa).")
    total_articulos_originales_en_ley: Optional[int] = Field(None, description="El número total de artículos que tiene la ley originalmente, si la lista de artículos devuelta fue truncada.")
    nota_truncamiento_lista: Optional[str] = Field(None, description="Nota indicando si la lista de artículos fue truncada.")


class ArticuloHTML(BaseModel):
    idNorma: str
    idParte: str
    url_fuente: str
    selector_usado: str
    texto_html_extraido: str = Field(..., description="El texto HTML extraído, con formato mejorado. Puede estar truncado si excede el límite de longitud.")

# --- Funciones de Lógica de Negocio ---

def normalizar_numero_articulo_para_comparacion(num_str: Optional[str]) -> str:
    if not num_str: 
        return "s/n"
    s = str(num_str).lower().strip() # 's' se define aquí
    logger.debug(f"Normalizando: '{num_str}' -> '{s}' (inicial)")
    
    s = re.sub(r"^(artículo|articulo|art\.?|nro\.?|n[º°]|disposición|disp\.?)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.rstrip('.-').strip() 

    if s in WORDS_TO_INT: 
        logger.debug(f"Normalizado por WORDS_TO_INT: '{s}' -> '{WORDS_TO_INT[s]}'")
        return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:
        logger.debug(f"Normalizado por ROMAN_TO_INT: '{s}' -> '{str(ROMAN_TO_INT[s])}'")
        return str(ROMAN_TO_INT[s])
    
    prefijo_transitorio = ""
    transitorio_match = re.match(r"^(transitorio|trans\.?|t)\s*(.*)", s, flags=re.IGNORECASE)
    if transitorio_match:
        prefijo_transitorio = "t" 
        s_antes_trans = s
        s = transitorio_match.group(2).strip().rstrip('.-').strip() 
        logger.debug(f"Detectado transitorio: '{s_antes_trans}' -> prefijo='{prefijo_transitorio}', s='{s}'")
    
    s_antes_palabras = s
    for palabra, digito in WORDS_TO_INT.items(): 
        if re.search(r'\b' + re.escape(palabra) + r'\b', s): 
             s = re.sub(r'\b' + re.escape(palabra) + r'\b', digito, s)
    if s != s_antes_palabras: logger.debug(f"Después de reemplazar palabras numéricas: '{s_antes_palabras}' -> '{s}'")
    
    s_antes_ord = s
    s = re.sub(r"[º°ª\.,]", "", s) 
    if s != s_antes_ord: logger.debug(f"Después de quitar ordinales/puntuación: '{s_antes_ord}' -> '{s}'")
    
    s = s.strip().rstrip('-').strip() 

    partes_numericas = re.findall(r"(\d+)\s*([a-zA-Z]*)", s)
    logger.debug(f"Partes numéricas encontradas en '{s}': {partes_numericas}")
    componentes_normalizados = []
    texto_restante = s 
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part: 
            if letra_part in ["bis", "ter", "quater"] or (len(letra_part) == 1 and letra_part.isalpha()):
                componente += letra_part
        componentes_normalizados.append(componente)
        texto_restante = texto_restante.replace(num_part, "", 1).replace(letra_part, "", 1).strip() 
    
    logger.debug(f"Componentes normalizados de partes numéricas: {componentes_normalizados}, texto restante: '{texto_restante}'")
    if not componentes_normalizados and texto_restante: 
        posible_romano = texto_restante.replace(" ", "") 
        if posible_romano in ROMAN_TO_INT: 
            componentes_normalizados.append(str(ROMAN_TO_INT[posible_romano]))
            logger.debug(f"Componente romano añadido: '{str(ROMAN_TO_INT[posible_romano])}' desde '{posible_romano}'")
            texto_restante = ""
    if not componentes_normalizados and texto_restante: 
        componentes_normalizados.append(texto_restante.replace(" ", ""))
        logger.debug(f"Componente de texto restante añadido: '{texto_restante.replace(' ', '')}'")
    
    id_final = "".join(componentes_normalizados)
    if not id_final: 
        s_limpio = re.sub(r"[^a-z0-9]", "", s.replace(" ", "")).strip() 
        logger.debug(f"ID final estaba vacío. s='{s}', s_limpio='{s_limpio}'")
        if not s_limpio: 
            logger.warning(f"Error de normalización para '{num_str}'. No se pudo extraer un ID limpio.")
            return "s/n_error_normalizacion"
        id_final = s_limpio
    
    id_con_prefijo = prefijo_transitorio + id_final if id_final else "s/n"
    logger.debug(f"Normalización final para '{num_str}': '{id_con_prefijo}'")
    return id_con_prefijo

def limpiar_texto_articulo(texto: str) -> str:
    if not texto: return ""
    texto_limpio = re.sub(r'[ \t]+', ' ', texto)
    texto_limpio = re.sub(r'\n\s*\n+', '\n', texto_limpio) 
    texto_limpio = "\n".join([line.strip() for line in texto_limpio.split('\n')])
    return texto_limpio.strip()

def extraer_referencias_legales_mejorado(texto_articulo: str) -> List[str]:
    if not texto_articulo: return []
    patrones = [
        r"ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+",
        r"decreto\s+ley\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+",
        r"decreto\s+con\s+fuerza\s+de\s+ley\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+(?:,\s*de\s+\d{4}(?:,\s*d[e|el]\s*\w+(?:\s+de\s+\w+)*)?(?:,\s*d[e|el]\s*\w+(?:\s+de\s+\w+)*)?(?:,\s*d[e|el]\s*\w+(?:\s+de\s+\w+)*)?)?",
        r"D\.F\.L\.?\s+(?:N(?:[°ºªo]|\.?)?|No\.)?\s*[\d\.]+(?:/\d+)?",
        r"D\.L\.?\s+(?:N(?:[°ºªo]|\.?)?|No\.)?\s*[\d\.]+",
        r"C[óo]digo\s+(?:Penal|Tributario|Civil|del\s+Trabajo|de\s+Comercio|Procesal\s+Penal|Sanitario|de\s+Miner[íi]a|de\s+Aguas)(?:\s+y\s+sus\s+modificaciones)?",
        r"art[íi]culo\s+[\w\d]+(?:\s*bis)?\s+d[e|el]\s+(?:presente\s+ley|esta\s+ley|la\s+ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+|C[óo]digo\s+\w+|D\.F\.L\.?\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+|decreto\s+ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+)",
        r"inciso\s+\w+\s+del\s+art[íi]culo\s+[\w\d]+(?:\s*bis)?",
        r"reglamento\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+",
        r"Constituci[oó]n\s+Pol[íi]tica\s+de\s+la\s+Rep[úu]blica",
        r"decreto\s+supremo\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+(?:,\s*de\s+\d{4}(?:,\s*d[e|el]\s*\w+(?:\s+de\s+\w+)*)?)?",
    ]
    referencias_encontradas = set()
    texto_norm_espacios = re.sub(r'\s+', ' ', texto_articulo)
    for patron in patrones:
        try:
            for match_obj in re.finditer(patron, texto_norm_espacios, re.IGNORECASE):
                match_str = match_obj.group(0); ref_limpia = match_str.strip(" .,;:()-")
                if len(ref_limpia) > 5: referencias_encontradas.add(ref_limpia)
        except re.error as e: logger.error(f"Error en regex '{patron}': {e}")
    return sorted(list(referencias_encontradas))

async def obtener_id_norma_async(numero_ley: str, client: httpx.AsyncClient) -> Optional[str]:
    norm_numero_ley_buscado = numero_ley.strip().replace(".", "").replace(",", "")
    if not norm_numero_ley_buscado.isdigit(): logger.warning(f"El número de ley '{numero_ley}' no es puramente numérico.")
    cached_id = cache_id_norma.get(norm_numero_ley_buscado)
    if cached_id: logger.info(f"IdNorma '{cached_id}' para ley '{norm_numero_ley_buscado}' obtenido desde caché."); return cached_id
    if norm_numero_ley_buscado in fallback_ids:
        logger.info(f"Usando ID de fallback para ley '{norm_numero_ley_buscado}': {fallback_ids[norm_numero_ley_buscado]}"); cache_id_norma[norm_numero_ley_buscado] = fallback_ids[norm_numero_ley_buscado]; return fallback_ids[norm_numero_ley_buscado]
    if not norm_numero_ley_buscado.isdigit(): logger.warning(f"Número de ley '{norm_numero_ley_buscado}' no es numérico y no se encontró en fallbacks."); return None
    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{norm_numero_ley_buscado}"; logger.info(f"Consultando URL para ID de norma (async): {url}"); max_retries = 3; retry_delay = 1
    for attempt in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}; response = await client.get(url, timeout=10, headers=headers); response.raise_for_status(); soup = BeautifulSoup(response.content, "xml"); normas = soup.find_all("Norma")
            if not normas: return None
            for norma in normas:
                numero_norma_tag = norma.find("Numero")
                if numero_norma_tag and numero_norma_tag.text:
                    numero_norma_en_xml = numero_norma_tag.text.strip().replace(".", "").replace(",", "")
                    if numero_norma_en_xml == norm_numero_ley_buscado:
                        id_norma_tag = norma.find("IdNorma")
                        if id_norma_tag and id_norma_tag.text: id_encontrado = id_norma_tag.text.strip(); cache_id_norma[norm_numero_ley_buscado] = id_encontrado; return id_encontrado
            return None
        except httpx.TimeoutException: logger.warning(f"Timeout (Intento {attempt + 1}/{max_retries})")
        except httpx.RequestError as e: logger.error(f"Error HTTP (Intento {attempt + 1}/{max_retries}): {e}")
        except Exception as e: logger.exception(f"Error procesando ID norma."); return None
        if attempt < max_retries - 1: await asyncio.sleep(retry_delay); retry_delay *= 2
        else: logger.error(f"Fallaron todos los reintentos para ID norma."); return None
    return None

async def obtener_xml_ley_async(id_norma: str, client: httpx.AsyncClient) -> Optional[bytes]:
    cached_xml = cache_xml_ley.get(id_norma)
    if cached_xml: logger.info(f"XML para IdNorma '{id_norma}' obtenido desde caché."); return cached_xml
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"; logger.info(f"Consultando XML de ley con IDNorma {id_norma} en URL (async): {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}; response = await client.get(url, timeout=15, headers=headers); response.raise_for_status(); xml_content = response.content
        if xml_content: cache_xml_ley[id_norma] = xml_content
        return xml_content
    except httpx.TimeoutException: logger.warning(f"Timeout XML IdNorma {id_norma}"); return None
    except httpx.RequestError as e: logger.error(f"Error HTTP XML IdNorma {id_norma}: {e}"); return None
    except Exception as e: logger.exception(f"Error inesperado XML IdNorma {id_norma}."); return None

def extraer_articulos(xml_data: Optional[bytes]) -> List[Articulo]:
    if not xml_data: return []
    try: soup = BeautifulSoup(xml_data, "lxml-xml")
    except Exception as e: logger.exception(f"Error al parsear XML."); return []
    articulos_procesados: List[Articulo] = []
    estructuras_funcionales_tags = soup.find_all("EstructuraFuncional")
    logger.info(f"Se encontraron {len(estructuras_funcionales_tags)} etiquetas <EstructuraFuncional>.")
    if not estructuras_funcionales_tags: return []
    for i, ef_tag in enumerate(estructuras_funcionales_tags):
        tipo_parte = ef_tag.get('tipoParte', '').lower()
        id_parte_xml = ef_tag.get('idParte')
        if "artículo" not in tipo_parte and "articulo" not in tipo_parte: continue
        texto_tag = ef_tag.find("Texto")
        if not texto_tag or not texto_tag.text or not texto_tag.text.strip(): continue
        texto_completo_articulo = texto_tag.text.strip()
        match_numero_en_texto = re.match(r"^\s*((?:art(?:ículo|iculo)?s?\.?\s*)?[\w\d\s]+(?:bis|ter|quater)?)\s*([\.\-\–\—:]\s*)?(.*)", texto_completo_articulo, re.IGNORECASE | re.DOTALL)
        numero_display_extraido = "S/N"; texto_neto_articulo = texto_completo_articulo
        if match_numero_en_texto:
            display_temp = match_numero_en_texto.group(1).strip()
            display_temp = re.sub(r"^(artículo|articulo|art\.?)\s*", "", display_temp, flags=re.IGNORECASE).strip()
            numero_display_extraido = display_temp.rstrip('.-').strip()
            if match_numero_en_texto.group(3) and match_numero_en_texto.group(3).strip(): texto_neto_articulo = match_numero_en_texto.group(3).strip()
            elif numero_display_extraido != texto_completo_articulo: texto_neto_articulo = ""
        else: numero_display_extraido = texto_completo_articulo.split('.')[0].split('-')[0].strip()
        numero_id_interno = normalizar_numero_articulo_para_comparacion(numero_display_extraido)
        if numero_id_interno == "s/n_error_normalizacion" or not numero_id_interno: continue
        if not texto_neto_articulo.strip() and numero_display_extraido == texto_completo_articulo: texto_neto_articulo = numero_display_extraido
        texto_final_limpio = limpiar_texto_articulo(texto_neto_articulo) 
        if len(texto_final_limpio) > MAX_TEXT_LENGTH: 
            logger.warning(f"Artículo (display='{numero_display_extraido}', id_interno='{numero_id_interno}') texto truncado. Longitud original: {len(texto_final_limpio)}")
            texto_final_limpio = texto_final_limpio[:MAX_TEXT_LENGTH].rsplit(' ', 1)[0] + TRUNCATION_MESSAGE_TEXT
        referencias_finales = extraer_referencias_legales_mejorado(texto_final_limpio) 
        articulos_procesados.append(Articulo(
            articulo_display=numero_display_extraido.strip(), articulo_id_interno=numero_id_interno,
            texto=texto_final_limpio, referencias_legales=referencias_finales, id_parte_xml=id_parte_xml
        ))
    logger.info(f"Extracción finalizada. {len(articulos_procesados)} artículos procesados.")
    return articulos_procesados

# --- Endpoints de la API ---
import asyncio 

@app.get("/ley", response_model=LeyDetalle, summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley a consultar (ej. '21595')."), 
    articulo: Optional[str] = Query(None, description="Número o identificador del artículo a consultar (ej. '15', '1 bis', 'Primero Transitorio').")
):
    logger.info(f"INICIO /ley | numero_ley={numero_ley}, articulo={articulo if articulo else 'Todos'}")
    async with httpx.AsyncClient() as client: 
        id_norma = await obtener_id_norma_async(numero_ley, client)
        logger.info(f"ID NORMA: {id_norma}")
        if not id_norma:
            logger.error(f"No se encontró ID para la ley {numero_ley}")
            raise HTTPException(status_code=404, detail=f"No se encontró ID para la ley {numero_ley}.")
        xml_content = await obtener_xml_ley_async(id_norma, client)
        logger.info(f"XML recibido: {'Sí' if xml_content else 'No'}")
        if not xml_content:
            logger.error(f"No se pudo obtener XML para ley {numero_ley} (ID Norma: {id_norma})")
            raise HTTPException(status_code=503, detail=f"No se pudo obtener XML para ley {numero_ley} (ID Norma: {id_norma}).")
    
    articulos_data_original = extraer_articulos(xml_content) 
    logger.info(f"Artículos extraídos: {len(articulos_data_original)}")
    if not articulos_data_original: 
        logger.error(f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma})")
        raise HTTPException(status_code=404, detail=f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma}).")
    
    articulos_a_devolver: List[Articulo] = []
    nota_truncamiento_lista_resp: Optional[str] = None
    total_articulos_originales_resp = len(articulos_data_original)

    if articulo:
        articulo_buscado_norm = normalizar_numero_articulo_para_comparacion(articulo)
        logger.info(f"Artículo buscado normalizado: {articulo_buscado_norm}")
        if articulo_buscado_norm == "s/n_error_normalizacion" or not articulo_buscado_norm or articulo_buscado_norm == "s/n":
            logger.error(f"No se pudo normalizar el artículo buscado: '{articulo}'")
            raise HTTPException(status_code=400, detail=f"No se pudo normalizar el artículo buscado: '{articulo}'.")
        
        articulo_encontrado_obj: Optional[Articulo] = None
        for art_obj in articulos_data_original: 
            if art_obj.articulo_id_interno == articulo_buscado_norm:
                articulo_encontrado_obj = art_obj
                break
        
        if not articulo_encontrado_obj: 
            try:
                termino_busqueda_texto = re.escape(articulo_buscado_norm.replace("t",""))
                patron_texto = re.compile(r"\b(?:art(?:ículo|iculo)?s?\.?|art\.?|disposición|disp\.?)\s+(?:transitorio|trans\.?\s*)?" + termino_busqueda_texto + r"(?:[\sº°ªÞ,\.;:\(\)]|\b|$)", re.IGNORECASE)
            except re.error as e: 
                logger.error(f"Error interno en búsqueda textual: {e}")
                raise HTTPException(status_code=500, detail="Error interno en búsqueda textual.")
            for art_obj in articulos_data_original:
                if patron_texto.search(art_obj.texto):
                    art_obj.nota_busqueda = f"Encontrado por mención de '{articulo}' (normalizado a '{articulo_buscado_norm}') en texto."
                    articulo_encontrado_obj = art_obj
                    break 
        if articulo_encontrado_obj:
            logger.info(f"Artículo encontrado: {articulo_encontrado_obj.articulo_display}")
            articulos_a_devolver = [articulo_encontrado_obj]
        else: 
            ids_internos = [a.articulo_id_interno for a in articulos_data_original]
            displays = [a.articulo_display for a in articulos_data_original]
            logger.error(f"Artículo '{articulo}' no encontrado en la ley {numero_ley}")
            raise HTTPException(status_code=404, detail={
                "error": f"Artículo '{articulo}' (buscado como '{articulo_buscado_norm}') no encontrado.",
                "sugerencia": "Verifique número.",
                "ids_disponibles": ids_internos[:10],
                "displays_disponibles": displays[:10]
            })
    else: 
        if len(articulos_data_original) > MAX_ARTICULOS_RETURNED:
            logger.info(f"Ley {numero_ley} tiene {len(articulos_data_original)} artículos. Devolviendo los primeros {MAX_ARTICULOS_RETURNED}.")
            articulos_a_devolver = articulos_data_original[:MAX_ARTICULOS_RETURNED]
            nota_truncamiento_lista_resp = TRUNCATION_MESSAGE_LIST.replace(str(MAX_ARTICULOS_RETURNED), str(len(articulos_a_devolver))) 
        else:
            articulos_a_devolver = articulos_data_original
    
    logger.info(f"ANTES DE RETURN: {len(articulos_a_devolver)} artículos a devolver")
    return LeyDetalle(
        ley=numero_ley, 
        id_norma=id_norma, 
        articulos_totales_en_respuesta=len(articulos_a_devolver), 
        articulos=articulos_a_devolver,
        total_articulos_originales_en_ley=total_articulos_originales_resp, 
        nota_truncamiento_lista=nota_truncamiento_lista_resp
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
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}
            response = await client.get(url, timeout=10, headers=headers)
            logger.debug(f"Respuesta de /ley_html para {url} - Status: {response.status_code}")
            response_text_snippet = response.text[:2000] if response.text else "Respuesta vacía"
            logger.debug(f"Inicio del HTML recibido de /ley_html (primeros 2000 chars):\n{response_text_snippet}")
            response.raise_for_status()
        except httpx.TimeoutException: 
            error_message = f"Timeout al obtener HTML para idNorma {idNorma}, idParte {idParte} desde {url}"
            logger.error(error_message)
            raise HTTPException(status_code=504, detail=error_message)
        except httpx.RequestError as e: 
            error_message = f"Error en petición HTTP para HTML (idNorma {idNorma}, idParte {idParte}) desde {url}: {e}"
            logger.exception(error_message)
            raise HTTPException(status_code=502, detail=f"No se pudo obtener el contenido de {url}. Error: {e}")

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        # Selectores ajustados y priorizados
        selectores_posibles = [
            f"div.textoNorma[id^='p{idParte}']",      
            f"article[id='{idParte}']",  
            f"div.textoNorma[id*='{idParte}']", 
            f"div.textoArticulo[id*='{idParte}']",
            f"div[id='{idParte}']",
            f"div[id^='p{idParte}']" 
        ]
        div_contenido = None; selector_usado = "Ninguno"
        for selector_idx, selector in enumerate(selectores_posibles):
            logger.debug(f"Intentando selector HTML #{selector_idx + 1}: '{selector}'")
            try:
                div_temp = soup.select_one(selector)
                if div_temp: 
                    div_contenido = div_temp
                    selector_usado = selector
                    logger.info(f"Contenido encontrado para idParte '{idParte}' usando selector CSS '{selector}'")
                    break
                else:
                    logger.debug(f"Selector '{selector}' no encontró nada.")
            except Exception as e_selector: 
                logger.warning(f"Error al usar selector '{selector}': {e_selector}")
                continue 

        if not div_contenido: 
            logger.info(f"Selectores específicos no encontraron contenido para idParte '{idParte}'. Intentando fallback con regex en ID.")
            try:
                div_contenido = soup.find("div", id=re.compile(f".*{re.escape(idParte)}.*", re.IGNORECASE))
                if div_contenido: 
                    selector_usado = f"Fallback regex: div con id que contiene '{idParte}' (id real: {div_contenido.get('id')})"
                    logger.info(f"Contenido encontrado para idParte '{idParte}' usando {selector_usado}")
                else: 
                    error_message = f"No se encontró contenido para idParte '{idParte}' en norma '{idNorma}' con los selectores probados en {url}."
                    logger.error(error_message)
                    if len(response.text) < 20000: 
                        logger.debug(f"HTML completo donde no se encontró idParte '{idParte}':\n{response.text}")
                    else:
                        logger.debug(f"HTML (primeros 20000 chars) donde no se encontró idParte '{idParte}':\n{response.text[:20000]}")
                    raise HTTPException(status_code=404, detail=error_message) # Devolver 404 si no se encuentra
            except HTTPException as http_exc_inner: 
                raise http_exc_inner
            except Exception as e_fallback:
                logger.exception(f"Error durante el fallback de búsqueda de div_contenido para idParte '{idParte}'.")
                raise HTTPException(status_code=500, detail=f"Error interno al intentar fallback de búsqueda para idParte '{idParte}'.")
        
        texto_extraido = div_contenido.get_text(separator="\n", strip=True)
        texto_limpio_final = limpiar_texto_articulo(texto_extraido)

        if len(texto_limpio_final) > MAX_TEXT_LENGTH: 
            logger.warning(f"Texto HTML para idNorma {idNorma}, idParte {idParte} truncado. Longitud original: {len(texto_limpio_final)}")
            texto_limpio_final = texto_limpio_final[:MAX_TEXT_LENGTH].rsplit(' ', 1)[0] + TRUNCATION_MESSAGE_TEXT
        
        return ArticuloHTML(idNorma=idNorma, idParte=idParte, url_fuente=url, selector_usado=selector_usado, texto_html_extraido=texto_limpio_final)
    except HTTPException as http_exc: # Asegurarse de que las HTTPExceptions generadas se propaguen
        raise http_exc 
    except Exception as e: # Capturar cualquier otro error de parseo o extracción
        error_message = f"Error al parsear HTML o extraer texto para idNorma {idNorma}, idParte {idParte}."
        logger.exception(error_message) 
        raise HTTPException(status_code=500, detail=f"Error al procesar el contenido HTML para idParte '{idParte}': {e}")

# Ejemplo para ejecutar con Uvicorn (si este archivo se llama main.py):
# uvicorn main:app --reload --log-level debug
