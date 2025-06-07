import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "mensaje" in data
    assert "version" in data

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ley_articulo():
    params = {"numero_ley": "21595", "articulo": "1"}
    response = client.get("/ley", params=params)
    assert response.status_code == 200
    data = response.json()
    assert data["ley"] == "21595"
    assert data["articulos_totales_en_respuesta"] == 1
    assert len(data["articulos"]) == 1
    assert data["articulos"][0]["texto"]


def test_ley_html():
    params = {"id_norma": "1195119", "id_parte": "10449614"}
    response = client.get("/ley_html", params=params)
    assert response.status_code == 200
    data = response.json()
    assert data["idNorma"] == "1195119"
    assert data["idParte"] == "10449614"
    assert data["texto_html_extraido"]
