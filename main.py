from fastapi import FastAPI
from typing import Optional, List, Dict, Any
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import logging 
import time 

app = FastAPI()

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

def normalizar_numero_articulo_para_comparacion(num_str: Optional[str]) -> str:
    if not num_str:
        return "s/n"
    s = str(num_str).lower().strip()
    logger.debug(f"Normalizando: '{num_str}' -> '{s}' (inicial)")
    if s in WORDS_TO_INT:
        logger.debug(f"Normalizado por WORDS_TO_INT: '{s}' -> '{WORDS_TO_INT[s]}'")
        return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:
        logger.debug(f"Normalizado por ROMAN_TO_INT: '{s}' -> '{str(ROMAN_TO_INT[s])}'")
        return str(ROMAN_TO_INT[s])
    s_original_prefijos = s
    s = re.sub(r"^(artículo|articulo|art\.?|nro\.?|n[º°]|disposición|disp\.?)\s*", "", s, flags=re.IGNORECASE)
    if s != s_original_prefijos: logger.debug(f"Después de quitar prefijos: '{s_original_prefijos}' -> '{s}'")
    transitorio_match = re.match(r"^(transitorio|trans\.?|t)\s*(.*)", s, flags=re.IGNORECASE)
    prefijo_transitorio = ""
    if transitorio_match:
        prefijo_transitorio = "t" 
        s_antes_trans = s
        s = transitorio_match.group(2).strip()
        logger.debug(f"Detectado transitorio: '{s_antes_trans}' -> prefijo='{prefijo_transitorio}', s='{s}'")
    s_antes_palabras = s
    for palabra, digito in WORDS_TO_INT.items():
        if re.search(r'\b' + re.escape(palabra) + r'\b', s): 
             s = re.sub(r'\b' + re.escape(palabra) + r'\b', digito, s)
    if s != s_antes_palabras: logger.debug(f"Después de reemplazar palabras numéricas: '{s_antes_palabras}' -> '{s}'")
    s_antes_ord = s
    s = re.sub(r"[º°ª\.,]", "", s)
    if s != s_antes_ord: logger.debug(f"Después de quitar ordinales/puntuación: '{s_antes_ord}' -> '{s}'")
    partes_numericas = re.findall(r"(\d+)\s*([a-zA-Z]*)", s)
    logger.debug(f"Partes numéricas encontradas en '{s}': {partes_numericas}")
    componentes_normalizados = []
    texto_restante = s 
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part: 
            if letra_part == "bis": componente += "bis"
            elif letra_part == "ter": componente += "ter"
            elif letra_part == "quater": componente += "quater"
            elif len(letra_part) == 1 and letra_part.isalpha(): componente += letra_part
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

