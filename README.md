# Consulta Leyes Chile API

Esta API permite consultar leyes chilenas por su número, y acceder a sus artículos completos o a uno específico. Utiliza la fuente oficial [Ley Chile](https://www.bcn.cl/leychile).

## 🛠 Tecnologías usadas

- FastAPI
- Uvicorn
- BeautifulSoup
- requests
- lxml

## 📦 Instalación

```bash
pip install -r requirements.txt
```

## 🚀 Ejecución

Para iniciar el servidor en modo local utiliza:

```bash
uvicorn main:app --reload
```

Luego visita `http://localhost:8000/docs` para ver la documentación interactiva.
