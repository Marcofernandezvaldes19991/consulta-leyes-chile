# main.py
# API de Consulta de Leyes Chilenas
# Versión 2.3.2 – Compatibilidad snake_case y camelCase en parámetros

import logging
import os
import json
import re
import asyncio
from typing import Optional, List

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from cachetools import TTLCache

# --- Logging ---
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)
logger.info("API v2.3.2 INICIANDO...")

# --- FastAPI & CORS ---
app = FastAPI(
    title="API de Consulta de Leyes Chilenas",
    version="2.3.2",
    description="Consulta leyes chilenas (XML) y artículos (HTML) de LeyChile.cl",
    servers=[{"url": "https://consulta-leyes-chile.onrender.com", "description": "Producción"}],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # Puedes restringir a tu dominio de plugin: ["https://chat.openai.com"]
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# --- Caché ---
cache_id_norma = TTLCache(maxsize=100, ttl=3600)
cache_xml_ley  = TTLCache(maxsize=50,  ttl=3600)

# --- Fallback IDs ---
try:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base_dir, "fallbacks.json"), encoding="utf-8") as f:
        fallback_ids = json.load(f)
    logger.info("Fallbacks cargados exitosamente.")
except Exception:
    fallback_ids = {}
    logger.warning("No se cargaron fallbacks.")

# --- Constantes ---
ROMAN_TO_INT = {
    "i":1,"ii":2,"iii":3,"iv":4,"v":5,"vi":6,"vii":7,"viii":8,"ix":9,"x":10
}
WORDS_TO_INT = {
    "primero":"1","segundo":"2","tercero":"3","cuarto":"4","quinto":"5",
    "sexto":"6","séptimo":"7","octavo":"8","noveno":"9","décimo":"10"
}
MAX_TEXT_LENGTH = 10000
MAX_ARTICULOS_RETURNED = 15
TRUNC_TEXT = "\n\n[... texto truncado ...]"
TRUNC_LIST = f"Mostrando primeros {MAX_ARTICULOS_RETURNED} artículos. La ley tiene más."

# --- Modelos Pydantic ---
class Articulo(BaseModel):
    articulo_display: str
    articulo_id_interno: str
    texto: str
    referencias_legales: List[str] = []
    id_parte_xml: Optional[str] = None
    nota_busqueda: Optional[str] = None

class LeyDetalle(BaseModel):
    ley: str
    id_norma: str
    articulos_totales_en_respuesta: int
    articulos: List[Articulo]
    total_articulos_originales_en_ley: Optional[int] = None
    nota_truncamiento_lista: Optional[str] = None

class ArticuloHTML(BaseModel):
    idNorma: str
    idParte: str
    url_fuente: str
    selector_usado: str
    texto_html_extraido: str

# --- Helpers ---
def normalizar_articulo(num_str: Optional[str]) -> str:
    if not num_str:
        return "s/n"
    s = num_str.lower().strip()
    s = re.sub(r"^(artículo|articulo)\s*", "", s)
    if s in WORDS_TO_INT:
        return WORDS_TO_INT[s]
    if s in ROMAN_TO_INT:
        return str(ROMAN_TO_INT[s])
    nums = re.findall(r"(\d+)([a-z]*)", s)
    comps = [n + t for n, t in nums]
    return "".join(comps) or s

def limpiar_texto(txt: str) -> str:
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return "\n".join(line.strip() for line in txt.splitlines()).strip()

def extraer_referencias(txt: str) -> List[str]:
    patrones = [r"ley\s+N[°º]?\s*\d+", r"art[íi]culo\s+\d+"]
    refs = set()
    for pat in patrones:
        for m in re.finditer(pat, txt, re.IGNORECASE):
            refs.add(m.group(0).strip())
    return sorted(refs)

# --- Llamadas a LeyChile ---
async def obtener_id_norma(n_ley: str, client: httpx.AsyncClient) -> Optional[str]:
    clave = n_ley.strip().replace(".", "")
    if cid := cache_id_norma.get(clave):
        return cid
    if clave in fallback_ids:
        cache_id_norma[clave] = fallback_ids[clave]
        return fallback_ids[clave]
    url = f"https://www.leychile.cl/Consulta/indice_normas_busqueda_simple?formato=xml&modo=1&busqueda=ley+{clave}"
    for _ in range(3):
        try:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "xml")
            for norma in soup.find_all("Norma"):
                tag_num = norma.find("Numero")
                num = tag_num.text.strip().replace(".", "") if tag_num and tag_num.text else ""
                if num == clave:
                    tag_id = norma.find("IdNorma")
                    idn = tag_id.text.strip() if tag_id and tag_id.text else None
                    if idn:
                        cache_id_norma[clave] = idn
                        return idn
            return None
        except Exception:
            await asyncio.sleep(1)
    return None

async def obtener_xml_ley(idn: str, client: httpx.AsyncClient) -> Optional[bytes]:
    if cxml := cache_xml_ley.get(idn):
        return cxml
    url = f"https://www.leychile.cl/Consulta/obtxml?opt=7&idNorma={idn}&notaPIE=1"
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        cache_xml_ley[idn] = r.content
        return r.content
    except Exception:
        return None