def obtener_id_norma(numero_ley: str) -> Optional[str]:
    norm_numero_ley_buscado = numero_ley.strip().replace(".", "").replace(",", "") 
    if not norm_numero_ley_buscado.isdigit():
        logger.warning(f"El número de ley '{numero_ley}' (normalizado a '{norm_numero_ley_buscado}') no parece ser un número válido.")
    if norm_numero_ley_buscado in fallback_ids:
        logger.info(f"Usando ID de fallback para ley '{norm_numero_ley_buscado}': {fallback_ids[norm_numero_ley_buscado]}")
        return fallback_ids[norm_numero_ley_buscado]
    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{norm_numero_ley_buscado}"
    logger.info(f"Consultando URL para ID de norma: {url}")
    max_retries = 3
    retry_delay = 1
    response = None
    for attempt in range(max_retries):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'} 
            response = requests.get(url, timeout=10, headers=headers)
            response.raise_for_status()
            break 
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout al buscar ID para ley {norm_numero_ley_buscado} (Intento {attempt + 1}/{max_retries})")
        except requests.exceptions.RequestException as e:
            logger.error(f"Error en petición HTTP para ID de norma {norm_numero_ley_buscado} (Intento {attempt + 1}/{max_retries}): {e}")
        if attempt < max_retries - 1:
            logger.info(f"Reintentando en {retry_delay} segundos...")
            time.sleep(retry_delay)
            retry_delay *= 2
        else:
            logger.error(f"No se pudo obtener el ID para la ley {norm_numero_ley_buscado} después de {max_retries} intentos.")
            return None
    if response is None: return None
    try:
        soup = BeautifulSoup(response.content, "xml") 
        normas = soup.find_all("Norma")
        if not normas:
            logger.warning(f"No se encontraron etiquetas <Norma> para ley {norm_numero_ley_buscado}. XML (primeros 500b): {response.content[:500].decode('utf-8', 'ignore')}")
            return None
        for norma in normas:
            numero_norma_tag = norma.find("Numero")
            if numero_norma_tag and numero_norma_tag.text:
                numero_norma_en_xml = numero_norma_tag.text.strip().replace(".", "").replace(",", "")
                if numero_norma_en_xml == norm_numero_ley_buscado:
                    id_norma_tag = norma.find("IdNorma")
                    if id_norma_tag and id_norma_tag.text:
                        id_encontrado = id_norma_tag.text.strip()
                        titulo_tag_debug = norma.find("Titulo")
                        titulo_debug_text = titulo_tag_debug.text.strip() if titulo_tag_debug and titulo_tag_debug.text else "N/A"
                        logger.info(f"ID encontrado para ley '{norm_numero_ley_buscado}' (N° XML: '{numero_norma_en_xml}'): {id_encontrado}. Título: '{titulo_debug_text}'")
                        return id_encontrado
                    else:
                        logger.warning(f"Coincidencia de número de ley '{numero_norma_en_xml}' para '{norm_numero_ley_buscado}', pero no se encontró IdNorma.")
        logger.warning(f"No se encontró un ID de norma coincidente para la ley '{norm_numero_ley_buscado}' en {len(normas)} normas evaluadas.\nXML (primeros 500b): {response.content[:500].decode('utf-8', 'ignore')}")
        return None
    except Exception as e:
        logger.exception(f"Error al parsear XML para obtener ID de norma ({norm_numero_ley_buscado}).")
        return None

