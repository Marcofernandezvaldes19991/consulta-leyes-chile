from fastapi import FastAPI, HTTPException, Query
from typing import Optional, List, Any, Union 
import httpx 
from bs4 import BeautifulSoup
import re
import json
import os
import logging 
import time 
from pydantic import BaseModel, Field
from cachetools import TTLCache 

app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    description="Permite consultar artículos de leyes chilenas obteniendo datos desde LeyChile.cl. Esta versión incluye operaciones asíncronas, caché y mejoras en la extracción de datos.",
    version="1.2.0", 
)

# --- Configuración de Logging ---
logging.basicConfig(
    level=logging.DEBUG, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True 
)
logger = logging.getLogger(__name__) 

try:
    logging.getLogger("uvicorn").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.error").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.access").setLevel(logging.DEBUG)
    logger.info("Se intentó establecer el nivel de log de Uvicorn a DEBUG desde el código.")
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

# --- Constantes para Normalización ---
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

# --- Modelos Pydantic ---
class Articulo(BaseModel):
    articulo_display: str = Field(..., description="El número o identificador del artículo tal como se muestra originalmente (ej. 'Artículo 15', 'Primero Transitorio').")
    articulo_id_interno: str = Field(..., description="El identificador normalizado del artículo usado para búsquedas internas (ej. '15', 't1').")
    texto: str = Field(..., description="El contenido textual del artículo, con formato mejorado.")
    referencias_legales: List[str] = Field(default_factory=list, description="Lista de referencias precisas a otras normativas encontradas en el texto del artículo.")
    id_parte_xml: Optional[str] = Field(None, description="El atributo 'idParte' de la etiqueta <EstructuraFuncional> en el XML original, si está disponible.")
    nota_busqueda: Optional[str] = Field(None, description="Nota adicional si el artículo fue encontrado por búsqueda textual en lugar de ID exacto.")

class LeyDetalle(BaseModel):
    ley: str = Field(..., description="El número de ley consultado.")
    id_norma: str = Field(..., description="El IdNorma interno de la ley en LeyChile.cl.")
    articulos_totales: int = Field(..., description="El número total de artículos extraídos de la ley.")
    articulos: List[Articulo] = Field(..., description="La lista de artículos de la ley.")

class ArticuloHTML(BaseModel):
    idNorma: str
    idParte: str
    url_fuente: str
    selector_usado: str
    texto_html_extraido: str

# --- Funciones de Lógica de Negocio ---

