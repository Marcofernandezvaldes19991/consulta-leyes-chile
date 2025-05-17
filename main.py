from fastapi import FastAPI, HTTPException, Query
from typing import Optional, List, Any 
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
    description="Permite consultar artículos de leyes chilenas obteniendo datos desde LeyChile.cl. Esta versión incluye operaciones asíncronas, caché, y truncamiento de listas de artículos y texto largo.",
    version="1.7.0", # Incremento de versión por corrección en selectores HTML
    servers=[
        {
            "url": "https://consulta-leyes-chile.onrender.com", 
            "description": "Servidor de Producción en Render"
        }
    ]
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
# (Las funciones normalizar_numero_articulo_para_comparacion, limpiar_texto_articulo, 
#  extraer_referencias_legales_mejorado, obtener_id_norma_async, obtener_xml_ley_async 
#  se mantienen igual que en la versión v16. Se omiten aquí por brevedad pero deben estar presentes)

def normalizar_numero_articulo_para_comparacion(num_str: Optional[str]) -> str:
    if not num_str: return "s/n"
    s = str(num_str).lower().strip()
    logger.debug(f"Normalizando: '{num_str}' -> '{s}' (inicial)")
    s = re.sub(r"^(artículo|articulo|art\.?|nro\.?|n[º°]|disposición|disp\.?)\s*", "", s, flags=re.IGNORECASE).strip()
    s = s.rstrip('.-').strip()
    if s in WORDS_TO_INT: return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT: return str(ROMAN_TO_INT[s])
    prefijo_transitorio = ""
    transitorio_match = re.match(r"^(transitorio|trans\.?|t)\s*(.*)", s, flags=re.IGNORECASE)
    if transitorio_match:
        prefijo_transitorio = "t"
        s = transitorio_match.group(2).strip().rstrip('.-').strip()
    for palabra, digito in WORDS_TO_INT.items():
        if re.search(r'\b' + re.escape(palabra) + r'\b', s): s = re.sub(r'\b' + re.escape(palabra) + r'\b', digito, s)
    s = re.sub(r"[º°ª\.,]", "", s)
    s = s.strip().rstrip('-').strip()
    partes_numericas = re.findall(r"(\d+)\s*([a-zA-Z]*)", s)
    componentes_normalizados = []
    texto_restante = s
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part:
            if letra_part in ["bis", "ter", "quater"] or (len(letra_part) == 1 and letra_part.isalpha()): componente += letra_part
        componentes_normalizados.append(componente)
        texto_restante = texto_restante.replace(num_part, "", 1).replace(letra_part, "", 1).strip()
    if not componentes_normalizados and tex