def obtener_xml_ley(id_norma: str) -> Optional[bytes]:
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    logger.info(f"Consultando XML de ley con IDNorma {id_norma} en URL: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'} 
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        logger.debug(f"XML de ley obtenido para IDNorma {id_norma}. Content-Type: {response.headers.get('Content-Type')}. Tamaño: {len(response.content)} bytes.")
        if response.content: 
            logger.debug(f"Inicio del XML de la ley (primeros 1000 caracteres):\n{response.content[:1000].decode('utf-8', 'ignore')}")
        return response.content
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout al obtener XML para IDNorma {id_norma}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error en petición HTTP para XML de ley (IDNorma {id_norma}): {e}")
        return None

def extraer_articulos(xml_data: Optional[bytes]) -> List[Dict[str, Any]]:
    if not xml_data:
        logger.warning("No hay datos XML para extraer artículos (xml_data es None).")
        return []
    try:
        soup = BeautifulSoup(xml_data, "lxml-xml") 
    except Exception as e: 
        logger.exception(f"Error al parsear XML de la ley con BeautifulSoup (lxml-xml).")
        return []
        
    resultado = []
    
    # Buscar todas las etiquetas <EstructuraFuncional>
    # El atributo tipoParte="Artículo" (o con tilde) identificará los artículos.
    estructuras_funcionales_tags = soup.find_all("EstructuraFuncional")
    logger.info(f"Se encontraron {len(estructuras_funcionales_tags)} etiquetas <EstructuraFuncional> en el XML.")

    if not estructuras_funcionales_tags:
        logger.warning("No se encontraron etiquetas <EstructuraFuncional>. No se pueden extraer artículos.")
        logger.debug(f"XML completo (o primeros 5000b) donde no se encontraron EstructuraFuncional:\n{xml_data[:5000].decode('utf-8', 'ignore')}")
        return []

    # Loggear las primeras N etiquetas <EstructuraFuncional> para inspección
    for i_debug, ef_tag_debug in enumerate(estructuras_funcionales_tags[:3]): # Loggear las primeras 3
        logger.debug(f"Contenido de la etiqueta <EstructuraFuncional> XML #{i_debug+1} (primeros 500 chars):\n{str(ef_tag_debug)[:500]}")

    for i, ef_tag in enumerate(estructuras_funcionales_tags):
        tipo_parte = ef_tag.get('tipoParte', '').lower()
        id_parte_xml = ef_tag.get('idParte', 'N/A')

        # Solo procesar si es un artículo (ser flexible con la tilde en "Artículo")
        if "artículo" not in tipo_parte and "articulo" not in tipo_parte:
            logger.debug(f"Omitiendo EstructuraFuncional XML #{i+1} (idParte: {id_parte_xml}) porque tipoParte='{ef_tag.get('tipoParte', '')}' no es 'Artículo'.")
            continue

        logger.debug(f"Procesando EstructuraFuncional XML #{i+1} (idParte: {id_parte_xml}, tipoParte='{ef_tag.get('tipoParte', '')}') como un Artículo.")

        texto_tag = ef_tag.find("Texto") # Buscar la etiqueta <Texto> hija de esta <EstructuraFuncional>
        if not texto_tag or not texto_tag.text or not texto_tag.text.strip():
            logger.warning(f"EstructuraFuncional tipo Artículo XML #{i+1} (idParte: {id_parte_xml}) no tiene etiqueta <Texto> o está vacía. Se omite.")
            continue
        
        texto_completo_articulo = texto_tag.text.strip()
        logger.debug(f"Texto completo extraído de <Texto> para EF #{i+1} (idParte: {id_parte_xml}): '{texto_completo_articulo[:200]}...'")

        # Extraer el número del artículo y el texto neto del artículo
        # Patrón para capturar el encabezado del artículo (ej. "Artículo 1.- ", "Art. 15 ", "Primero Transitorio.- ")
        # Grupo 1: Todo el encabezado del número (ej. "Artículo 1", "Art. 15", "Primero Transitorio")
        # Grupo 2: El delimitador opcional (ej. ".-", ". ", "- ")
        # Grupo 3: El resto del texto
        match_numero_en_texto = re.match(r"^\s*((?:art(?:ículo|iculo)?s?\.?\s*)?[\w\d\s]+(?:bis|ter|quater)?)\s*([\.\-\–\—:]\s*)?(.*)", texto_completo_articulo, re.IGNORECASE | re.DOTALL)
        
        numero_display_extraido = "S/N"
        texto_neto_articulo = texto_completo_articulo # Por defecto, si no se puede separar

        if match_numero_en_texto:
            numero_display_extraido = match_numero_en_texto.group(1).strip()
            # Si el grupo 3 (resto del texto) existe y no está vacío, ese es el texto neto.
            if match_numero_en_texto.group(3) and match_numero_en_texto.group(3).strip():
                 texto_neto_articulo = match_numero_en_texto.group(3).strip()
            # Si no hay grupo 3, o está vacío, pero el grupo 1 (número) es diferente del texto completo,
            # significa que el texto completo podría ser solo el "número" (ej. "Artículo Final")
            elif numero_display_extraido != texto_completo_articulo:
                 texto_neto_articulo = "" # No hay texto después del número.
            # else: el texto completo es el número, texto_neto_articulo ya es texto_completo_articulo
            
            logger.debug(f"EF #{i+1}: NumeroDisplay extraído del texto: '{numero_display_extraido}', Texto neto (inicio): '{texto_neto_articulo[:100]}...'")
        else:
            logger.warning(f"EF #{i+1}: No se pudo extraer un número de artículo del inicio del texto: '{texto_completo_articulo[:100]}...'. Se usará 'S/N' para el display y se intentará normalizar el texto completo.")
            # Intentar normalizar todo el texto si no se pudo separar el número.
            numero_display_extraido = texto_completo_articulo # Para que la normalización intente con todo el texto.

        numero_id_interno = normalizar_numero_articulo_para_comparacion(numero_display_extraido)

        if numero_id_interno == "s/n_error_normalizacion" or not numero_id_interno :
            logger.warning(f"EF Artículo XML #{i+1} (Display extraído='{numero_display_extraido}', idParte: {id_parte_xml}): Normalización fallida. Se omite.")
            continue
        if numero_id_interno == "s/n" and numero_display_extraido != "S/N":
             logger.warning(f"EF Artículo XML #{i+1} (Display extraído='{numero_display_extraido}', idParte: {id_parte_xml}): Normalización resultó en 's/n'. Revisar.")
        
        # Si después de extraer el número, el texto_neto_articulo quedó vacío, 
        # y el numero_display_extraido es igual al texto_completo_articulo,
        # significa que el "artículo" era solo su título/número (ej. "ARTICULO FINAL"). En este caso, el texto es el mismo display.
        if not texto_neto_articulo.strip() and numero_display_extraido == texto_completo_articulo:
            texto_neto_articulo = numero_display_extraido
            logger.debug(f"EF #{i+1}: El texto neto estaba vacío y el display era el texto completo. Usando display como texto: '{texto_neto_articulo[:100]}...'")


        logger.debug(f"EF Artículo XML #{i+1} (Display extraído='{numero_display_extraido}', idParte: {id_parte_xml}): IDInterno='{numero_id_interno}'. Texto final para el artículo (inicio): '{texto_neto_articulo[:100]}...'")

        patron_referencias = r"(?:Ley|Decreto\s+(?:con\s+Fuerza\s+de\s+Ley|Ley|Supremo)|D\.F\.L\.?|D\.L\.?|L\.?)\s*(?:N(?:[°ºªo]|\.?)|\bnúmeros?\b)?\s*([\w\d\.-]+(?:/\d{2,4})?)"
        referencias_encontradas_tuplas = re.findall(patron_referencias, texto_neto_articulo, re.IGNORECASE) # Buscar referencias en el texto neto
        referencias_limpias = list(set(ref.strip(" .-") for ref in referencias_encontradas_tuplas if ref.strip(" .-")))

        resultado.append({
            "articulo_display": numero_display_extraido.strip(), # El número/título extraído del texto
            "articulo_id_interno": numero_id_interno,
            "texto": texto_neto_articulo.strip(), # El cuerpo del artículo
            "referencias_legales": referencias_limpias,
            "id_parte_xml": id_parte_xml 
        })
    
    logger.info(f"Extracción finalizada. {len(resultado)} artículos procesados y añadidos.")
    if not resultado and xml_data and len(estructuras_funcionales_tags) > 0 :
        logger.warning("Se encontraron etiquetas <EstructuraFuncional> pero no se extrajo ningún artículo válido. Revisar lógica de normalización o extracción de texto.")
    return resultado

@app.get("/ley")
def consultar_ley(numero_ley: str, articulo: Optional[str] = None):
    logger.info(f"Recibida consulta para ley: {numero_ley}, artículo: {articulo if articulo else 'Todos'}")
    id_norma = obtener_id_norma(numero_ley)
    if not id_norma:
        return {"error": f"No se encontró ID para la ley {numero_ley}. Verifique el número o si la ley está disponible en leychile.cl."}
    xml_content = obtener_xml_ley(id_norma)
    if not xml_content:
        return {"error": f"No se pudo obtener el contenido XML para la ley {numero_ley} (ID Norma: {id_norma})."}
    articulos_data = extraer_articulos(xml_content) 
    if not articulos_data: 
        logger.warning(f"La función extraer_articulos devolvió una lista vacía para la ley {numero_ley} (ID Norma: {id_norma}).")
        return {"error": f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma}). El XML podría estar vacío, no tener artículos válidos, o no tener el formato esperado."}
    if articulo:
        articulo_buscado_norm = normalizar_numero_articulo_para_comparacion(articulo)
        logger.info(f"Buscando artículo '{articulo}' (normalizado a '{articulo_buscado_norm}') en ley {numero_ley} (ID Norma: {id_norma}).")
        if articulo_buscado_norm == "s/n_error_normalizacion" or not articulo_buscado_norm or articulo_buscado_norm == "s/n":
            logger.error(f"Error de normalización para el artículo buscado: '{articulo}'. No se puede proceder con la búsqueda.")
            return {"error": f"No se pudo normalizar el número de artículo buscado: '{articulo}'. Intente con un formato más simple (ej. '15', '15bis', 't1', 'Final')."}
        articulos_encontrados_exactos = []
        for art_obj in articulos_data:
            if art_obj["articulo_id_interno"] == articulo_buscado_norm:
                logger.debug(f"Coincidencia exacta de ID interno: Buscado='{articulo_buscado_norm}', Encontrado='{art_obj['articulo_id_interno']}' para Display='{art_obj['articulo_display']}'")
                articulos_encontrados_exactos.append(art_obj)
        if articulos_encontrados_exactos:
            if len(articulos_encontrados_exactos) == 1:
                logger.info(f"Artículo '{articulo}' (normalizado '{articulo_buscado_norm}') encontrado por ID exacto.")
                return articulos_encontrados_exactos[0]
            else: 
                logger.warning(f"Múltiples artículos ({len(articulos_encontrados_exactos)}) coinciden con el ID interno normalizado '{articulo_buscado_norm}'. Devolviendo el primero.")
                return articulos_encontrados_exactos[0]
        logger.info(f"Artículo '{articulo_buscado_norm}' no encontrado por ID exacto. Intentando búsqueda textual.")
        try:
            termino_busqueda_texto = re.escape(articulo_buscado_norm.replace("t",""))
            patron_texto = re.compile(
                r"\b(?:art(?:ículo|iculo)?s?\.?|art\.?|disposición|disp\.?)\s+" 
                + r"(?:transitorio|trans\.?\s*)?" 
                + termino_busqueda_texto
                + r"(?:[\sº°ªÞ,\.;:\(\)]|\b|$)", 
                re.IGNORECASE
            )
            logger.debug(f"Patrón de búsqueda textual: {patron_texto.pattern}")
        except re.error as e:
            logger.exception(f"Error al compilar regex para búsqueda textual de artículo '{articulo_buscado_norm}'.")
            return {"error": f"Error interno al procesar la búsqueda del artículo '{articulo}'."}
        articulos_coincidentes_texto = []
        for art_obj in articulos_data:
            if patron_texto.search(art_obj["texto"]): # Buscar en el texto neto del artículo
                logger.debug(f"Coincidencia textual encontrada para '{articulo_buscado_norm}' en artículo display '{art_obj['articulo_display']}' (ID interno '{art_obj['articulo_id_interno']}')")
                art_obj_copia = art_obj.copy() 
                art_obj_copia["nota_busqueda"] = f"Artículo encontrado por mención de '{articulo}' (normalizado a '{articulo_buscado_norm}') en su texto. El número formal del artículo es '{art_obj['articulo_display']}'."
                articulos_coincidentes_texto.append(art_obj_copia)
        if articulos_coincidentes_texto:
            if len(articulos_coincidentes_texto) == 1:
                 logger.info(f"Artículo '{articulo}' (normalizado '{articulo_buscado_norm}') encontrado por búsqueda textual.")
                 return articulos_coincidentes_texto[0]
            else:
                logger.warning(f"Múltiples artículos ({len(articulos_coincidentes_texto)}) mencionan textualmente '{articulo}'. Devolviendo el primero.")
                return {
                    "advertencia": f"Se encontraron {len(articulos_coincidentes_texto)} artículos que mencionan textualmente '{articulo}'. Se devuelve el primero de ellos.",
                    "articulo_encontrado": articulos_coincidentes_texto[0],
                    "otros_articulos_con_menciones_similares": [a["articulo_display"] for a in articulos_coincidentes_texto]
                }
        logger.warning(f"Artículo '{articulo}' (buscado como '{articulo_buscado_norm}') no encontrado en ley {numero_ley} (ID Norma: {id_norma}).")
        ids_internos_disponibles = [a["articulo_id_interno"] for a in articulos_data]
        logger.debug(f"IDs internos de artículos extraídos de la ley {numero_ley} (ID Norma: {id_norma}): {ids_internos_disponibles}")
        return {
            "error": f"Artículo '{articulo}' (buscado como '{articulo_buscado_norm}') no encontrado ni por ID exacto ni por mención textual en la ley {numero_ley}.",
            "sugerencia": "Verifique el número del artículo. Pruebe formatos como '15', '15bis', 'Primero Transitorio', 'Final'. También puede intentar sin especificar un artículo para ver todos los artículos disponibles.",
            "articulos_disponibles_ids_internos_muestra": ids_internos_disponibles[:20], 
            "articulos_disponibles_display_muestra": [a["articulo_display"] for a in articulos_data[:20]] 
        }
    logger.info(f"Devolviendo todos los {len(articulos_data)} artículos para la ley {numero_ley} (ID Norma: {id_norma}).")
    return {"ley": numero_ley, "id_norma": id_norma, "articulos_totales": len(articulos_data), "articulos": articulos_data}

