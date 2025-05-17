from fastapi import FastAPI
from typing import Optional
import requests
from bs4 import BeautifulSoup
import re
import json

app = FastAPI()

# Cargar IDs conocidos desde archivo externo
with open("fallbacks.json", "r", encoding="utf-8") as f:
    fallback_ids = json.load(f)

def obtener_id_norma(numero_ley):
    numero_ley = str(numero_ley)  # <-- Asegura que sea string

    # Fallback manual
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
        numero = art.find("Numero").text if art.find("Numero") else "S/N"
        texto = art.find("Texto").text if art.find("Texto") else ""
        referencias = re.findall(r"Ley N[°º]\\s*\\d{4,7}", texto)
        resultado.append({
            "articulo": numero,
            "texto": texto.strip(),
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
        for art in articulos:
            if art["articulo"] == articulo:
                return art
        return {"error": f"Artículo {articulo} no encontrado"}
    else:
        return {"articulos": articulos}
