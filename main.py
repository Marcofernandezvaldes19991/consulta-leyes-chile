from fastapi import FastAPI
from typing import Optional
import requests
from bs4 import BeautifulSoup
import re
import json
import logging

# -------------------------------
# Configuración de logging
# -------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------
# Inicialización de FastAPI
# -------------------------------
app = FastAPI()

# -------------------------------
# Cargar IDs conocidos desde fallback
# -------------------------------
with open("fallbacks.json", "r", encoding="utf-8") as f:
    fallback_ids = json.load(f)

# -------------------------------
# Buscar ID de norma
# -------------------------------
def obtener_id_norma(numero_ley):
    if numero_ley in fallback_ids:
        logger.info(f"[INFO] Usando ID fallback para ley {numero_ley}: {fallback_ids[numero_ley]}")
        return fallback_ids[numero_ley]

    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{numero_ley}"
    response = requests.get(url)
    if response.status_code != 200:
        logger.error(f"[ERROR] No se pudo conectar a LeyChile al buscar ID de la ley {numero_ley}")
        return None

    soup = BeautifulSoup(response.content, "xml")
    normas = soup.find_all("Norma")

    for norma in normas:
        titulo = norma.find("Titulo") or norma.find("Rubro")
        if titulo and numero_ley in titulo.text:
            id_norma = norma.find("IdNorma")
            if id_norma:
                logger.info(f"[INFO] ID detectado desde búsqueda para ley {numero_ley}: {id_norma.text}")
                return id_norma.text

    logger.warning(f"[WARN] No se encontró la ley {numero_ley}")
    return None

# -------------------------------
# Obtener XML completo de la ley
# -------------------------------
def obtener_xml_ley(id_norma):
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    response = requests.get(url)
    if response.status_code != 200:
        logger.error(f"[ERROR] Fallo al obtener XML para ID {id_norma}")
        return None
    return response.content

# -------------------------------
# Extraer artículos desde el XML
# -------------------------------
def extraer_articulos(xml_data):
    soup = BeautifulSoup(xml_data, "xml")
    articulos = soup.find_all("Articulo")
    resultado = []

    logger.info(f"[INFO] Total de artículos encontrados: {len(articulos)}")

    for art in articulos:
        numero = art.find("Numero").text.strip() if art.find("Numero") else "S/N"
        texto = art.find("Texto").text.strip() if art.find("Texto") else ""
        referencias = re.findall(r"Ley N[°º]?\s*\d{4,7}", texto)
        resultado.append({
            "articulo": numero,
            "texto": texto,
            "referencias_legales": list(set(referencias))
        })

    return resultado

# -------------------------------
# Endpoint principal
# -------------------------------
@app.get("/ley")
def consultar_ley(numero_ley: str, articulo: Optional[str] = None):
    logger.info(f"[SOLICITUD] Ley {numero_ley} / Artículo: {articulo if articulo else 'todos'}")

    id_norma = obtener_id_norma(numero_ley)
    if not id_norma:
        return {"error": f"No se encontró la ley {numero_ley}"}

    xml = obtener_xml_ley(id_norma)
    if not xml:
        return {"error": f"No se pudo obtener el contenido de la ley {numero_ley}"}

    articulos = extraer_articulos(xml)

    if articulo:
        for art in articulos:
            if art["articulo"].strip() == articulo.strip():
                logger.info(f"[OK] Artículo {articulo} encontrado en ley {numero_ley}")
                return art
        logger.warning(f"[WARN] Artículo {articulo} no encontrado en ley {numero_ley}")
        return {"error": f"Artículo {articulo} no encontrado"}
    else:
        return {"articulos": articulos}