@app.get("/ley_html")
def consultar_articulo_html(idNorma: str, idParte: str):
    logger.info(f"Consultando HTML desde bcn.cl para idNorma: {idNorma}, idParte: {idParte}")
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'}
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        error_message = f"Timeout al obtener HTML para idNorma {idNorma}, idParte {idParte} desde {url}"
        logger.error(error_message)
        return {"error": error_message}
    except requests.exceptions.RequestException as e:
        error_message = f"Error en petición HTTP para HTML (idNorma {idNorma}, idParte {idParte}) desde {url}: {e}"
        logger.exception(error_message)
        return {"error": f"No se pudo obtener el contenido de {url}. Error: {e}"}
    try:
        soup = BeautifulSoup(response.text, "html.parser")
        selectores_posibles = [
            f"div#p{idParte}", f"article#{idParte}", f"div.textoNorma[id*='{idParte}']", 
            f"div.textoArticulo[id*='{idParte}']", f"div[id='{idParte}']"         
        ]
        div_contenido = None
        selector_usado = "Ninguno"
        for selector in selectores_posibles:
            div_temp = soup.select_one(selector) 
            if div_temp:
                div_contenido = div_temp
                selector_usado = selector
                logger.info(f"Contenido encontrado para idParte '{idParte}' usando selector CSS '{selector}'")
                break
        if not div_contenido:
            div_contenido = soup.find("div", id=re.compile(f".*{re.escape(idParte)}.*", re.IGNORECASE))
            if div_contenido:
                selector_usado = f"Fallback regex: div con id que contiene '{idParte}' (id real: {div_contenido.get('id')})"
                logger.info(f"Contenido encontrado para idParte '{idParte}' usando {selector_usado}")
            else:
                error_message = f"No se encontró contenido para idParte '{idParte}' en norma '{idNorma}' con los selectores probados en {url}."
                logger.error(error_message)
                return {"error": f"No se encontró el elemento de contenido específico para idParte '{idParte}' en la página de la norma '{idNorma}'."}
        texto_extraido = div_contenido.get_text(separator="\n", strip=True)
        logger.info(f"Texto extraído exitosamente para idNorma {idNorma}, idParte {idParte}.")
        return {
            "idNorma": idNorma, "idParte": idParte, "url_fuente": url,
            "selector_usado": selector_usado, "texto_html_extraido": texto_extraido
        }
    except Exception as e:
        error_message = f"Error al parsear HTML o extraer texto para idNorma {idNorma}, idParte {idParte}."
        logger.exception(error_message) 
        return {"error": f"Error al procesar el contenido HTML para idParte '{idParte}'. Detalle: {e}"}

# uvicorn main:app --reload --log-level debug
