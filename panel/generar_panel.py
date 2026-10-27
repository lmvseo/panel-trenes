#!/usr/bin/env python3
"""
Genera panel.json con los trenes de Renfe con retraso que pasan por Valencia.

Qué hace, en cada ejecución:
  1. Carga (o descarga una vez al día) el horario oficial GTFS de Renfe y se queda
     con los trenes de hoy que pasan por una estación de València.
  2. Lee el feed oficial en tiempo real (GTFS-RT trip updates) y mira el retraso
     de cada uno de esos trenes.
  3. Escribe panel.json con el formato que espera el panel HTML.

Uso:
  python generar_panel.py --salida salida/panel.json --cache cache/estatico.json
  python generar_panel.py --diagnostico      (muestra qué ha encontrado, para ajustar)

Si no consigue datos fiables para ninguna tarjeta, termina con error y NO
escribe panel.json, para no publicar números falsos. Si solo consigue algunas
tarjetas (por ejemplo Cercanías sí y larga distancia no), publica solo esas.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from zoneinfo import ZoneInfo

import requests

try:
    from google.transit import gtfs_realtime_pb2
except ImportError:  # permite importar el módulo sin la librería (solo para pruebas)
    gtfs_realtime_pb2 = None

# =============================== CONFIGURACIÓN ===============================
ZONA = ZoneInfo("Europe/Madrid")
UMBRAL_MIN = 5  # minutos a partir de los que un tren cuenta como retrasado

# Horarios estáticos: comprobados (funcionan).
# Tiempo real:
#   - Cercanías: gtfsrt.renfe.com/trip_updates.pb (confirmado en MobilityDatabase).
#   - Larga/Media Distancia: gtfsrt.renfe.com/trip_updates_LD.json (en formato JSON,
#     confirmado en data.renfe.com). Se prueban las direcciones en orden y se usa la
#     primera que responda y encaje con el horario; si ninguna sirve, se omite esa tarjeta.
FUENTES = {
    "cercanias": {
        "gtfs_zip": "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip",
        "trip_updates": ["https://gtfsrt.renfe.com/trip_updates.pb"],
    },
    "larga": {  # Alta Velocidad, Larga y Media Distancia
        "gtfs_zip": "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip",
        "trip_updates": [
            "https://gtfsrt.renfe.com/trip_updates_LD.pb",    # probado: funciona con datos reales
            "https://gtfsrt.renfe.com/trip_updates_LD.json",  # alternativa en JSON (data.renfe.com)
        ],
    },
}

# Tarjetas del panel, en orden: (clave de grupo, nombre que se muestra)
TARJETAS = [
    ("cercanias", "Cercanías Valencia"),
    ("md", "Media Distancia"),
    ("ld", "Larga Distancia y Alta Velocidad"),
]

# Un tren "pasa por Valencia" si alguna parada contiene "valencia" (sin acentos,
# así que cubre "València-...")...
EXCLUIR_PARADAS = ("de alcantara", "don juan", "ventoso")  # ...salvo estas Valencias
# Un tren del fichero AV/LD se considera Media Distancia si su nombre de ruta
# contiene alguna de estas palabras. Con los datos reales de hoy: REGIONAL y MD
# van a Media Distancia; AVE, EUROMED, AVANT, AVLO, ALVIA, Intercity y REG.EXP.
# van a Larga Distancia.
PALABRAS_MD = {"md", "regional"}

CABECERAS = {"User-Agent": "panel-trenes/1.0 (redaccion digital)"}


# ================================ UTILIDADES =================================
def norm(texto):
    t = unicodedata.normalize("NFD", texto or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.lower().strip()


def es_parada_valencia(nombre):
    n = norm(nombre)
    return "valencia" in n and not any(x in n for x in EXCLUIR_PARADAS)


def es_media_distancia(corto, largo):
    tokens = set(re.findall(r"[a-z0-9]+", norm(corto + " " + largo)))
    return bool(tokens & PALABRAS_MD)


def clave_flexible(trip_id):
    """Forma simplificada de un trip_id para casar el horario con el tiempo real.
    Quita una fecha final (AAAA-MM-DD) y los ceros a la izquierda:
    '0046012026-10-27' -> '4601'."""
    t = re.sub(r"-?\d{4}-\d{2}-\d{2}$", "", trip_id or "")
    t = t.lstrip("0")
    return t


def clave_nucleo(trip_id):
    """Para identificadores de Cercanías tipo '4098M18094C6': (núcleo, resto) sin
    los dos dígitos de versión. '4098M18094C6' y '4198M18094C6' dan la misma clave
    ('40', 'M18094C6'); '5198M18094C6' no (otro núcleo)."""
    m = re.match(r"^(\d{2})\d{2}(M\d+.*)$", trip_id or "")
    return (m.group(1), m.group(2)) if m else None


def hora_hhmm(valor):
    try:
        h, m = valor.strip().split(":")[:2]
        return "%02d:%02d" % (int(h) % 24, int(m))
    except Exception:
        return ""


def descargar(url):
    r = requests.get(url, timeout=60, headers=CABECERAS)
    r.raise_for_status()
    return r.content


def miembro(zf, nombre):
    for n in zf.namelist():
        if n.split("/")[-1] == nombre:
            return n
    return None


def leer_csv(zf, nombre):
    real = miembro(zf, nombre)
    if real is None:
        return
    with zf.open(real) as f:
        for fila in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")):
            yield {(k or "").strip(): (v or "").strip() for k, v in fila.items()}


def servicios_activos(zf, hoy):
    ymd = hoy.strftime("%Y%m%d")
    dia = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][hoy.weekday()]
    activos = set()
    for r in leer_csv(zf, "calendar.txt"):
        if r["start_date"] <= ymd <= r["end_date"] and r.get(dia) == "1":
            activos.add(r["service_id"])
    for r in leer_csv(zf, "calendar_dates.txt"):
        if r["date"] == ymd:
            if r["exception_type"] == "1":
                activos.add(r["service_id"])
            elif r["exception_type"] == "2":
                activos.discard(r["service_id"])
    return activos


# ============================ HORARIO ESTÁTICO (GTFS) ========================
def procesar_estatico(datos_zip, hoy, es_cercanias):
    """Devuelve ({trip_id: {hora, destino, linea, grupo}}, info_diagnostico)."""
    zf = zipfile.ZipFile(io.BytesIO(datos_zip))
    nombres, valencia = {}, set()
    for r in leer_csv(zf, "stops.txt"):
        nombres[r["stop_id"]] = r.get("stop_name", "")
        if es_parada_valencia(r.get("stop_name", "")):
            valencia.add(r["stop_id"])

    activos = servicios_activos(zf, hoy)
    rutas = {r["route_id"]: (r.get("route_short_name", ""), r.get("route_long_name", ""))
             for r in leer_csv(zf, "routes.txt")}
    viajes = {r["trip_id"]: r for r in leer_csv(zf, "trips.txt") if r["service_id"] in activos}

    info = {}
    for r in leer_csv(zf, "stop_times.txt"):
        t = r["trip_id"]
        if t not in viajes:
            continue
        try:
            seq = int(r["stop_sequence"])
        except ValueError:
            continue
        hora = r.get("departure_time") or r.get("arrival_time") or ""
        e = info.get(t)
        if e is None:
            info[t] = {"min": seq, "hora": hora, "max": seq, "dest": r["stop_id"], "val": False}
            e = info[t]
        else:
            if seq < e["min"]:
                e["min"], e["hora"] = seq, hora
            if seq > e["max"]:
                e["max"], e["dest"] = seq, r["stop_id"]
        if r["stop_id"] in valencia:
            e["val"] = True

    trips, nombres_ruta = {}, Counter()
    for t, e in info.items():
        if not e["val"]:
            continue
        corto, largo = rutas.get(viajes[t].get("route_id", ""), ("", ""))
        if es_cercanias:
            grupo = "cercanias"
        else:
            grupo = "md" if es_media_distancia(corto, largo) else "ld"
        nombres_ruta[(grupo, corto or largo)] += 1
        trips[t] = {
            "hora": hora_hhmm(e["hora"]),
            "destino": viajes[t].get("trip_headsign") or nombres.get(e["dest"], ""),
            "linea": corto or largo,
            "grupo": grupo,
        }
    diag = {
        "paradas_valencia": sorted({nombres[s] for s in valencia})[:20],
        "servicios_activos_hoy": len(activos),
        "viajes_hoy": len(viajes),
        "viajes_por_valencia": len(trips),
        "rutas": nombres_ruta.most_common(25),
    }
    return trips, diag


# ============================ TIEMPO REAL (GTFS-RT) ==========================
def retraso_segundos(tu):
    if tu.HasField("delay"):
        return tu.delay
    for stu in tu.stop_time_update:
        for campo in ("arrival", "departure"):
            if stu.HasField(campo) and getattr(stu, campo).HasField("delay"):
                return getattr(stu, campo).delay
    return 0


def _pick(d, *nombres):
    """Primer valor presente entre varias grafías de una clave (snake_case / camelCase)."""
    if isinstance(d, dict):
        for n in nombres:
            if n in d and d[n] is not None:
                return d[n]
    return None


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def entradas_pb(contenido):
    """Feed binario (protobuf): lista de (trip_id, retraso_en_segundos)."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(contenido)
    return [(ent.trip_update.trip.trip_id, retraso_segundos(ent.trip_update))
            for ent in feed.entity if ent.HasField("trip_update")]


