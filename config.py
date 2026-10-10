"""Configuración del bot. Todo lo sensible viene de variables de entorno (Railway → Variables)."""
import os

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY", "").split() or [""])[0]
MODELO = os.getenv("MODELO", "claude-sonnet-5-5")          # cerebro principal
MODELO_DIFICIL = os.getenv("MODELO_DIFICIL", "claude-opus-5-5")  # reintento si el revisor rechaza

# Private Integration Token de GHL. Solo la primera palabra: si al pegarlo quedó texto extra, se descarta.
GHL_TOKEN = (os.getenv("GHL_TOKEN", "").split() or [""])[0]
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "GQTHBIr92oupzd54Ui7X")
GHL_CALENDARIO_ID = os.getenv("GHL_CALENDARIO_ID", "EjeX9T2HQO3sriUhsA8R")  # "Servicio a domicilio Autoprog"
GHL_USUARIO_JESUS = os.getenv("GHL_USUARIO_JESUS", "wxVhUI6b9Gf5xYTtKroz")

# Clave compartida para que solo GHL pueda llamar al webhook (se pone igual en el workflow de GHL)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "q2X3R0XWMxBSiAMwIaoGnJTK")

# MODO: "apagado" (no hace nada) | "borrador" (deja la respuesta como nota interna) | "activo" (responde al cliente)
MODO = os.getenv("MODO", "apagado")

URL_PUBLICA = os.getenv("URL_PUBLICA", "https://web-production-0c666.up.railway.app")
ZONA_HORARIA = "America/Santiago"

# Espera tras el último mensaje del cliente antes de responder (junta mensajes seguidos)
ESPERA_SEGUNDOS = int(os.getenv("ESPERA_SEGUNDOS", "12"))
# Si Jesús escribió a mano en la conversación en las últimas N horas, el bot no se mete
SILENCIO_SI_HUMANO_HORAS = float(os.getenv("SILENCIO_SI_HUMANO_HORAS", "6"))

PIPELINES = {
    "BMW MINI": {"id": "wOnfbY9gUlsK5ou6xawJ", "etapas": {
        "Calificando": "fadb6cf7-3229-4a03-bbf6-8f3fe6c5337e",
        "Escalado a Jesús": "33517732-5369-44e5-8b42-244fdc006799",
        "Cotizado": "ed4bd73c-27b9-488e-a3e3-2501c96c6e22",
        "Agendado": "27163b5a-7f06-427b-b364-f9fd5e5b0bb9"}},
    "FCA": {"id": "l8cwnRR008fjCeZNotlt", "etapas": {
        "Calificando": "ac1c9106-a278-4b90-bf47-baff1c93bc50",
        "Escalado a Jesús": "ac1c9106-a278-4b90-bf47-baff1c93bc50",
        "Cotizado": "9c6223cb-bca1-48c7-8b28-cd7665f93117",
        "Agendado": "addc55df-0493-4b8c-803c-83e667cf4d57"}},
    "VAG": {"id": "1Gp2OdsEh1zMeKysajSv", "etapas": {
        "Calificando": "8b4cf1c0-f8a5-46cc-8ef2-712cbb4f927c",
        "Escalado a Jesús": "caf2f131-a902-452a-9ecb-76ed94c48b62",
        "Cotizado": "1cbfb578-9bfb-4089-ab1f-d82c94be900d",
        "Agendado": "ce272385-c53e-4465-adf2-8440e54ac8d7"}},
    "MULTIMARCA": {"id": "3NjiHVj8ujaG2GzoL1QU", "etapas": {
        "Calificando": "7e648622-1348-4c85-8441-aba9df8a5594",
        "Escalado a Jesús": "7e648622-1348-4c85-8441-aba9df8a5594",
        "Cotizado": "52264950-21e4-416e-803d-4b57f5964d93",
        "Agendado": "d45d4672-95aa-4477-b1d2-f283b871625b"}},
}

CAMPOS = {  # ids de campos personalizados del contacto en GHL
    "marca": "wY8XrrAUzhUTCgUTJReR", "modelo": "AgM3xV8Q5Zx3KzzMdLvJ", "anio": "tWxH2FmvgacHiAVjEgpW",
    "comuna_detectada": "6Sgnc7aXDWcIk7sgynhM", "sistema_immo": "xaErSKY47TOQLEnzEreb",
    "llave_actual": "uEzP8DvfAcqsGM4yqTnn", "llave_deseada": "hwBK1H7XxJgYNsO8lmcH",
    "tipo_cliente": "16Ds10Xf6todHUETGqN9",
}

ETIQUETA_BOT_OFF = "bot-off"            # Jesús la pone y el bot no vuelve a hablarle a ese contacto
ETIQUETA_ESCALADO = "bot-escalado"      # el bot la pone al pasar el caso a Jesús (y se calla)

IMAGENES = {  # catálogo que el bot puede mandar
    "catalogo_general": "catalogo-general.jpg",   # llaves 1 a 8
    "selector_serie_g": "selector-g.jpg",          # 6A, 6B, 7, 8
    "selector_llave_4": "selector-4.jpg",          # variantes 4A-4H
    "selector_llave_5": "selector-5.jpg",          # variantes 5A-5D
    "selector_mini": "selector-mini.jpg",
}


def pipeline_por_marca(marca: str | None) -> str:
    m = (marca or "").lower()
    if any(x in m for x in ("jeep", "dodge", "ram", "chrysler", "fiat")):
        return "FCA"
    if any(x in m for x in ("vw", "volkswagen", "audi", "porsche", "seat", "skoda", "cupra")):
        return "VAG"
    if m and not any(x in m for x in ("bmw", "mini")):
        return "MULTIMARCA"
    return "BMW MINI"
