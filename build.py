#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construye el catalogo publico de TV Guia desde fuentes abiertas.

Corre igual en GitHub Actions (sin secretos) o en local:

    python build.py                # con sondeo HTTP (CI)
    python build.py --sin-sondeo   # solo parseo (local / prueba)

Salidas en ./public:
    catalog.json   {version, count, channels:[{n,name,g,c,lang,ok,logo,url}]}
    tv-guia.m3u    la misma seleccion
    version        fecha ISO

Fuentes: iptv-org (indice global + API de canales) y TDTChannels (Espana).
Reglas de exclusion: las del escaneo de seguridad del proyecto original — nada con
credenciales en la URL, claves, hosts de red privada ni tokens largos en la ruta.
"""
import argparse
import base64
import binascii
import gzip
import json
import re
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SALIDA = Path(__file__).resolve().parent / "public"
UA = {"User-Agent": "tv-guia-data/1.0 (+https://github.com/cjtemer-kaos)"}

FUENTES = [
    ("iptv-org", "https://iptv-org.github.io/iptv/index.m3u"),
    ("tdtchannels", "https://www.tdtchannels.com/lists/tv.m3u8"),
]
API_CANALES = "https://iptv-org.github.io/api/channels.json"

EXTINF = re.compile(r"^#EXTINF:(-?\d+(?:\.\d+)?)\s*(.*?),(.*)$", re.I)
ATTR = re.compile(r'([a-zA-Z0-9\-]+)="([^"]*)"')

CRED = re.compile(r"[?&][\w-]*(token|key|pass|pwd|auth|sig|mac)[\w-]*=", re.I)
MAC = re.compile(r"\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
FIRMADA = re.compile(r"[?&](akes|hdnts|expires|policy|signature|sp|sv|se|sr)=", re.I)
B64_LARGO = re.compile(r"[?&][\w-]*=([A-Za-z0-9_\-+/=]{40,})")
SK = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}")
PRIVADA = re.compile(r"^https?://(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)")
TOKEN_LARGO = re.compile(r"/[A-Fa-f0-9]{24,}/|[A-Fa-f0-9]{32,}")
CALIDAD = re.compile(r"\((\d{3,4})p\)", re.I)

REGLAS = [
    ("Deportes", r"deport|sport|futbol|fútbol|espn|dazn|motor|racing|nfl|nba|ufc|golf|tenis|tennis"),
    ("Cine", r"pel[ií]c|movie|cine|film|western"),
    ("Series", r"serie|series"),
    ("Infantil", r"infantil|kids|animac|animat|cartoon|family|familia"),
    ("Documentales", r"document|docu|naturaleza|nature|history|historia"),
    ("Música", r"music|m[uú]sica|radio|hits|mtv|vevo"),
    ("Noticias", r"noticia|news|informativ|weather|clima|actualidad"),
    ("Religión", r"religi|iglesia|church|cristian|católic|catolic|islam|juda|evangel"),
    ("Cultura", r"cultura|culture|educac|educat|arte|art|teatro|science|ciencia"),
    ("Estilo de vida", r"lifestyle|viaje|travel|outdoor|cocina|cooking|shop|hogar|relax|jardin|moda|fashion|salud|health"),
    ("Negocios", r"business|negocio|legislat|finanz|finance|econom"),
    ("Entretenimiento", r"entertainment|entretenimiento|comedy|comedia|classic|variedad|reality|talk|game|musical"),
    ("Generalistas", r"general|generalista"),
    ("Sin clasificar", r"undefined|sin grupo"),
]

# pais -> idioma principal (solo el bloque hispano + los grandes)
LANG_PAIS = {
    "ES": "spa", "AR": "spa", "MX": "spa", "UY": "spa", "CL": "spa", "CO": "spa", "PE": "spa",
    "VE": "spa", "EC": "spa", "BO": "spa", "PY": "spa", "DO": "spa", "CR": "spa", "GT": "spa",
    "HN": "spa", "NI": "spa", "PA": "spa", "PR": "spa", "SV": "spa", "CU": "spa",
    "BR": "por", "PT": "por", "US": "eng", "GB": "eng", "UK": "eng", "CA": "eng", "AU": "eng",
    "DE": "deu", "AT": "deu", "CH": "deu", "FR": "fra", "BE": "fra", "IT": "ita", "RU": "rus",
    "TR": "tur", "NL": "nld", "SE": "swe", "PL": "pol", "IN": "hin", "JP": "jpn", "KR": "kor",
    "CN": "zho", "TW": "zho", "HK": "zho", "SA": "ara", "AE": "ara", "EG": "ara", "MA": "ara",
}


def bajar(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_m3u(texto):
    salida, cur = [], None
    for raw in texto.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.upper().startswith("#EXTINF"):
            m = EXTINF.match(ln)
            if not m:
                cur = None
                continue
            attrs = dict(ATTR.findall(m.group(2) or ""))
            cur = {
                "id": attrs.get("tvg-id", ""),
                "name": (m.group(3) or "").strip(),
                "logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title") or "",
                "url": "",
            }
            continue
        if ln.startswith("#"):
            continue
        if cur is not None and ln.startswith(("http", "rtmp")):
            cur["url"] = ln
            salida.append(cur)
            cur = None
    return salida


def jwt_embebido(url):
    """Detecta un JWT o token escondido en base64 dentro de un parametro."""
    for m in B64_LARGO.finditer(url):
        v = m.group(1)
        pad = v + "=" * (-len(v) % 4)
        for dec in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                txt = dec(pad).decode("utf-8", "ignore")
            except (binascii.Error, ValueError):
                continue
            if "eyJ" in txt or "token=" in txt or "jwt" in txt.lower():
                return True
    return False


def motivo_exclusion(url):
    if not url.startswith(("http://", "https://")):
        return "no http(s)"
    if CRED.search(url):
        return "credencial en URL"
    if MAC.search(url):
        return "MAC de portal en URL"
    if JWT.search(url):
        return "JWT en URL"
    if FIRMADA.search(url):
        return "URL firmada"
    if SK.search(url):
        return "clave sk-"
    if PRIVADA.search(url):
        return "host de red privada"
    if TOKEN_LARGO.search(url):
        return "token/ID largo en la ruta"
    if jwt_embebido(url):
        return "JWT embebido (base64)"
    return None


def pais_de(canal_id):
    m = re.search(r"\.([a-z]{2})$", canal_id or "")
    return m.group(1).upper() if m else ""


def grupo_de(texto):
    t = (texto or "").lower()
    for canonico, patron in REGLAS:
        if re.search(patron, t):
            return canonico
    return "Sin clasificar"


def clave_nombre(nombre):
    n = CALIDAD.sub("", nombre or "")
    n = re.sub(r"\[[^\]]*\]", "", n)
    return re.sub(r"[^a-z0-9]+", "", n.lower())


def probar(url, timeout=8):
    """Sondeo barato: alcanza con que responda y devuelva algo."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            trozo = r.read(2048)
            return r.status == 200 and len(trozo) > 0
    except Exception:
        return False


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-sondeo", action="store_true")
    ap.add_argument("--limite-sondeo", type=int, default=0)
    a = ap.parse_args(argv)

    todos, excluidos = [], Counter()
    for etiqueta, url in FUENTES:
        try:
            texto = bajar(url)
        except Exception as e:
            print(f"!! {etiqueta}: {e}")
            continue
        lote = parse_m3u(texto)
        print(f".. {etiqueta:12} {len(lote):5} canales")
        for c in lote:
            c["origen"] = etiqueta
            todos.append(c)

    # metadatos por tvg-id desde la API (idiomas y pais reales)
    meta = {}
    try:
        api = json.loads(bajar(API_CANALES, timeout=120))
        for x in api:
            meta[(x.get("id") or "").lower()] = x
        print(f".. api          {len(meta):5} canales con metadatos")
    except Exception as e:
        print(f"!! api: {e}")

    # dedupe por URL y por nombre, quedando la mejor calidad
    por_url, unicos = set(), []
    for c in todos:
        if c["url"] in por_url:
            continue
        por_url.add(c["url"])
        unicos.append(c)
    por_nombre = {}
    for c in unicos:
        k = clave_nombre(c["name"]) or c["url"]
        prev = por_nombre.get(k)
        if prev is None:
            por_nombre[k] = c
        else:
            q1 = int((CALIDAD.search(c["name"]) or [0, 0])[1] or 0)
            q2 = int((CALIDAD.search(prev["name"]) or [0, 0])[1] or 0)
            if q1 > q2:
                por_nombre[k] = c
    final = list(por_nombre.values())

    limpios = []
    for c in final:
        motivo = motivo_exclusion(c["url"])
        if motivo:
            excluidos[motivo] += 1
            continue
        base_id = (c["id"] or "").split("@")[0].lower()
        info = meta.get(base_id, {})
        pais = (info.get("country") or pais_de(c["id"]) or "").upper()
        lang = LANG_PAIS.get(pais, "")
        cats = info.get("categories") or []
        grupo = grupo_de(c["group"] or (cats[0] if cats else "") or c["name"])
        limpios.append({
            "name": c["name"],
            "g": grupo,
            "c": pais,
            "lang": lang,
            "logo": c["logo"],
            "url": c["url"],
        })

    print(f"\nleidos {len(todos)} | unicos por nombre {len(final)} | publicables {len(limpios)}")
    for k, v in excluidos.most_common():
        print(f"  excluidos {v:>5}  {k}")

    # sondeo
    for c in limpios:
        c["ok"] = None   # None = desconocido; el sondeo lo pasa a true/false
    if a.sin_sondeo:
        print("sondeo: salteado")
    else:
        objetivo = limpios[: a.limite_sondeo] if a.limite_sondeo else limpios
        print(f"sondeando {len(objetivo)} URLs...", flush=True)
        with ThreadPoolExecutor(max_workers=48) as ex:
            resultados = list(ex.map(lambda c: probar(c["url"]), objetivo))
        for c, ok in zip(objetivo, resultados):
            c["ok"] = ok
        vivos = sum(1 for c in objetivo if c["ok"])
        print(f"  vivos: {vivos}/{len(objetivo)}")

    # orden: vivos primero, luego grupo, luego nombre
    limpios.sort(key=lambda c: (c["ok"] is not True, c["g"], c["name"].lower()))
    for i, c in enumerate(limpios, 1):
        c["n"] = i

    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    catalogo = {
        "version": ahora,
        "count": len(limpios),
        "vivos": sum(1 for c in limpios if c["ok"] is True),
        "channels": [
            {"n": c["n"], "name": c["name"], "g": c["g"], "c": c["c"], "lang": c["lang"],
             "ok": c["ok"], "logo": c["logo"], "url": c["url"]}
            for c in limpios
        ],
    }

    SALIDA.mkdir(exist_ok=True)
    texto = json.dumps(catalogo, ensure_ascii=False, separators=(",", ":"))
    (SALIDA / "catalog.json").write_text(texto, encoding="utf-8")
    (SALIDA / "catalog.json.gz").write_bytes(gzip.compress(texto.encode("utf-8"), 9))
    (SALIDA / "version").write_text(ahora + "\n", encoding="utf-8")

    lineas = ["#EXTM3U", f"# TV Guia - {len(limpios)} canales - {ahora}"]
    for c in limpios:
        lineas.append(f'#EXTINF:-1 tvg-logo="{c["logo"]}" group-title="{c["g"]}",{c["name"]}')
        lineas.append(c["url"])
    (SALIDA / "tv-guia.m3u").write_text("\n".join(lineas) + "\n", encoding="utf-8")

    print(f"\npublic/: catalog.json {len(texto)/1024:.0f} KB · m3u "
          f"{(SALIDA/'tv-guia.m3u').stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