def entradas_json(datos):
    """Feed en JSON: acepta la estructura GTFS-RT (snake_case o camelCase) o una
    lista plana. Devuelve lista de (trip_id, retraso_en_segundos)."""
    if isinstance(datos, dict):
        ents = _pick(datos, "entity", "entities", "Entity") or []
    else:
        ents = datos or []
    salida = []
    for ent in ents:
        tu = _pick(ent, "trip_update", "tripUpdate", "TripUpdate") or ent
        trip = _pick(tu, "trip", "Trip") or tu
        tid = _pick(trip, "trip_id", "tripId", "TripId")
        if tid is None:
            continue
        delay = _num(_pick(tu, "delay", "Delay"))
        if delay is None:
            for stu in _pick(tu, "stop_time_update", "stopTimeUpdate", "StopTimeUpdate") or []:
                for campo in ("arrival", "departure", "Arrival", "Departure"):
                    d = _num(_pick(_pick(stu, campo), "delay", "Delay"))
                    if d is not None:
                        delay = d
                        break
                if delay is not None:
                    break
        salida.append((str(tid), delay or 0))
    return salida


def leer_tiempo_real(url, trips):
    """Devuelve (retrasos {trip_id_del_horario: minutos}, entidades_leidas,
    ejemplos_de_trip_id_del_feed)."""
    contenido = descargar(url)
    if url.lower().endswith(".json") or contenido.lstrip()[:1] in (b"{", b"["):
        entradas = entradas_json(json.loads(contenido.decode("utf-8-sig")))
    else:
        entradas = entradas_pb(contenido)

    if not entradas:
        raise ValueError("el feed no contiene trenes con el formato esperado; primeros caracteres: %r"
                         % contenido[:300])

    # Índice flexible: solo claves que identifican un único viaje del horario.
    flex = {}
    for tid in trips:
        flex.setdefault(clave_flexible(tid), []).append(tid)

    nuc = {}
    for tid in trips:
        k = clave_nucleo(tid)
        if k:
            nuc.setdefault(k, []).append(tid)

    retrasos, ejemplos, por_nucleo = {}, [rt_id for rt_id, _ in entradas[:5]], 0
    for rt_id, delay_s in entradas:
        tid = None
        if rt_id in trips:
            tid = rt_id
        else:
            cand = flex.get(clave_flexible(rt_id), [])
            if len(cand) == 1 and clave_flexible(rt_id):
                tid = cand[0]
            else:
                k = clave_nucleo(rt_id)
                cand = nuc.get(k, []) if k else []
                if len(cand) == 1:
                    tid = cand[0]
                    por_nucleo += 1
        if tid is not None:
            retrasos[tid] = max(0, round(delay_s / 60))
    prefijos = Counter(re.match(r"\d{0,2}", r).group(0) for r, _ in entradas)
    extra = {"por_clave_nucleo": por_nucleo, "prefijos_feed": prefijos.most_common(10)}
    return retrasos, len(entradas), ejemplos, extra


