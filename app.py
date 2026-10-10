"""Servidor del bot de WhatsApp de Autoprog (en construcción)."""
from fastapi import FastAPI
from cotizador import cotizar

app = FastAPI(title="Autoprog bot")


@app.get("/")
def salud():
    return {"ok": True, "servicio": "autoprog-bot"}


@app.get("/cotizar")
def probar_cotizacion(modelo: str, anio: int, lugar: str, situacion: str = "duplicado"):
    return cotizar(modelo, anio, lugar, situacion).dict()
