# tv-guia-data

Catálogo de canales para la app **TV Guía**, construido y actualizado por GitHub Actions.
No necesita servidor propio: la app lee `public/catalog.json` por HTTPS.

## Qué hay acá

| Archivo | Qué es |
|---|---|
| `build.py` | baja las listas, deduplica, normaliza grupos e idioma, sondea y escribe `public/` |
| `public/catalog.json` | lo que consume la app: `{n, name, g, c, lang, ok, logo, url}` |
| `public/catalog.json.gz` | la misma cosa comprimida (~480 KB) |
| `public/tv-guia.m3u` | la selección como lista M3U, para VLC u otro reproductor |
| `public/version` | fecha ISO de la última corrida |
| `.github/workflows/actualizar.yml` | cron diario 05:10 UTC + ejecución manual |

`ok` es el resultado del sondeo: `true` responde, `false` no, `null` sin sondear (el workflow lo
completa). La app debe tratar `null` como desconocido, no como caído.

## Fuentes

- [iptv-org](https://github.com/iptv-org/iptv) — índice global y metadatos por canal (país, categorías).
- [TDTChannels](https://www.tdtchannels.com/) — TDT española y canales FAST en castellano.

Los streams son de sus respectivos dueños; este repo sólo publica la indexación. Verificá los
derechos de lo que mirás en tu país.

## Reglas de publicación (seguridad)

Se excluyen del catálogo público los canales cuya URL:

- tiene credenciales en el query (`token`, `key`, `auth`, `sig`, `pass`…),
- contiene una clave con forma `sk-…`,
- apunta a un host de red privada (`192.168.`, `10.`, `172.16-31.`),
- lleva un token/ID hexadecimal largo en la ruta (no se puede distinguir de un token de cuenta).

En la última corrida eso dejó afuera ~770 entradas. El catálogo resultante no contiene datos
personales, rutas locales ni configuraciones de ningún equipo.

## Normalización

- **Grupos**: 14 canales canónicos en castellano (Deportes, Cine, Series, Infantil, Documentales,
  Música, Noticias, Religión, Cultura, Estilo de vida, Negocios, Entretenimiento, Generalistas,
  Sin clasificar), derivados del `group-title` y de las categorías de iptv-org.
- **Idioma**: derivado del país del canal (`spa`, `eng`, `por`, …).

## Uso desde la app

```
GET https://raw.githubusercontent.com/cjtemer-kaos/tv-guia-data/main/public/catalog.json
If-None-Match: <etag de la copia local>
```

Con `ETag` la app sólo descarga cuando cambió. Si no hay red, usa la última copia guardada.

## Local

```bash
python build.py               # con sondeo
python build.py --sin-sondeo  # sólo parseo, ok=null
python build.py --limite-sondeo 200
```

Sin dependencias: Python 3.10+ y biblioteca estándar.

## Licencia

MIT para el código de este repo.
