"""Revisor final: nada sale al cliente si trae un precio que no calculó el cotizador o rompe el estilo."""
import re

# $280.000 · 280.000 · $280000 · 280 mil · 280 lucas
_MONTO = re.compile(r"\$\s?\d[\d\.]*|\b\d{1,3}(?:\.\d{3})+\b|\b\d{4,7}\b(?=\s*(?:pesos|clp))|\b\d{2,3}\s*(?:mil|lucas)\b", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF⭐✅❌]")
_LISTA = re.compile(r"^\s*(?:[-•*·]|\d+[\.\)])\s+", re.M)
_SE_CONFIRMA = re.compile(r"(valor|precio|monto|llave)[^.\n]{0,40}se confirma|se confirma al (revisar|ver|identificar)|valor referencial|precio referencial|precio final", re.I)
_NOMBRE = re.compile(r"\bjes[uú]s\b|\bperdomo\b", re.I)
_HORA_ACOMODA = re.compile(r"(a\s+)?qu[eé]\s+hora\s+te\s+acomoda", re.I)
_FECHA = re.compile(r"\b\d{1,2}[\./-]\d{1,2}(?:[\./-]\d{2,4})?\b")


def _valor(txt: str) -> int | None:
    t = txt.lower().replace("$", "").replace(" ", "")
    if "mil" in t or "luca" in t:
        n = re.sub(r"\D", "", t)
        return int(n) * 1000 if n else None
    n = re.sub(r"\D", "", t)
    return int(n) if n else None


_PRESENTACION = re.compile(r"te escribe jes[uú]s(,)? ingeniero", re.I)


def revisar(mensajes: list[str], montos_permitidos: set[int], permitir_presentacion: bool = False) -> list[str]:
    """Devuelve la lista de problemas (vacía = se puede enviar)."""
    problemas = []
    texto = "\n".join(mensajes)
    sin_fechas = _FECHA.sub(" ", texto)
    for m in _MONTO.findall(sin_fechas):
        v = _valor(m)
        if v is None or v < 1000:      # años, horas, números chicos
            continue
        if 1900 <= v <= 2100 and "$" not in m:   # un año, no un precio
            continue
        if v not in montos_permitidos:
            problemas.append(f"El monto {m.strip()} no salió del cotizador en esta respuesta. Llama cotizar o quítalo.")
    if _SE_CONFIRMA.search(texto):
        problemas.append("No digas que el precio o la llave se confirma al revisar el auto ni que es referencial: el precio que das es el precio. Quita esa frase.")
    if _NOMBRE.search(_PRESENTACION.sub(" ", texto) if permitir_presentacion else texto):
        problemas.append("No menciones el nombre de Jesús ni de ninguna persona: habla como Autoprog.")
    if _HORA_ACOMODA.search(texto):
        problemas.append("No preguntes 'a qué hora te acomoda' ni propongas hora: pregunta si le gustaría la visita y que el cliente diga cuándo le viene bien.")
    if _EMOJI.search(texto):
        problemas.append("Hay emojis. Quítalos.")
    if _LISTA.search(texto):
        problemas.append("Hay listas o viñetas. Escríbelo en frases corridas.")
    for m in mensajes:
        if len(m) > 420:
            problemas.append("Un mensaje es muy largo. Máximo 2 a 3 líneas por mensaje.")
            break
    if re.search(r"\*\*|__", texto):
        problemas.append("Hay formato (negritas). Texto plano.")
    return problemas