# ================================== PRINCIPAL ================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", default="salida/panel.json")
    ap.add_argument("--cache", default="cache/estatico.json")
    ap.add_argument("--diagnostico", action="store_true")
    args = ap.parse_args()

    if gtfs_realtime_pb2 is None:
        sys.exit("Falta la librería: pip install -r requirements.txt")

    hoy = dt.datetime.now(ZONA).date()

    cache = {"fecha": "", "fuentes": {}}
    if os.path.exists(args.cache):
        try:
            with open(args.cache, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
    if cache.get("fecha") != hoy.isoformat():
        cache = {"fecha": hoy.isoformat(), "fuentes": {}}

    # 1) horario estático (una vez al día)
    for clave, cfg in FUENTES.items():
        if clave in cache["fuentes"]:
            continue
        try:
            print("Descargando horario:", cfg["gtfs_zip"])
            trips, diag = procesar_estatico(descargar(cfg["gtfs_zip"]), hoy, clave == "cercanias")
            cache["fuentes"][clave] = trips
            if args.diagnostico:
                print("[diagnóstico %s]" % clave, json.dumps(diag, ensure_ascii=False))
        except Exception as e:
            print("AVISO: no se pudo procesar el horario de %s: %s" % (clave, e), file=sys.stderr)
    os.makedirs(os.path.dirname(args.cache) or ".", exist_ok=True)
    with open(args.cache, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)

    # 2) tiempo real: para cada fuente se prueban sus direcciones en orden y se
    #    usa la primera que responda y cuyos trenes encajen con el horario.
    grupos = {}
    for clave, cfg in FUENTES.items():
        trips = cache["fuentes"].get(clave)
        if not trips:
            continue
        usado = False
        for url in cfg["trip_updates"]:
            try:
                retrasos, entidades, ejemplos, extra = leer_tiempo_real(url, trips)
            except Exception as e:
                print("AVISO: %s no disponible (%s): %s" % (url, clave, e), file=sys.stderr)
                continue
            coinciden = len(retrasos)
            if args.diagnostico:
                print("[diagnóstico %s] %s entidades=%d coincidencias=%d" % (clave, url, entidades, coinciden))
                print("   ejemplos de trip_id en el feed:", ejemplos)
                print("   ejemplos de trip_id en el horario:", list(trips)[:5])
                print("   primeros dígitos de los trip_id del feed (cuántos de cada):", extra["prefijos_feed"])
                print("   primeros dígitos de los trip_id del horario:",
                      Counter(t[:2] for t in trips).most_common(10))
                print("   casados por clave de núcleo:", extra["por_clave_nucleo"])
                if retrasos:
                    print("   retraso máx=%d min, trenes >= %d min: %d"
                          % (max(retrasos.values()), UMBRAL_MIN,
                             sum(1 for v in retrasos.values() if v >= UMBRAL_MIN)))
            if coinciden == 0:
                print("AVISO: ningún tren de %s coincide con el horario de %s." % (url, clave),
                      file=sys.stderr)
                continue
            for t in trips.values():
                grupos.setdefault(t["grupo"], {"total": 0, "trenes": []})["total"] += 1
            for tid, minutos in retrasos.items():
                if minutos >= UMBRAL_MIN:
                    t = trips[tid]
                    grupos[t["grupo"]]["trenes"].append(
                        {"linea": t["linea"], "hora": t["hora"], "destino": t["destino"], "retraso": minutos})
            usado = True
            break
        if not usado:
            print("AVISO: sin tiempo real fiable para %s; se omiten sus tarjetas." % clave, file=sys.stderr)

    servicios = []
    for grupo, nombre in TARJETAS:
        s = grupos.get(grupo)
        if s:
            servicios.append({"tipo": "trenes", "nombre": nombre, "total": s["total"],
                              "unidad": "servicios hoy" if grupo == "cercanias" else "trenes hoy",
                              "trenes": sorted(s["trenes"], key=lambda x: -x["retraso"])})

    if not servicios:
        sys.exit("ERROR: no hay datos fiables para ninguna tarjeta; no se escribe panel.json.")

    salida = {"actualizado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "servicios": servicios}
    os.makedirs(os.path.dirname(args.salida) or ".", exist_ok=True)
    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False)
    print("OK:", ", ".join("%s: %d de %d con retraso" % (s["nombre"], len(s["trenes"]), s["total"])
                           for s in servicios))


if __name__ == "__main__":
    main()