def normalizar_numero_articulo_para_comparacion(num_str: Optional[str]) -> str:
    if not num_str: return "s/n"
    s = str(num_str).lower().strip()
    logger.debug(f"Normalizando: '{num_str}' -> '{s}' (inicial)")
    
    # Limpieza inicial de prefijos y sufijos comunes antes de diccionarios
    s = re.sub(r"^(artículo|articulo|art\.?|nro\.?|n[º°]|disposición|disp\.?)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.rstrip('.-').strip() # Quitar puntos o guiones comunes al final del display del número

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
        s = transitorio_match.group(2).strip().rstrip('.-').strip() # Limpiar también el número del transitorio
        logger.debug(f"Detectado transitorio: '{s_antes_trans}' -> prefijo='{prefijo_transitorio}', s='{s}'")
    
    s_antes_palabras = s
    for palabra, digito in WORDS_TO_INT.items():
        if re.search(r'\b' + re.escape(palabra) + r'\b', s): 
             s = re.sub(r'\b' + re.escape(palabra) + r'\b', digito, s)
    if s != s_antes_palabras: logger.debug(f"Después de reemplazar palabras numéricas: '{s_antes_palabras}' -> '{s}'")
    
    s_antes_ord = s
    s = re.sub(r"[º°ª\.,]", "", s) # Quitar ordinales y puntuación restante
    if s != s_antes_ord: logger.debug(f"Después de quitar ordinales/puntuación: '{s_antes_ord}' -> '{s}'")
    
    s = s.strip() # Limpieza final de espacios

    partes_numericas = re.findall(r"(\d+)\s*([a-zA-Z]*)", s)
    logger.debug(f"Partes numéricas encontradas en '{s}': {partes_numericas}")
    componentes_normalizados = []
    texto_restante = s 
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part: 
            if letra_part in ["bis", "ter", "quater"] or (len(letra_part) == 1 and letra_part.isalpha()): componente += letra_part
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
    """Limpia el texto de un artículo, mejorando el manejo de espacios y saltos de línea."""
    if not texto:
        return ""
    # Reemplazar múltiples espacios/tabs con un solo espacio
    texto_limpio = re.sub(r'[ \t]+', ' ', texto)
    # Reemplazar múltiples saltos de línea con un máximo de dos (para párrafos)
    # o uno si se prefiere más compacto. Aquí se usa uno.
    texto_limpio = re.sub(r'\n\s*\n+', '\n', texto_limpio)
    # Eliminar espacios al inicio y final de cada línea
    texto_limpio = "\n".join([line.strip() for line in texto_limpio.split('\n')])
    # Eliminar saltos de línea redundantes al inicio y final del texto completo
    return texto_limpio.strip()

def extraer_referencias_legales_mejorado(texto_articulo: str) -> List[str]:
    """Extrae referencias legales más completas y precisas del texto de un artículo."""
    if not texto_articulo:
        return []
    
    # Patrones más específicos y ordenados por prioridad o especificidad
    patrones = [
        # Referencias a artículos de la misma ley (implícito) - Difícil de capturar sin contexto, omitido por ahora.
        # Códigos completos
        r"C[óo]digo\s+(?:Penal|Tributario|Civil|del\s+Trabajo|de\s+Comercio|Procesal\s+Penal|Sanitario|de\s+Miner[íi]a|de\s+Aguas)",
        # Leyes con número
        r"ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+",
        # Decretos Leyes con número
        r"decreto\s+ley\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+",
        # Decretos con Fuerza de Ley (DFL) con número y opcionalmente año y ministerio
        r"decreto\s+con\s+fuerza\s+de\s+ley\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+(?:,\s*de\s+\d{4}(?:,\s*d[e|el]\s*Ministerio\s+d[e|el][\s\w]+)?)?",
        r"D\.F\.L\.?\s+(?:N(?:[°ºªo]|\.?)?|No\.)?\s*[\d\.]+(?:/\d+)?", # Abreviatura DFL
        r"D\.L\.?\s+(?:N(?:[°ºªo]|\.?)?|No\.)?\s*[\d\.]+",      # Abreviatura DL
        # Decretos Supremos
        r"decreto\s+supremo\s+(?:N(?:[°ºªo]|\.?)?|número)?\s*[\d\.]+(?:,\s*de\s+\d{4}(?:,\s*d[e|el]\s*Ministerio\s+d[e|el][\s\w]+)?)?",
        # Constitución
        r"Constituci[oó]n\s+Pol[íi]tica\s+de\s+la\s+Rep[úu]blica",
        # Referencias a artículos específicos de otras normas (más complejo, intentar capturar frases comunes)
        # "artículo X del Código Y", "artículo Y de la ley Z"
        r"art[íi]culo\s+[\w\d]+(?:\s*bis)?\s+d[e|el]\s+(?:la\s+)?(?:ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+|C[óo]digo\s+\w+|D\.F\.L\.?\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+|decreto\s+ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+)",
        r"inciso\s+\w+\s+del\s+art[íi]culo\s+[\w\d]+(?:\s*bis)?\s+d[e|el]\s+(?:la\s+)?(?:ley\s+N(?:[°ºªo]|\.?)?\s*[\d\.]+|C[óo]digo\s+\w+)",
    ]
    
    referencias_encontradas = set()
    # Normalizar espacios en el texto para facilitar el matcheo de regex
    texto_norm_espacios = re.sub(r'\s+', ' ', texto_articulo)

    for patron in patrones:
        try:
            # Buscar todas las ocurrencias no superpuestas
            for match_obj in re.finditer(patron, texto_norm_espacios, re.IGNORECASE):
                match_str = match_obj.group(0)
                # Limpiar la referencia: quitar puntuación inicial/final, normalizar espacios.
                ref_limpia = match_str.strip(" .,;:()-")
                if len(ref_limpia) > 5: # Filtrar referencias muy cortas o genéricas
                    referencias_encontradas.add(ref_limpia)
        except re.error as e:
            logger.error(f"Error en la expresión regular '{patron}': {e}")
            continue
            
    return sorted(list(referencias_encontradas))

async def obtener_id_norma_async(numero_ley: str, client: httpx.AsyncClient) -> Optional[str]:
    # ... (sin cambios respecto a v11)
    norm_numero_ley_buscado = numero_ley.strip().replace(".", "").replace(",", "")
    if not norm_numero_ley_buscado.isdigit(): logger.warning(f"El número de ley '{numero_ley}' no es puramente numérico.")
    cached_id = cache_id_norma.get(norm_numero_ley_buscado)
    if cached_id:
        logger.info(f"IdNorma '{cached_id}' para ley '{norm_numero_ley_buscado}' obtenido desde caché.")
        return cached_id
    if norm_numero_ley_buscado in fallback_ids:
        logger.info(f"Usando ID de fallback para ley '{norm_numero_ley_buscado}': {fallback_ids[norm_numero_ley_buscado]}")
        cache_id_norma[norm_numero_ley_buscado] = fallback_ids[norm_numero_ley_buscado]
        return fallback_ids[norm_numero_ley_buscado]
    if not norm_numero_ley_buscado.isdigit():
        logger.warning(f"Número de ley '{norm_numero_ley_buscado}' no es numérico y no se encontró en fallbacks.")
        return None
    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{norm_numero_ley_buscado}"
    logger.info(f"Consultando URL para ID de norma (async): {url}")
    max_retries = 3; retry_delay = 1
    for attempt in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}
            response = await client.get(url, timeout=10, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "xml")
            normas = soup.find_all("Norma")
            if not normas: return None
            for norma in normas:
                numero_norma_tag = norma.find("Numero")
                if numero_norma_tag and numero_norma_tag.text:
                    numero_norma_en_xml = numero_norma_tag.text.strip().replace(".", "").replace(",", "")
                    if numero_norma_en_xml == norm_numero_ley_buscado:
                        id_norma_tag = norma.find("IdNorma")
                        if id_norma_tag and id_norma_tag.text:
                            id_encontrado = id_norma_tag.text.strip()
                            cache_id_norma[norm_numero_ley_buscado] = id_encontrado
                            return id_encontrado
            return None
        except httpx.TimeoutException: logger.warning(f"Timeout (Intento {attempt + 1}/{max_retries})")
        except httpx.RequestError as e: logger.error(f"Error HTTP (Intento {attempt + 1}/{max_retries}): {e}")
        except Exception as e: logger.exception(f"Error procesando ID norma."); return None
        if attempt < max_retries - 1: await asyncio.sleep(retry_delay); retry_delay *= 2
        else: logger.error(f"Fallaron todos los reintentos para ID norma."); return None
    return None

