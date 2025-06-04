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
python main.py
```

La API puede personalizarse mediante variables de entorno:

- `PORT`: Puerto donde se ejecuta Uvicorn (por defecto `8000`).
- `FALLBACKS_FILE`: Ruta del archivo JSON con IDs de respaldo para las leyes.
- `MAX_TEXT_LENGTH`: Máximo de caracteres del texto devuelto por artículo.
- `MAX_ARTICULOS_RETURNED`: Límite de artículos incluidos en la respuesta.