def extraer_articulos(xml_data: bytes) -> List[Articulo]:
    soup = BeautifulSoup(xml_data, "lxml-xml")
    arts = []
    for ef in soup.find_all("EstructuraFuncional"):
        if "artículo" not in (ef.get("tipoParte") or "").lower():
            continue
        idp = ef.get("idParte")
        tag_txt = ef.find("Texto")
        txt = tag_txt.text if tag_txt and tag_txt.text else ""
        m = re.match(r"^\s*([\w\s]+?)[:\-\.\n](.*)$", txt, re.DOTALL)
        disp = m.group(1).strip() if m else txt[:20]
        body = m.group(2).strip() if m else ""
        norm = normalizar_articulo(disp)
        limpio = limpiar_texto(body or disp)
        if len(limpio) > MAX_TEXT_LENGTH:
            limpio = limpio[:MAX_TEXT_LENGTH] + TRUNC_TEXT
        refs = extraer_referencias(limpio)
        arts.append(Articulo(
            articulo_display=disp,
            articulo_id_interno=norm,
            texto=limpio,
            referencias_legales=refs,
            id_parte_xml=idp
        ))
    return arts

# --- Endpoints ---

@app.get("/ley", response_model=LeyDetalle, summary="Consultar ley (XML)")
async def consultar_ley(
    numero_ley: Optional[str] = Query(
        None,
        description="Número de la ley (snake_case, ej. 21595)"
    ),
    numeroLey: Optional[str] = Query(
        None,
        description="Número de la ley (camelCase, ej. 21595)"
    ),
    articulo: Optional[str] = Query(
        None,
        description="Artículo (ej. 15, 1 bis)"
    )
):
    numero = numero_ley or numeroLey
    if not numero:
        raise HTTPException(
            status_code=422,
            detail="Debes especificar `numero_ley` o `numeroLey`."
        )
    async with httpx.AsyncClient() as client:
        idn = await obtener_id_norma(numero, client)
        if not idn:
            raise HTTPException(404, f"No hallé IDNorma para ley {numero}")
        xml = await obtener_xml_ley(idn, client)
        if not xml:
            raise HTTPException(503, "No pude obtener el XML de la ley.")
    lista = extraer_articulos(xml)
    if not lista:
        raise HTTPException(404, "No extraje artículos de la ley.")
    if articulo:
        norm = normalizar_articulo(articulo)
        for art in lista:
            if art.articulo_id_interno == norm:
                return LeyDetalle(
                    ley=numero,
                    id_norma=idn,
                    articulos_totales_en_respuesta=1,
                    articulos=[art],
                    total_articulos_originales_en_ley=len(lista)
                )
        raise HTTPException(404, f"No hallé artículo {articulo}")
    out = lista if len(lista) <= MAX_ARTICULOS_RETURNED else lista[:MAX_ARTICULOS_RETURNED]
    nota = TRUNC_LIST if len(lista) > MAX_ARTICULOS_RETURNED else None
    return LeyDetalle(
        ley=numero,
        id_norma=idn,
        articulos_totales_en_respuesta=len(out),
        articulos=out,
        total_articulos_originales_en_ley=len(lista),
        nota_truncamiento_lista=nota
    )

@app.get("/ley_html", response_model=ArticuloHTML, summary="Consultar artículo (HTML scraping)")
async def consultar_articulo_html(
    id_norma: Optional[str] = Query(None, description="IDNorma (snake_case)"),
    idNorma:   Optional[str] = Query(None, description="IDNorma (camelCase)"),
    id_parte:  Optional[str] = Query(None, description="IdParte (snake_case)"),
    idParte:   Optional[str] = Query(None, description="IdParte (camelCase)")
):
    inorma = id_norma or idNorma
    iparte = id_parte or idParte
    if not inorma or not iparte:
        raise HTTPException(
            status_code=422,
            detail="Debes pasar `id_norma` o `idNorma` Y `id_parte` o `idParte`."
        )
    url = f"https://www.bcn.cl/leychile/navegar?idNorma={inorma}&idParte={iparte}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, timeout=10)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Código %s al obtener HTML: %s",
                exc.response.status_code,
                exc.request.url,
            )
            raise HTTPException(502, "Error al obtener HTML de la BCN")
        except httpx.RequestError as exc:
            logger.warning("Error de conexión al obtener HTML: %s", exc.request.url)
            raise HTTPException(503, "No pude conectarme a la BCN")
        soup = BeautifulSoup(r.text, "html.parser")
        sel = soup.select_one(f"div.textoNorma[id*='{iparte}'], div[id*='{iparte}']")
        if not sel:
            return ArticuloHTML(
                idNorma=inorma,
                idParte=iparte,
                url_fuente=url,
                selector_usado="ninguno",
                texto_html_extraido=f"⚠️ No encontré el artículo. Sigue este enlace:\n{url}"
            )
        txt = limpiar_texto(sel.get_text("\n"))
        if len(txt) > MAX_TEXT_LENGTH:
            txt = txt[:MAX_TEXT_LENGTH] + TRUNC_TEXT
        return ArticuloHTML(
            idNorma=inorma,
            idParte=iparte,
            url_fuente=url,
            selector_usado=sel.name + (f"#{sel.get('id')}" if sel.get("id") else ""),
            texto_html_extraido=txt
        )

@app.get("/health", summary="Estado del servicio")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

