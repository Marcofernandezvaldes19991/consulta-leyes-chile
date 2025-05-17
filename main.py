from fastapi import FastAPI
from typing import Optional, List, Dict, Any
import requests
from bs4 import BeautifulSoup
import re
import json
import os

app = FastAPI()

# Cargar fallback de ID de normas
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    fallback_path = os.path.join(current_dir, "fallbacks.json")
    with open(fallback_path, "r", encoding="utf-8") as f:
        fallback_ids = json.load(f)
    print(f"IDs de fallback cargados exitosamente desde: {fallback_path}")
except FileNotFoundError:
    fallback_ids = {}
    print(f"ADVERTENCIA: Archivo 'fallbacks.json' no encontrado en {fallback_path}. El fallback de IDs no estará disponible.")
except json.JSONDecodeError:
    fallback_ids = {}
    print(f"ADVERTENCIA: Error al decodificar 'fallbacks.json'. El fallback de IDs no estará disponible.")
except Exception as e:
    fallback_ids = {}
    print(f"ADVERTENCIA: Ocurrió un error inesperado al cargar 'fallbacks.json': {e}")

# Diccionarios para ayudar en la normalización de números de artículo
ROMAN_TO_INT = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15, 'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20,
    # Añadir más si es necesario
}
WORDS_TO_INT = {
    'primero': '1', 'segundo': '2', 'tercero': '3', 'cuarto': '4', 'quinto': '5',
    'sexto': '6', 'séptimo': '7', 'octavo': '8', 'noveno': '9', 'décimo': '10',
    'undécimo': '11', 'duodécimo': '12', 'decimotercero': '13', 'decimocuarto': '14', 'decimoquinto': '15',
    'vigésimo': '20', 'trigésimo': '30', 'cuadragésimo': '40', 'quincuagésimo': '50',
    'unico': 'unico', 'final': 'final', # Palabras clave especiales
    # Formas abreviadas o con tilde
    'único': 'unico',
}

def normalizar_numero_articulo_para_comparacion(num_str: Optional[str]) -> str:
    """
    Normaliza un número de artículo para facilitar la comparación interna.
    Intenta manejar diversos formatos: "Artículo 1 bis", "1° A", "V", "Décimo", "Transitorio 1", "Final".
    El objetivo es convertirlo a un string consistente, ej: "1bis", "1a", "5", "10", "t1", "final".
    """
    if not num_str:
        return "s/n"  # Sin número / Sin normalizar

    s = str(num_str).lower().strip()

    # Manejar casos especiales directos
    if s in WORDS_TO_INT:
        return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:
        return str(ROMAN_TO_INT[s])

    # Quitar prefijos como "artículo", "art.", "nro.", "disposición", etc.
    s = re.sub(r'^(artículo|articulo|art\.?|nro\.?|nº|n°|disposición|disp\.?)\s*', '', s, flags=re.IGNORECASE)
    
    # Manejar "transitorio"
    transitorio_match = re.match(r'^(transitorio|trans\.?|t)\s*(.*)', s, flags=re.IGNORECASE)
    prefijo_transitorio = ""
    if transitorio_match:
        prefijo_transitorio = "t"
        s = transitorio_match.group(2).strip() # Continuar procesando el número del transitorio

    # Convertir palabras de números a dígitos dentro del string (ej. "decimo primero" -> "10 1")
    # Esta parte puede volverse compleja y necesitar un parser más sofisticado para casos combinados.
    # Por ahora, se prioriza la extracción de números directos.
    for palabra, digito in WORDS_TO_INT.items():
        if palabra in s: # Solo si la palabra completa está
             # Cuidado con reemplazos parciales, ej. "decimo" en "decimoprimero"
             # Usar \b para asegurar palabra completa si se reemplaza
             s = re.sub(r'\b' + palabra + r'\b', digito, s)

    # Quitar ordinales (º, °, ª), puntos, comas.
    s = re.sub(r'[º°ª\.,]', '', s)

    # Extraer números y letras (para "bis", "a", "b", etc.)
    # Este regex busca un número, opcionalmente seguido de letras (bis, ter, a, b)
    # o letras seguidas de un número (raro, pero posible).
    # También maneja números romanos que no fueron capturados antes (ej. "XV A")
    
    # Primero, intentar identificar números arábigos y letras adjuntas
    partes_numericas = re.findall(r'(\d+)\s*([a-zA-Z]*)', s)
    
    componentes_normalizados = []
    texto_restante = s
    
    for num_part, letra_part in partes_numericas:
        componente = num_part
        if letra_part:
            if letra_part == "bis":
                componente += "bis"
            elif letra_part == "ter":
                componente += "ter"
            elif letra_part == "quater":
                componente += "quater"
            # Para letras solas como A, B, C después de un número
            elif len(letra_part) == 1 and letra_part.isalpha():
                componente += letra_part
            # Ignorar otras letras si no son modificadores conocidos
        componentes_normalizados.append(componente)
        # Remover la parte procesada de texto_restante para evitar reprocesamiento
        # Esto es simplificado; un enfoque más robusto usaría índices de match.
        texto_restante = texto_restante.replace(num_part, "", 1).replace(letra_part, "", 1).strip()


    # Si después de extraer números arábigos queda algo que podría ser romano
    if not componentes_normalizados and texto_restante:
        # Quitar espacios y verificar si es un número romano conocido
        posible_romano = texto_restante.replace(" ", "")
        if posible_romano in ROMAN_TO_INT:
            componentes_normalizados.append(str(ROMAN_TO_INT[posible_romano]))
            texto_restante = "" # Se consumió todo

    # Si aún queda texto y no se identificó nada, puede ser una palabra clave no mapeada o un formato complejo
    if not componentes_normalizados and texto_restante:
        # Conservar el texto restante limpio como último recurso
        componentes_normalizados.append(texto_restante.replace(" ", ""))


    # Unir los componentes normalizados y el prefijo de transitorio
    id_final = "".join(componentes_normalizados)
    
    if not id_final: # Si no se pudo extraer nada inteligible
        # Como último recurso, tomar todo el string original, quitar espacios y caracteres no alfanuméricos
        # excepto los que podrían ser parte de un identificador (ej. "bis").
        # Esto es muy genérico.
        s_limpio = re.sub(r'[^a-z0-9]', '', s.replace(" ", "")).strip()
        if not s_limpio: # Si después de limpiar no queda nada
            return "s/n_error_normalizacion"
        id_final = s_limpio


    return prefijo_transitorio + id_final if id_final else "s/n"


