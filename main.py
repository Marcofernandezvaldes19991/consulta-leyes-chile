from fastapi import FastAPI
from typing import Optional
import requests
from bs4 import BeautifulSoup
import re
import json

app = FastAPI()

# ✅ Cargar IDs conocidos desde archivo externo
with open("fallbacks.json", "r", encoding="utf-8") as f:
    fallback_ids = json.load(f)

# ✅ Buscar el idNorma de una ley por su número, con fallback si es necesario
def obtener_id_norma(numero_ley):
    numero_ley = str(numero_ley)  # Forzar a string por seguridad
    if numero_ley in fallback_ids:
        print(f"[INFO] Usando ID desde fallback para ley {numero_ley}: {fallback_ids[numero_ley]}")
        return fallback_ids[numero_ley]

    # Buscar dinámicamente si no está en fallback
    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{numero_ley}"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"[ERROR] No se pudo conectar con Ley Chile para la ley {numero_ley}")
        return None
    soup = BeautifulSoup(response.content, "xml")
    normas = soup.find_all("Norma")
    for norma in normas:
        titulo = norma.find("Titulo") or norma.find("Rubro")
        if titulo and numero_ley in titulo.text:
            id_norma = norma.find("IdNorma")
            if id_norma:
                print(f"[INFO] ID obtenido dinámicamente: {id_norma.text}")
                return id_norma.text
    return None

# ✅ Obtener el XML completo de la ley desde Ley Chile
def obtener_xml_ley(id_norma):
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={id_norma}&notaPIE=1"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"[ERROR] Fallo al obtener el XML para idNorma {id_norma}")
        return None
    return response.content

# ✅ Extraer artículos del XML, incluso si no tienen <Numero>
def extraer_articulos(xml_data):
    soup = BeautifulSoup(xml_data, "xml")
    articulos = soup.find_all("Articulo")
    resultado = []
    print("[INFO] Artículos detectados:")
    for art in articulos:
        texto = art.find("Texto").text.strip() if art.find("Texto") else ""
        numero_tag = art.find("Numero")

        if numero_tag:
            numero_raw = numero_tag.text.strip().lower()
            numero = re.sub(r"art[íi]?culo\s*\.?:?\s*", "", numero_raw).strip()
        else:
            # Detectar "Artículo XX" al inicio del texto si no hay <Numero>
            match = re.match(r"art[íi]?culo\s+(\d{1,3})", texto.lower())
            numero = match.group(1) if match else "S/N"

        referencias = re.findall(r"Ley N[°º]?\s*\d{4,7}", texto)
        resultado.append({
            "articulo": numero,
            "texto": texto,
            "referencias_legales": list(set(referencias))
        })
        print(f" - Artículo detectado → {numero}")
    return resultado

# ✅ Endpoint principal para consultar leyes
@app.get("/ley")
def consultar_ley(numero_ley: str, articulo: Optional[str] = None):
    print(f"[SOLICITUD] Ley {numero_ley} / Artículo {articulo if articulo else 'todos'}")

    id_norma = obtener_id_norma(numero_ley)
    if not id_norma:
        return {"error": f"No se encontró la ley {numero_ley}"}

    xml = obtener_xml_ley(id_norma)
    if not xml:
        return {"error": f"No se pudo obtener la ley {numero_ley}"}

    articulos = extraer_articulos(xml)

    if articulo:
        articulo_normalizado = articulo.strip().lower()
        busqueda_en_texto = f"artículo {articulo_normalizado}"

        for art in articulos:
            # Coincidencia exacta por número
            if art["articulo"].strip().lower() == articulo_normalizado:
                return art
            # Coincidencia por contenido de texto al inicio
            if art["texto"].lower().startswith(busqueda_en_texto):
                return art

        return {"error": f"Artículo {articulo} no encontrado"}
    else:
        return {"articulos": articulos}
