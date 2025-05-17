from fastapi import FastAPI
from typing import Optional, List, Dict, Any
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import logging # Importado para el logging
import time # Importado para la lógica de reintentos

app = FastAPI()

# Configurar logging
# El formato incluye timestamp, nivel de log y mensaje.
# Para ver los mensajes DEBUG, asegúrate de que tu servidor (ej. Uvicorn) esté configurado para mostrar ese nivel.
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Crear una instancia del logger

# Cargar fallback de ID de normas
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
except Exception as e: # Captura más genérica para errores inesperados durante la carga
    fallback_ids = {}
    logger.exception(f"Ocurrió un error inesperado al cargar 'fallbacks.json'.") # logger.exception incluye el stack trace

# Diccionarios para ayudar en la normalización de números de artículo
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
    """
    Normaliza un número de artículo para facilitar la comparación interna.
    """
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
    if s != s_original_prefijos:
        logger.debug(f"Después de quitar prefijos: '{s_original_prefijos}' -> '{s}'")
    
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
    if s != s_antes_palabras:
        logger.debug(f"Después de reemplazar palabras numéricas: '{s_antes_palabras}' -> '{s}'")

    s_antes_ord = s
    s = re.sub(r"[º°ª\.,]", "", s)
    if s != s_antes_ord:
        logger.debug(f"Después de quitar ordinales/puntuación: '{s_antes_ord}' -> '{s}'")

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

    if not componentes_normalizados and texto_restante: # Si aún queda texto y no se identificó nada más
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
    """
    Obtiene el IdNorma desde leychile.cl para un número de ley dado.
    """
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
    
    if response is None:
        return None

    try:
        soup = BeautifulSoup(response.content, "xml") # No especificar encoding aquí, dejar que BS lo detecte del XML
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
    """Obtiene el contenido XML de una ley dado su IdNorma."""
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    logger.info(f"Consultando XML de ley con IDNorma {id_norma} en URL: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0'} 
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        logger.debug(f"XML de ley obtenido para IDNorma {id_norma}. Content-Type: {response.headers.get('Content-Type')}. Tamaño: {len(response.content)} bytes.")
        return response.content
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout al obtener XML para IDNorma {id_norma}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error en petición HTTP para XML de ley (IDNorma {id_norma}): {e}")
        return None


def extraer_articulos(xml_data: Optional[bytes]) -> List[Dict[str, Any]]:
    """Extrae los artículos y sus detalles desde el contenido XML de una ley."""
    if not xml_data:
        logger.warning("No hay datos XML para extraer artículos.")
        return []
    try:
        # Dejar que BeautifulSoup intente detectar el encoding del XML si es posible
        # o usar 'lxml-xml' que es más robusto. Si se usa 'xml', from_encoding es útil.
        soup = BeautifulSoup(xml_data, "lxml-xml") # Usar lxml-xml para mejor manejo de XML
    except Exception as e: 
        logger.exception(f"Error al parsear XML de la ley con BeautifulSoup.")
        return []
        
    resultado = []
    # Buscar <Articulo> o <articulo> para ser más flexible con la capitalización, aunque el XML suele ser estricto.
    articulos_tags = soup.find_all(re.compile(r'^Articulo$', re.IGNORECASE))
    logger.info(f"Se encontraron {len(articulos_tags)} etiquetas <Articulo> (o similar) en el XML.")

    if not articulos_tags:
        return []

    for i, art_tag in enumerate(articulos_tags):
        id_parte_articulo = art_tag.get('idParte', 'N/A')

        numero_tag = art_tag.find("Numero")
        numero_display = numero_tag.text.strip() if numero_tag and numero_tag.text else "S/N"
        numero_id_interno = normalizar_numero_articulo_para_comparacion(numero_display)
        
        logger.debug(f"Procesando Artículo XML #{i+1} (idParte: {id_parte_articulo}): Display='{numero_display}', IDInterno='{numero_id_interno}'")

        if numero_id_interno == "s/n_error_normalizacion" or not numero_id_interno :
            logger.warning(f"Artículo XML #{i+1} (Display='{numero_display}', idParte: {id_parte_articulo}): Normalización fallida. Se omite.")
            continue
        if numero_id_interno == "s/n" and numero_display != "S/N":
             logger.warning(f"Artículo XML #{i+1} (Display='{numero_display}', idParte: {id_parte_articulo}): Normalización resultó en 's/n'. Revisar.")

        texto_articulo = None
        campo_texto_usado = "Ninguno"
        # El orden de prueba de estas etiquetas puede ser importante.
        campos_posibles_texto = ["Texto", "TextoArticulo", "Cuerpo", "Contenido", "Text"] # Añadido "Text" por si acaso
        for campo_nombre in campos_posibles_texto:
            tag_texto = art_tag.find(campo_nombre) 
            if tag_texto and tag_texto.text and tag_texto.text.strip():
                texto_articulo = tag_texto.text.strip()
                campo_texto_usado = campo_nombre
                break
        
        if not texto_articulo: 
            texto_bruto_articulo = art_tag.get_text(separator=" ", strip=True)
            campo_texto_usado = "get_text() fallback"
            if numero_display != "S/N":
                # Limpieza de prefijo más robusta
                patron_inicio_articulo = r"^(?:\s*" + re.escape(numero_display) + r"\s*(?:[\.\-\–\—:]|\s*[º°ª])?\s*)?"
                texto_limpio_temp = re.sub(patron_inicio_articulo, '', texto_bruto_articulo, count=1, flags=re.IGNORECASE).strip()
                
                if texto_limpio_temp and len(texto_limpio_temp) < len(texto_bruto_articulo) : # Asegurarse que la limpieza hizo algo útil
                    texto_articulo = texto_limpio_temp
                    campo_texto_usado += " (con limpieza de prefijo)"
                else: # Si la limpieza no cambió nada o vació el string, usar el bruto
                    texto_articulo = texto_bruto_articulo
            else:
                 texto_articulo = texto_bruto_articulo
            
            if not texto_articulo:
                logger.warning(f"Artículo XML #{i+1} (Display='{numero_display}', idParte: {id_parte_articulo}): No tiene contenido textual identificable. Se omite.")
                continue 
        
        logger.debug(f"Artículo XML #{i+1} (Display='{numero_display}', idParte: {id_parte_articulo}): Texto extraído usando '{campo_texto_usado}'. Longitud: {len(texto_articulo)} chars.")

        patron_referencias = r"(?:Ley|Decreto\s+(?:con\s+Fuerza\s+de\s+Ley|Ley|Supremo)|D\.F\.L\.?|D\.L\.?|L\.?)\s*(?:N(?:[°ºªo]|\.?)|\bnúmeros?\b)?\s*([\w\d\.-]+(?:/\d{2,4})?)"
        referencias_encontradas_tuplas = re.findall(patron_referencias, texto_articulo, re.IGNORECASE)
        referencias_limpias = list(set(ref.strip(" .-") for ref in referencias_encontradas_tuplas if ref.strip(" .-")))

        resultado.append({
            "articulo_display": numero_display,
            "articulo_id_interno": numero_id_interno,
            "texto": texto_articulo,
            "referencias_legales": referencias_limpias,
            "id_parte_xml": id_parte_articulo # Añadido para trazabilidad
        })
    
    logger.info(f"Extracción finalizada. {len(resultado)} artículos procesados y añadidos.")
    if not resultado and xml_data and len(articulos_tags) > 0 :
        logger.warning("Se encontraron etiquetas <Articulo> pero no se extrajo ningún artículo válido. Revisar lógica de normalización o extracción de texto.")

    return resultado


@app.get("/ley")
def consultar_ley(numero_ley: str, articulo: Optional[str] = None):
    """
    Endpoint principal para consultar una ley y, opcionalmente, un artículo específico.
    """
    logger.info(f"Recibida consulta para ley: {numero_ley}, artículo: {articulo if articulo else 'Todos'}")
    id_norma = obtener_id_norma(numero_ley)
    if not id_norma:
        return {"error": f"No se encontró ID para la ley {numero_ley}. Verifique el número o si la ley está disponible en leychile.cl."}

    xml_content = obtener_xml_ley(id_norma)
    if not xml_content:
        return {"error": f"No se pudo obtener el contenido XML para la ley {numero_ley} (ID Norma: {id_norma})."}

    articulos_data = extraer_articulos(xml_content)
    if not articulos_data:
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
            # Se escapa articulo_buscado_norm.replace("t","") para la regex
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
            if patron_texto.search(art_obj["texto"]):
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
    """
    Consulta el contenido HTML de una parte específica (artículo) de una norma desde bcn.cl.
    """
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
            f"div#p{idParte}",             
            f"article#{idParte}",          
            f"div.textoNorma[id*='{idParte}']", 
            f"div.textoArticulo[id*='{idParte}']",
            f"div[id='{idParte}']"         
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
            "idNorma": idNorma,
            "idParte": idParte,
            "url_fuente": url,
            "selector_usado": selector_usado,
            "texto_html_extraido": texto_extraido
        }
    except Exception as e:
        error_message = f"Error al parsear HTML o extraer texto para idNorma {idNorma}, idParte {idParte}."
        logger.exception(error_message) 
        return {"error": f"Error al procesar el contenido HTML para idParte '{idParte}'. Detalle: {e}"}

# Ejemplo para ejecutar con Uvicorn (si este archivo se llama main.py):
# uvicorn main:app --reload --log-level debug
#
# Pruebas sugeridas:
# http://127.0.0.1:8000/ley?numero_ley=21595&articulo=15
# http://127.0.0.1:8000/ley?numero_ley=21595&articulo=ARTICULO 15
# http://127.0.0.1:8000/ley?numero_ley=21595&articulo=Art. 15
# http://127.0.0.1:8000/ley?numero_ley=19880&articulo=24
# http://127.0.0.1:8000/ley?numero_ley=DFL-1&articulo=10 
# http://127.0.0.1:8000/ley?numero_ley=20370&articulo=3 bis 
# http://127.0.0.1:8000/ley?numero_ley=20370&articulo=Primero Transitorio
# http://127.0.0.1:8000/ley?numero_ley=1&articulo=1 (Código Civil, requiere fallback para "1" o "CC")