async def obtener_xml_ley_async(id_norma: str, client: httpx.AsyncClient) -> Optional[bytes]:
    # ... (sin cambios respecto a v11)
    cached_xml = cache_xml_ley.get(id_norma)
    if cached_xml:
        logger.info(f"XML para IdNorma '{id_norma}' obtenido desde caché.")
        return cached_xml
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    logger.info(f"Consultando XML de ley con IDNorma {id_norma} en URL (async): {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}
        response = await client.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        xml_content = response.content
        if xml_content: cache_xml_ley[id_norma] = xml_content
        return xml_content
    except httpx.TimeoutException: logger.warning(f"Timeout XML IdNorma {id_norma}"); return None
    except httpx.RequestError as e: logger.error(f"Error HTTP XML IdNorma {id_norma}: {e}"); return None
    except Exception as e: logger.exception(f"Error inesperado XML IdNorma {id_norma}."); return None

def extraer_articulos(xml_data: Optional[bytes]) -> List[Articulo]:
    # ... (la lógica principal de extracción de artículos se mantiene igual que en v11, 
    #      pero ahora usa las funciones de limpieza y extracción de referencias mejoradas)
    # Asegúrate de copiar la función completa de la v11 aquí, solo cambiando las llamadas a las funciones auxiliares.
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
        
        texto_final_limpio = limpiar_texto_articulo(texto_neto_articulo) # Usar la función de limpieza mejorada
        referencias_finales = extraer_referencias_legales_mejorado(texto_final_limpio) # Usar la función de referencias mejorada

        articulos_procesados.append(Articulo(
            articulo_display=numero_display_extraido.strip(), articulo_id_interno=numero_id_interno,
            texto=texto_final_limpio, referencias_legales=referencias_finales, id_parte_xml=id_parte_xml
        ))
    logger.info(f"Extracción finalizada. {len(articulos_procesados)} artículos procesados.")
    return articulos_procesados

# --- Endpoints de la API ---
import asyncio 

@app.get("/ley", response_model=Union[Articulo, LeyDetalle], summary="Consultar Ley por Número y Artículo (Opcional)")
async def consultar_ley(
    numero_ley: str = Query(..., description="Número de la ley a consultar (ej. '21595')."), 
    articulo: Optional[str] = Query(None, description="Número o identificador del artículo a consultar (ej. '15', '1 bis', 'Primero Transitorio').")
):
    # ... (el endpoint consultar_ley sigue igual que en v11 - Corregido, se omite aquí por brevedad)
    # Asegúrate de copiar la función completa de la v11 - Corregido aquí.
    logger.info(f"Recibida consulta para ley: {numero_ley}, artículo: {articulo if articulo else 'Todos'}")
    async with httpx.AsyncClient() as client: 
        id_norma = await obtener_id_norma_async(numero_ley, client)
        if not id_norma: raise HTTPException(status_code=404, detail=f"No se encontró ID para la ley {numero_ley}.")
        xml_content = await obtener_xml_ley_async(id_norma, client)
        if not xml_content: raise HTTPException(status_code=503, detail=f"No se pudo obtener XML para ley {numero_ley} (ID Norma: {id_norma}).")
    articulos_data = extraer_articulos(xml_content) 
    if not articulos_data: raise HTTPException(status_code=404, detail=f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma}).")
    if articulo:
        articulo_buscado_norm = normalizar_numero_articulo_para_comparacion(articulo)
        if articulo_buscado_norm == "s/n_error_normalizacion" or not articulo_buscado_norm or articulo_buscado_norm == "s/n":
            raise HTTPException(status_code=400, detail=f"No se pudo normalizar el artículo buscado: '{articulo}'.")
        for art_obj in articulos_data:
            if art_obj.articulo_id_interno == articulo_buscado_norm: return art_obj
        try:
            termino_busqueda_texto = re.escape(articulo_buscado_norm.replace("t",""))
            patron_texto = re.compile(r"\b(?:art(?:ículo|iculo)?s?\.?|art\.?|disposición|disp\.?)\s+(?:transitorio|trans\.?\s*)?" + termino_busqueda_texto + r"(?:[\sº°ªÞ,\.;:\(\)]|\b|$)", re.IGNORECASE)
        except re.error as e: raise HTTPException(status_code=500, detail="Error interno en búsqueda textual.")
        for art_obj in articulos_data:
            if patron_texto.search(art_obj.texto):
                art_obj.nota_busqueda = f"Encontrado por mención de '{articulo}' (normalizado a '{articulo_buscado_norm}') en texto."
                return art_obj
        ids_internos = [a.articulo_id_interno for a in articulos_data]; displays = [a.articulo_display for a in articulos_data]
        raise HTTPException(status_code=404, detail={"error": f"Artículo '{articulo}' (buscado como '{articulo_buscado_norm}') no encontrado.", "sugerencia": "Verifique número.", "ids_disponibles": ids_internos[:10], "displays_disponibles": displays[:10]})
    return LeyDetalle(ley=numero_ley, id_norma=id_norma, articulos_totales=len(articulos_data), articulos=articulos_data)