def obtener_id_norma(numero_ley: str) -> Optional[str]:
    """
    Obtiene el IdNorma desde leychile.cl para un número de ley dado.
    """
    norm_numero_ley_buscado = numero_ley.strip().replace(".", "").replace(",", "") 
    if not norm_numero_ley_buscado.isdigit():
        print(f"Advertencia: El número de ley '{numero_ley}' (normalizado a '{norm_numero_ley_buscado}') no parece ser un número válido.")

    if norm_numero_ley_buscado in fallback_ids:
        print(f"Usando ID de fallback para ley '{norm_numero_ley_buscado}': {fallback_ids[norm_numero_ley_buscado]}")
        return fallback_ids[norm_numero_ley_buscado]

    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{norm_numero_ley_buscado}"
    print(f"Consultando URL para ID de norma: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"Timeout al buscar ID para ley {norm_numero_ley_buscado}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error en petición HTTP para ID de norma {norm_numero_ley_buscado}: {e}")
        return None

    try:
        soup = BeautifulSoup(response.content, "xml")
        normas = soup.find_all("Norma")
        if not normas:
            print(f"No se encontraron etiquetas <Norma> para ley {norm_numero_ley_buscado}. XML: {response.content.decode('utf-8', 'ignore')[:500]}")
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
                        print(f"ID encontrado para ley '{norm_numero_ley_buscado}' (N° XML: '{numero_norma_en_xml}'): {id_encontrado}. Título: '{titulo_debug_text}'")
                        return id_encontrado
                    else:
                        print(f"Coincidencia de número de ley '{numero_norma_en_xml}' para '{norm_numero_ley_buscado}', pero no se encontró IdNorma.")
        
        print(f"No se encontró un ID de norma coincidente para la ley '{norm_numero_ley_buscado}' en {len(normas)} normas evaluadas (verificando etiqueta <Numero>).")
        return None
    except Exception as e:
        print(f"Error al parsear XML para obtener ID de norma ({norm_numero_ley_buscado}): {e}")
        return None


def obtener_xml_ley(id_norma: str) -> Optional[bytes]:
    """Obtiene el contenido XML de una ley dado su IdNorma."""
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    print(f"Consultando XML de ley con IDNorma {id_norma} en URL: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'} 
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        return response.content
    except requests.exceptions.Timeout:
        print(f"Timeout al obtener XML para IDNorma {id_norma}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error en petición HTTP para XML de ley (IDNorma {id_norma}): {e}")
        return None


def extraer_articulos(xml_data: Optional[bytes]) -> List[Dict[str, Any]]:
    """Extrae los artículos y sus detalles desde el contenido XML de una ley."""
    if not xml_data:
        print("No hay datos XML para extraer artículos.")
        return []
    try:
        soup = BeautifulSoup(xml_data, "xml") 
    except Exception as e: 
        print(f"Error al parsear XML de la ley con BeautifulSoup: {e}")
        return []
        
    resultado = []
    # La etiqueta <Articulo> parece ser la que consistentemente contiene los artículos legislativos numerados.
    articulos_tags = soup.find_all("Articulo")

    if not articulos_tags:
        print("No se encontraron etiquetas <Articulo> en el XML.")
        return []

    for art_tag in articulos_tags:
        # El atributo 'tipoParte' puede ayudar a distinguir artículos de otros elementos si fuera necesario,
        # pero la búsqueda de <Articulo> ya es bastante específica.
        # tipo_parte = art_tag.get('tipoParte', '').lower()
        # if tipo_parte and tipo_parte != 'articulo': # Ejemplo si quisiéramos filtrar
        #     continue

        numero_tag = art_tag.find("Numero")
        numero_display = numero_tag.text.strip() if numero_tag and numero_tag.text else "S/N"
        numero_id_interno = normalizar_numero_articulo_para_comparacion(numero_display)

        # Si la normalización falla o da un ID no útil, podríamos optar por omitir el artículo
        # o usar el display_num como ID interno si es suficientemente simple.
        if numero_id_interno == "s/n_error_normalizacion" or not numero_id_interno :
            print(f"Advertencia: No se pudo normalizar satisfactoriamente el número de artículo '{numero_display}'. Se omite este artículo.")
            continue
        if numero_id_interno == "s/n" and numero_display != "S/N": # Si la normalización solo quitó prefijos pero no encontró número
             print(f"Advertencia: Número de artículo '{numero_display}' resultó en 's/n' tras normalización. Revisar lógica.")
             # Podríamos decidir usar una versión simplificada de numero_display aquí si es necesario.

        texto_articulo = None
        # Campos comunes para el texto del artículo. <Texto> es el más usual dentro de <Articulo>.
        campos_posibles_texto = ["Texto", "TextoArticulo", "Cuerpo", "Contenido"] 
        for campo_nombre in campos_posibles_texto:
            tag_texto = art_tag.find(campo_nombre) 
            if tag_texto and tag_texto.text and tag_texto.text.strip():
                texto_articulo = tag_texto.text.strip()
                break
        
        if not texto_articulo: # Fallback si no se encontró en campos específicos
            # Tomar todo el texto del tag Articulo y limpiar.
            texto_bruto_articulo = art_tag.get_text(separator=" ", strip=True)
            
            # Intentar remover el número de artículo del inicio del texto si está presente,
            # para evitar redundancia, ya que tenemos `numero_display`.
            # Esta lógica debe ser cuidadosa para no eliminar parte del texto real.
            # Se compara con numero_display que es el texto original del tag <Numero>.
            if numero_display != "S/N":
                # Crear un patrón para el número de artículo, considerando espacios y puntuación común al inicio.
                # Ejemplo: "Artículo 1.- Texto..." o "1. Texto..."
                patron_inicio_articulo = r"^(?:\s*" + re.escape(numero_display) + r"\s*(?:[\.\-\–\—:]|\s*[º°ª])?\s*)?"
                texto_limpio_temp = re.sub(patron_inicio_articulo, '', texto_bruto_articulo, count=1, flags=re.IGNORECASE).strip()

                # Solo usar el texto limpiado si realmente se quitó algo y no quedó vacío.
                if texto_limpio_temp and len(texto_limpio_temp) < len(texto_bruto_articulo):
                    texto_articulo = texto_limpio_temp
                else:
                    texto_articulo = texto_bruto_articulo # Usar el bruto si la limpieza no fue efectiva o vació el string.
            else:
                 texto_articulo = texto_bruto_articulo
            
            if not texto_articulo:
                # print(f"Artículo '{numero_display}' (ID interno '{numero_id_interno}') no tiene contenido textual identificable.")
                continue 

        patron_referencias = r"(?:Ley|Decreto\s+(?:con\s+Fuerza\s+de\s+Ley|Ley|Supremo)|D\.F\.L\.?|D\.L\.?|L\.?)\s*(?:N(?:[°ºªo]|\.?)|\bnúmeros?\b)?\s*([\w\d\.-]+(?:/\d{2,4})?)"
        referencias_encontradas_tuplas = re.findall(patron_referencias, texto_articulo, re.IGNORECASE)
        referencias_limpias = list(set(ref.strip(" .-") for ref in referencias_encontradas_tuplas if ref.strip(" .-")))

        resultado.append({
            "articulo_display": numero_display,
            "articulo_id_interno": numero_id_interno,
            "texto": texto_articulo,
            "referencias_legales": referencias_limpias
        })
    
    if not resultado and xml_data:
        print("Se procesó el XML pero no se extrajo ningún artículo. Revisar estructura del XML y selectores.")

    return resultado


@app.get("/ley")
def consultar_ley(numero_ley: str, articulo: Optional[str] = None):
    """
    Endpoint principal para consultar una ley y, opcionalmente, un artículo específico.
    """
    id_norma = obtener_id_norma(numero_ley)
    if not id_norma:
        return {"error": f"No se encontró ID para la ley {numero_ley}. Verifique el número o si la ley está disponible en leychile.cl."}

    xml_content = obtener_xml_ley(id_norma)
    if not xml_content:
        return {"error": f"No se pudo obtener el contenido XML para la ley {numero_ley} (ID Norma: {id_norma})."}

    articulos_data = extraer_articulos(xml_content)
    if not articulos_data:
        return {"error": f"No se extrajeron artículos de la ley {numero_ley} (ID Norma: {id_norma}). El XML podría estar vacío, no tener artículos, o no tener el formato esperado."}

    if articulo:
        articulo_buscado_norm = normalizar_numero_articulo_para_comparacion(articulo)
        
        # Verificación adicional: si la normalización del artículo buscado falla
        if articulo_buscado_norm == "s/n_error_normalizacion" or not articulo_buscado_norm or articulo_buscado_norm == "s/n":
            return {"error": f"No se pudo normalizar el número de artículo buscado: '{articulo}'. Intente con un formato más simple (ej. '15', '15bis', 't1')."}

        articulos_encontrados_exactos = []
        for art_obj in articulos_data:
            if art_obj["articulo_id_interno"] == articulo_buscado_norm:
                articulos_encontrados_exactos.append(art_obj)
        
        if articulos_encontrados_exactos:
            if len(articulos_encontrados_exactos) == 1:
                return articulos_encontrados_exactos[0]
            else:
                # Esto podría pasar si hay múltiples elementos con el mismo número normalizado (ej. Artículo 1 y Artículo 1 Transitorio si ambos normalizan a "1" y "t1" respectivamente y se busca "1")
                # O si hay duplicados por alguna razón en el XML.
                print(f"Advertencia: Múltiples artículos ({len(articulos_encontrados_exactos)}) coinciden con el ID interno normalizado '{articulo_buscado_norm}'. Devolviendo el primero.")
                return articulos_encontrados_exactos[0]


        # Búsqueda textual como fallback si no hay coincidencia exacta por ID interno
        try:
            # Patrón mejorado para buscar "artículo X", "art. X", etc. seguido del número.
            # Asegura que el número esté delimitado para evitar coincidencias parciales (e.g., "art 1" no debe coincidir con "art 10").
            patron_texto = re.compile(
                # Prefijos comunes para artículo
                r"\b(?:art(?:ículo|iculo)?s?\.?|art\.?|disposición|disp\.?)\s+" 
                # El número de artículo buscado (escapado por si tiene caracteres especiales, aunque la normalización debería minimizarlos)
                + r"(?:transitorio|trans\.?\s*)?" # Opcional "transitorio"
                + re.escape(articulo_buscado_norm.replace("t","")) # Quitar 't' para la búsqueda textual si estaba en el normalizado
                # Delimitador: espacio, puntuación, fin de palabra, fin de string. 
                # Se añade [º°ª]? para casos como "artículo 1º,"
                + r"(?:[\sº°ªÞ,\.;:\(\)]|\b|$)", 
                re.IGNORECASE
            )
        except re.error as e:
            print(f"Error al compilar regex para búsqueda textual de artículo '{articulo_buscado_norm}': {e}")
            return {"error": f"Error interno al procesar la búsqueda del artículo '{articulo}'."}

        articulos_coincidentes_texto = []
        for art_obj in articulos_data:
            if patron_texto.search(art_obj["texto"]):
                art_obj_copia = art_obj.copy() 
                art_obj_copia["nota_busqueda"] = f"Artículo encontrado por mención de '{articulo}' (normalizado a '{articulo_buscado_norm}') en su texto. El número formal del artículo es '{art_obj['articulo_display']}' (ID interno: '{art_obj['articulo_id_interno']}')."
                articulos_coincidentes_texto.append(art_obj_copia)
        
        if articulos_coincidentes_texto:
            if len(articulos_coincidentes_texto) == 1:
                 return articulos_coincidentes_texto[0]
            else:
                print(f"Múltiples artículos ({len(articulos_coincidentes_texto)}) mencionan textualmente '{articulo}'. Devolviendo el primero.")
                return {
                    "advertencia": f"Se encontraron {len(articulos_coincidentes_texto)} artículos que mencionan textualmente '{articulo}'. Se devuelve el primero de ellos.",
                    "articulo_encontrado": articulos_coincidentes_texto[0],
                    "otros_articulos_con_menciones_similares": [a["articulo_display"] for a in articulos_coincidentes_texto]
                }
        
        # Si no se encontró de ninguna forma.
        return {
            "error": f"Artículo '{articulo}' (buscado como '{articulo_buscado_norm}') no encontrado ni por ID exacto ni por mención textual en la ley {numero_ley}.",
            "sugerencia": "Verifique el número del artículo. Pruebe formatos como '15', '15bis', 'Primero Transitorio', 'Final'. También puede intentar sin especificar un artículo para ver todos los disponibles.",
            "articulos_disponibles_ids_internos_muestra": [a["articulo_id_interno"] for a in articulos_data[:20]], 
            "articulos_disponibles_display_muestra": [a["articulo_display"] for a in articulos_data[:20]] 
        }

    # Si no se especificó un artículo, devolver todos los artículos de la ley.
    return {"ley": numero_ley, "id_norma": id_norma, "articulos_totales": len(articulos_data), "articulos": articulos_data}


@app.get("/ley_html")
def consultar_articulo_html(idNorma: str, idParte: str):
    """
    Consulta el contenido HTML de una parte específica (artículo) de una norma desde bcn.cl.
    Este endpoint es inherentemente frágil debido a que depende de la estructura HTML de un sitio externo.
    """
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    print(f"Consultando HTML desde: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"Timeout al obtener HTML para idNorma {idNorma}, idParte {idParte}")
        return {"error": f"Timeout al obtener contenido de {url}"}
    except requests.exceptions.RequestException as e:
        print(f"Error en petición HTTP para HTML (idNorma {idNorma}, idParte {idParte}): {e}")
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
                print(f"Contenido encontrado para idParte '{idParte}' usando selector CSS '{selector}'")
                break
        
        if not div_contenido:
            div_contenido = soup.find("div", id=re.compile(f".*{re.escape(idParte)}.*", re.IGNORECASE))
            if div_contenido:
                selector_usado = f"Fallback regex: div con id que contiene '{idParte}' (id real: {div_contenido.get('id')})"
                print(f"Contenido encontrado para idParte '{idParte}' usando {selector_usado}")
            else:
                print(f"No se encontró contenido para idParte '{idParte}' en norma '{idNorma}' con los selectores probados.")
                return {"error": f"No se encontró el elemento de contenido específico para idParte '{idParte}' en la página de la norma '{idNorma}'."}

        texto_extraido = div_contenido.get_text(separator="\n", strip=True)
        
        return {
            "idNorma": idNorma,
            "idParte": idParte,
            "url_fuente": url,
            "selector_usado": selector_usado,
            "texto_html_extraido": texto_extraido
        }
    except Exception as e:
        print(f"Error al parsear HTML o extraer texto para idNorma {idNorma}, idParte {idParte}: {e}")
        return {"error": f"Error al procesar el contenido HTML para idParte '{idParte}'. Detalle: {e}"}

# Ejemplo para ejecutar con Uvicorn (si este archivo se llama main.py):
# uvicorn main:app --reload
#
# Pruebas sugeridas:
# http://127.0.0.1:8000/ley?numero_ley=21595&articulo=15
# http://127.0.0.1:8000/ley?numero_ley=21595&articulo=1
# http://127.0.0.1:8000/ley?numero_ley=19880&articulo=24
# http://127.0.0.1:8000/ley?numero_ley=DFL-1&articulo=10 (Probar DFL)
# http://127.0.0.1:8000/ley?numero_ley=20370&articulo=3 bis (Artículo con "bis")
# http://127.0.0.1:8000/ley?numero_ley=20370&articulo=Primero Transitorio
# http://127.0.0.1:8000/ley?numero_ley=CC Chilena&articulo=1 (Constitución, requiere fallback o manejo especial de nombre)

