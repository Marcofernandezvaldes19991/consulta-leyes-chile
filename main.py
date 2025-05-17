from fastapi import FastAPI
from typing import Optional
import requests
from bs4 import BeautifulSoup
import re
import json
import os

app = FastAPI()

# Cargar fallback de ID de normas
fallback_path = os.path.join(os.path.dirname(__file__), "fallbacks.json")
with open(fallback_path, "r", encoding="utf-8") as f:
    fallback_ids = json.load(f)

def obtener_id_norma(numero_ley):
    if numero_ley in fallback_ids:
        return fallback_ids[numero_ley]

    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{numero_ley}"
    response = requests.get(url)
    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.content, "xml")
    normas = soup.find_all("Norma")
    for norma in normas:
        titulo = norma.find("Titulo") or norma.find("Rubro")
        if titulo and numero_ley in titulo.text:
            id_norma = norma.find("IdNorma")
            if id_norma:
                return id_norma.text

    return None

def obtener_xml_ley(id_norma):
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    response = requests.get(url)
    if response.status_code != 200:
        return None
    return response.content

def extraer_articulos(xml_data):
    soup = BeautifulSoup(xml_data, "xml")
    articulos = soup.find_all("Articulo")
    resultado = []

    for art in articulos:
        numero = art.find("Numero").text.strip() if art.find("Numero") else "S/N"

        # Se prueba con distintos campos posibles
        campos = ["Texto", "Descripcion", "DescripcionNorma", "Contenido"]
        texto = None
        for campo in campos:
            tag = art.find(campo)
            if tag and tag.text.strip():
                texto = tag.text.strip()
                break

        if not texto:
            continue

        referencias = re.findall(r"Ley N[°º]?\\s*\\d{4,7}", texto)
        resultado.append({
            "articulo": numero,
            "texto": texto,
            "referencias_legales": list(set(referencias))
        })

    return resultado

@app.get("/ley")
def consultar_ley(numero_ley: str, articulo: Optional[str] = None):
    id_norma = obtener_id_norma(numero_ley)
    if not id_norma:
        return {"error": f"No se encontró la ley {numero_ley}"}

    xml = obtener_xml_ley(id_norma)
    if not xml:
        return {"error": f"No se pudo obtener la ley {numero_ley}"}

    articulos = extraer_articulos(xml)

    if articulo:
        # Normalización de número
        articulo_normalizado = str(int(articulo)).strip()

        import re
        patron = re.compile(rf"\\b(art(í)?culo|art\\.?)[\\s\\xa0]*{articulo_normalizado}\\b", re.IGNORECASE)

        for art in articulos:
            # Coincidencia exacta en el campo "articulo"
            if art["articulo"].strip() == articulo_normalizado:
                return art

            # Búsqueda por coincidencia textual dentro del contenido del artículo
            if patron.search(art["texto"]):
                art["nota"] = "Artículo encontrado por coincidencia textual en el contenido"
                return art

        return {
            "error": f"Artículo {articulo} no encontrado",
            "debug": [a["articulo"] for a in articulos]  # Para depuración
        }

    return {"articulos": articulos}

@app.get("/ley_html")
def consultar_articulo_html(idNorma: str, idParte: str):
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={idNorma}&idParte={idParte}"
    response = requests.get(url)
    if response.status_code != 200:
        return {"error": f"No se pudo obtener el contenido para idParte {idParte}"}

    soup = BeautifulSoup(response.text, "html.parser")
    div_contenido = soup.find("div", {"id": f"p{idParte}"})
    if not div_contenido:
        return {"error": f"No se encontró contenido para idParte {idParte}"}

    texto = div_contenido.get_text(separator="\n").strip()
    return {
        "idNorma": idNorma,
        "idParte": idParte,
        "texto": texto
    }