@app.get("/ley_html", response_model=ArticuloHTML, summary="Consultar Artículo por idNorma e idParte (HTML)")
async def consultar_articulo_html(
    idNorma: str = Query(..., description="El IdNorma de la ley."), 
    idParte: str = Query(..., description="El idParte específico del artículo o sección.")
):
    # ... (el endpoint consultar_articulo_html sigue igual que en v11, se omite aquí por brevedad)
    # Asegúrate de copiar la función completa de la v11 aquí.
    logger.info(f"Consultando HTML desde bcn.cl para idNorma: {idNorma}, idParte: {idParte}")
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    async with httpx.AsyncClient() as client:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}
            response = await client.get(url, timeout=10, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException: raise HTTPException(status_code=504, detail=f"Timeout al obtener HTML desde {url}")
        except httpx.RequestError as e: raise HTTPException(status_code=502, detail=f"Error HTTP al obtener HTML desde {url}: {e}")
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        selectores_posibles = [f"div#p{idParte}", f"article#{idParte}", f"div.textoNorma[id*='{idParte}']", f"div.textoArticulo[id*='{idParte}']", f"div[id='{idParte}']"]
        div_contenido = None; selector_usado = "Ninguno"
        for selector in selectores_posibles:
            div_temp = soup.select_one(selector)
            if div_temp: div_contenido = div_temp; selector_usado = selector; break
        if not div_contenido:
            div_contenido = soup.find("div", id=re.compile(f".*{re.escape(idParte)}.*", re.IGNORECASE))
            if div_contenido: selector_usado = f"Fallback regex: div con id que contiene '{idParte}'"
            else: raise HTTPException(status_code=404, detail=f"No se encontró contenido para idParte '{idParte}'.")
        texto_extraido = div_contenido.get_text(separator="\n", strip=True)
        texto_limpio_final = limpiar_texto_articulo(texto_extraido)
        return ArticuloHTML(idNorma=idNorma, idParte=idParte, url_fuente=url, selector_usado=selector_usado, texto_html_extraido=texto_limpio_final)
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error al procesar HTML para idParte '{idParte}': {e}")

# Ejemplo para ejecutar con Uvicorn (si este archivo se llama main.py):
# uvicorn main:app --reload --log-level debug
