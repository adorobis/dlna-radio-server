"""A tiny DLNA media server for internet radio streams.

It lists the stations of a YAML file as a UPnP/DLNA MediaServer, so renderers
that browse servers themselves (Yamaha MusicCast's "Server" input) can play
them and store them as favourites.

The codec of each stream is detected with ffprobe. Codecs the renderers play
(DLNA_RADIO_PASSTHROUGH, default mp3) are passed through untouched, anything
else (e.g. HE-AAC, which MusicCast can't decode) is transcoded to MP3 by
ffmpeg. A station can force either way with ``transcode: true|false``.

Configuration (environment):
    DLNA_RADIO_CONFIG       stations file (default /config/stations.yaml)
    DLNA_RADIO_PORT         HTTP port (default 8357)
    DLNA_RADIO_HOST         IP to announce (default: the host's outgoing IP)
    DLNA_RADIO_BITRATE      MP3 bitrate when transcoding (default 256k)
    DLNA_RADIO_PASSTHROUGH  codecs served as they are (default mp3; also: flac)
    DLNA_RADIO_FOLDER       name of the folder with the stations (default Stacje)
    DLNA_RADIO_DEBUG        1 for debug logging

Stations file:
    name: Radio            # server name shown on the renderers
    stations:
      - name: My station
        url: https://example.com/stream
        logo: https://...  # optional
        id: my-station     # optional, keeps favourites when the name changes
        transcode: true    # optional: true = always MP3, false = never

The file is re-read when it changes; no restart is needed. Stations can also
be added and removed on the server's web page (http://<ip>:<port>/).
"""

from __future__ import annotations

import asyncio
import contextlib
from email.utils import formatdate
import hashlib
import logging
import os
from pathlib import Path
import random
import signal
import socket
import struct
import time
from typing import Any
import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import aiohttp
from aiohttp import web
import yaml

_LOGGER = logging.getLogger("dlna_radio")

CONFIG = Path(os.environ.get("DLNA_RADIO_CONFIG", "/config/stations.yaml"))
PORT = int(os.environ.get("DLNA_RADIO_PORT", "8357"))
BITRATE = os.environ.get("DLNA_RADIO_BITRATE", "256k")
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
MAX_AGE = 1800
SERVER = "Linux UPnP/1.0 dlna-radio/1.0"
CHUNK = 64 * 1024
MAX_FAILURES = 6
CODEC_TTL = 6 * 3600
FOLDER = "stations"
FOLDER_TITLE = os.environ.get("DLNA_RADIO_FOLDER", "Stacje")
PASSTHROUGH = {c.strip() for c in os.environ.get("DLNA_RADIO_PASSTHROUGH", "mp3").lower().split(",") if c.strip()}

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
# Live stream: no seeking (OP=00), streaming transfer mode.
_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI={ci};DLNA.ORG_FLAGS=01700000000000000000000000000000"
# Codec -> (ffmpeg muxer, file extension, MIME, DLNA profile or None) when
# passed through. Anything else is transcoded to MP3.
FORMATS = {
    "mp3": ("mp3", "mp3", "audio/mpeg", "MP3"),
    "flac": ("flac", "flac", "audio/flac", None),
}
MP3 = FORMATS["mp3"]
SOURCE_PROTOCOLS = ",".join(f"http-get:*:{f[2]}:*" for f in FORMATS.values())


def local_ip() -> str:
    """Return the IP to announce."""
    if host := os.environ.get("DLNA_RADIO_HOST"):
        return host
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((SSDP_GROUP, SSDP_PORT))  # no traffic, picks the route
        return sock.getsockname()[0]


# ------------------------------------------------------------------ stations


class Stations:
    """The stations file, re-read when it changes."""

    def __init__(self) -> None:
        self.name = "Radio"
        self.stations: dict[str, dict[str, Any]] = {}
        self.update_id = 0
        self._mtime: float | None = None

    def load(self) -> None:
        try:
            mtime = CONFIG.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None or not self.update_id:
                _LOGGER.warning("%s not found, no stations", CONFIG)
                self.stations, self._mtime = {}, None
                self.update_id += 1
            return
        if mtime == self._mtime:
            return
        try:
            data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as err:
            _LOGGER.error("Can't read %s: %s", CONFIG, err)
            return
        stations: dict[str, dict[str, Any]] = {}
        for entry in data.get("stations") or []:
            if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
                _LOGGER.warning("Skipping station without name or url: %r", entry)
                continue
            name = str(entry["name"])
            sid = str(entry.get("id") or station_id(name))
            transcode = entry.get("transcode")
            stations[sid] = {
                "name": name,
                "url": str(entry["url"]),
                "logo": str(entry.get("logo") or ""),
                "transcode": transcode if isinstance(transcode, bool) else None,
                "id": str(entry["id"]) if entry.get("id") else "",
            }
        self.name = str(data.get("name") or "Radio")
        self.stations, self._mtime = stations, mtime
        self.update_id += 1
        _LOGGER.info("Loaded %d station(s): %s", len(stations), ", ".join(s["name"] for s in stations.values()))

    def save(self) -> None:
        """Write the stations back (after a change on the web page)."""
        entries = []
        for st in self.stations.values():
            entry: dict[str, Any] = {"name": st["name"], "url": st["url"]}
            for key in ("logo", "id"):
                if st.get(key):
                    entry[key] = st[key]
            if st.get("transcode") is not None:
                entry["transcode"] = st["transcode"]
            entries.append(entry)
        text = yaml.safe_dump({"name": self.name, "stations": entries}, allow_unicode=True, sort_keys=False)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(CONFIG)
        self._mtime = None
        self.load()


def station_id(name: str) -> str:
    """Return the stable object ID of a station without an explicit id."""
    return "s" + hashlib.sha1(name.encode()).hexdigest()[:10]


STATIONS = Stations()
_tasks: set[asyncio.Task] = set()


def background(coro: Any) -> None:
    """Run a coroutine in the background, keeping a reference to it."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


_codecs: dict[str, tuple[str | None, float]] = {}


async def codec_of(url: str) -> str | None:
    """Return the audio codec of a stream (cached), None if unknown."""
    cached = _codecs.get(url)
    if cached and time.monotonic() - cached[1] < CODEC_TTL:
        return cached[0]
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-rw_timeout", "15000000",
        "-select_streams", "a:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    codec = out.decode().strip().splitlines()[0] if out.strip() else None
    if codec:
        _codecs[url] = (codec, time.monotonic())
    return codec


async def output_of(station: dict[str, Any]) -> tuple[bool, tuple[str, str, str, str | None]]:
    """Return (pass through, (muxer, extension, MIME, DLNA profile))."""
    if station.get("transcode") is True:
        return False, MP3
    codec = await codec_of(station["url"])
    if station.get("transcode") is False or (codec in PASSTHROUGH and codec in FORMATS):
        return True, FORMATS.get(codec or "", MP3)
    return False, MP3


def protocol_info(fmt: tuple[str, str, str, str | None]) -> str:
    """Return the DLNA protocolInfo of a live stream in this format."""
    pn = f"DLNA.ORG_PN={fmt[3]};" if fmt[3] else ""
    return f"http-get:*:{fmt[2]}:{pn}{_FLAGS.format(ci=0)}"


# ------------------------------------------------------------------- streams


async def stream(request: web.Request) -> web.StreamResponse:
    """Serve a station as MP3, restarting ffmpeg if the source drops."""
    STATIONS.load()
    sid = request.match_info["name"].rpartition(".")[0]
    station = STATIONS.stations.get(sid)
    if not station:
        raise web.HTTPNotFound
    _LOGGER.debug("%s %s from %s: %s", request.method, request.path, request.remote, dict(request.headers))
    copy, fmt = await output_of(station)
    headers = {
        "Content-Type": fmt[2],
        "transferMode.dlna.org": "Streaming",
        "contentFeatures.dlna.org": protocol_info(fmt).split(":", 3)[3],
        "Cache-Control": "no-cache",
    }
    if request.method == "HEAD":
        return web.Response(headers=headers)
    codec = _codecs.get(station["url"], (None, 0))[0]
    _LOGGER.info(
        "%s: playing to %s (%s)",
        station["name"], request.remote, f"{codec}, as is" if copy else f"{codec} → MP3 {BITRATE}",
    )  # fmt: skip
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "10", "-rw_timeout", "20000000",
        "-i", station["url"], "-vn", "-map", "0:a:0", "-map_metadata", "-1",
        *(["-c:a", "copy"] if copy else ["-c:a", "libmp3lame", "-b:a", BITRATE]),
        # A plain frame stream: no ID3 tag, no Xing header (wrong for live).
        *(["-id3v2_version", "0", "-write_xing", "0"] if fmt[0] == "mp3" else []),
        "-f", fmt[0], "pipe:1",
    ]  # fmt: skip
    response = web.StreamResponse(headers=headers)
    await response.prepare(request)
    failures = 0
    while failures < MAX_FAILURES:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert proc.stdout and proc.stderr
        sent = 0
        try:
            while chunk := await proc.stdout.read(CHUNK):
                await response.write(chunk)
                sent += len(chunk)
                if sent > 2_000_000:
                    failures = 0
        except (ConnectionError, asyncio.CancelledError):
            _LOGGER.info("%s: %s stopped listening", station["name"], request.remote)
            return response
        finally:
            if proc.returncode is None:
                proc.kill()
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            await proc.wait()
        failures += 1
        _LOGGER.warning("%s: source ended (%s), restarting (%d)", station["name"], err[-300:] or "no error", failures)
        await asyncio.sleep(min(2 * failures, 10))
    _LOGGER.error("%s: giving up after %d restarts", station["name"], MAX_FAILURES)
    return response


# ---------------------------------------------------------------------- UPnP


class Server:
    """Device description, ContentDirectory and ConnectionManager."""

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.base = f"http://{ip}:{PORT}"
        name = STATIONS.name
        self.udn = "uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"dlna-radio:{name}"))
        self.session: aiohttp.ClientSession | None = None

    def description(self, _request: web.Request) -> web.Response:
        services = "".join(
            f"<service><serviceType>{st}</serviceType><serviceId>urn:upnp-org:serviceId:{sid}</serviceId>"
            f"<SCPDURL>/{key}.xml</SCPDURL><controlURL>/ctl/{key}</controlURL>"
            f"<eventSubURL>/evt/{key}</eventSubURL></service>"
            for st, sid, key in ((CDS, "ContentDirectory", "cds"), (CMS, "ConnectionManager", "cms"))
        )
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
            f"<deviceType>{DEVICE_TYPE}</deviceType><dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>"
            f"<friendlyName>{escape(STATIONS.name)}</friendlyName>"
            "<manufacturer>dlna-radio</manufacturer><modelName>dlna-radio</modelName>"
            "<modelNumber>1</modelNumber><modelDescription>Internet radio as a DLNA server</modelDescription>"
            f"<UDN>{self.udn}</UDN><presentationURL>{self.base}/</presentationURL>"
            f"<serviceList>{services}</serviceList></device></root>"
        )
        return web.Response(text=xml, content_type="text/xml", charset="utf-8")

    @staticmethod
    def scpd(request: web.Request) -> web.Response:
        key = request.match_info["svc"]
        if key not in SCPD:
            raise web.HTTPNotFound
        return web.Response(text=SCPD[key], content_type="text/xml", charset="utf-8")

    async def control(self, request: web.Request) -> web.Response:
        svc = request.match_info["svc"]
        action = request.headers.get("SOAPACTION", "").strip('"').rpartition("#")[2]
        args: dict[str, str] = {}
        with contextlib.suppress(ET.ParseError):
            root = ET.fromstring(await request.read())
            for el in root.iter():
                if el.tag.rpartition("}")[2] == action:
                    args = {c.tag.rpartition("}")[2]: (c.text or "") for c in el}
        STATIONS.load()
        _LOGGER.debug("%s %s %s from %s", svc, action, args, request.remote)
        service = CDS if svc == "cds" else CMS
        if svc == "cds" and action == "Browse":
            out = await self.browse(args)
        elif svc == "cds" and action == "GetSystemUpdateID":
            out = {"Id": str(STATIONS.update_id)}
        elif svc == "cds" and action == "GetSearchCapabilities":
            out = {"SearchCaps": ""}
        elif svc == "cds" and action == "GetSortCapabilities":
            out = {"SortCaps": ""}
        elif svc == "cms" and action == "GetProtocolInfo":
            out = {"Source": SOURCE_PROTOCOLS, "Sink": ""}
        elif svc == "cms" and action == "GetCurrentConnectionIDs":
            out = {"ConnectionIDs": "0"}
        elif svc == "cms" and action == "GetCurrentConnectionInfo":
            out = {
                "RcsID": "-1", "AVTransportID": "-1", "ProtocolInfo": "", "PeerConnectionManager": "",
                "PeerConnectionID": "-1", "Direction": "Output", "Status": "OK",
            }  # fmt: skip
        else:
            return soap_fault(401, "Invalid Action")
        body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in out.items())
        return soap(f'<u:{action}Response xmlns:u="{service}">{body}</u:{action}Response>')

    async def browse(self, args: dict[str, str]) -> dict[str, str]:
        object_id = args.get("ObjectID", "0")
        flag = args.get("BrowseFlag", "BrowseDirectChildren")
        start = int(args.get("StartingIndex") or 0)
        count = int(args.get("RequestedCount") or 0)
        stations = STATIONS.stations
        # Stations live in one folder under the root: MusicCast favourites
        # remember a folder and a position, and don't work for root items.
        if flag == "BrowseMetadata":
            if object_id in ("0", FOLDER):
                entries = [self._container(object_id)]
            elif object_id in stations:
                entries = [await self._item(object_id, stations[object_id])]
            else:
                entries = []
            total = len(entries)
        elif object_id == "0":
            total = 1
            entries = [self._container(FOLDER)] if start == 0 else []
        else:
            sids = list(stations) if object_id == FOLDER else []
            total = len(sids)
            page = sids[start : start + count] if count else sids[start:]
            entries = list(await asyncio.gather(*(self._item(sid, stations[sid]) for sid in page)))
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">' + "".join(entries) + "</DIDL-Lite>"
        )
        return {
            "Result": didl,
            "NumberReturned": str(len(entries)),
            "TotalMatches": str(total),
            "UpdateID": str(STATIONS.update_id),
        }

    @staticmethod
    def _container(object_id: str) -> str:
        if object_id == "0":
            parent, count, title = "-1", 1, STATIONS.name
        else:
            parent, count, title = "0", len(STATIONS.stations), FOLDER_TITLE
        return (
            f'<container id="{object_id}" parentID="{parent}" restricted="1" searchable="0" '
            f'childCount="{count}"><dc:title>{escape(title)}</dc:title>'
            "<upnp:class>object.container.storageFolder</upnp:class></container>"
        )

    async def _item(self, sid: str, station: dict[str, Any]) -> str:
        _, fmt = await output_of(station)
        art = (
            f'<upnp:albumArtURI dlna:profileID="JPEG_TN">{escape(station["logo"])}</upnp:albumArtURI>' if station["logo"] else ""
        )
        return (
            f'<item id={quoteattr(sid)} parentID="{FOLDER}" restricted="1">'
            f"<dc:title>{escape(station['name'])}</dc:title>"
            "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
            f"<upnp:album>{escape(STATIONS.name)}</upnp:album>{art}"
            f'<res protocolInfo="{protocol_info(fmt)}">{self.base}/stream/{sid}.{fmt[1]}</res></item>'
        )

    async def subscribe(self, request: web.Request) -> web.Response:
        """Accept event subscriptions and send the initial event."""
        timeout = {"TIMEOUT": f"Second-{MAX_AGE}"}
        if request.method == "UNSUBSCRIBE":
            return web.Response()
        if sid := request.headers.get("SID"):  # renewal
            return web.Response(headers={"SID": sid, **timeout, "SERVER": SERVER})
        callback = request.headers.get("CALLBACK", "").strip("<>").split("><")[0]
        sid = f"uuid:{uuid.uuid4()}"
        if callback and request.match_info["svc"] == "cds":
            background(self._notify(callback, sid))
        return web.Response(headers={"SID": sid, **timeout, "SERVER": SERVER})

    async def _notify(self, callback: str, sid: str) -> None:
        await asyncio.sleep(0.5)
        body = (
            '<?xml version="1.0"?><e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><SystemUpdateID>{STATIONS.update_id}</SystemUpdateID></e:property>"
            "<e:property><ContainerUpdateIDs></ContainerUpdateIDs></e:property></e:propertyset>"
        )
        assert self.session
        with contextlib.suppress(aiohttp.ClientError, TimeoutError):
            await self.session.request(
                "NOTIFY",
                callback,
                data=body.encode(),
                headers={"Content-Type": 'text/xml; charset="utf-8"', "NT": "upnp:event",
                         "NTS": "upnp:propchange", "SID": sid, "SEQ": "0"},
                timeout=aiohttp.ClientTimeout(total=5),
            )  # fmt: skip

    @staticmethod
    async def index(_request: web.Request) -> web.Response:
        """A page to list, add and remove stations."""
        STATIONS.load()
        rows = []
        for sid, st in STATIONS.stations.items():
            copy, fmt = await output_of(st)
            codec = _codecs.get(st["url"], (None, 0))[0] or "?"
            how = f"{codec}, bez zmian" if copy else f"{codec} → MP3 {BITRATE}"
            rows.append(
                f"<tr><td>{escape(st['name'])}</td><td><code>{escape(st['url'])}</code></td>"
                f"<td>{escape(how)}</td><td><a href='/stream/{sid}.{fmt[1]}'>odtwórz</a></td>"
                "<td><form method=post onsubmit=\"return confirm('Usunąć?')\">"
                f"<input type=hidden name=action value=delete><input type=hidden name=id value={quoteattr(sid)}>"
                "<button>usuń</button></form></td></tr>"
            )
        page = f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{escape(STATIONS.name)}</title>
<style>
body{{font:15px system-ui,sans-serif;margin:16px;max-width:1000px;color:#222;background:#fff}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
code{{word-break:break-all;font-size:12px}}input[type=text],input[type=url]{{width:100%;box-sizing:border-box;padding:6px}}
label{{display:block;margin:8px 0}}button{{padding:6px 12px}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#ddd}}td,th{{border-color:#333}}a{{color:#8ab4f8}}}}
</style>
<h1>{escape(STATIONS.name)}</h1>
<p>Serwer DLNA: stacje są widoczne na głośnikach (wejście Server). Strumienie, których
odtwarzacze nie obsługują ({escape(", ".join(sorted(PASSTHROUGH)))} idą bez zmian),
są przerabiane na MP3 {BITRATE}.</p>
<table><tr><th>Nazwa</th><th>Adres</th><th>Format</th><th></th><th></th></tr>{"".join(rows)}</table>
<h2>Dodaj strumień</h2>
<form method=post><input type=hidden name=action value=add>
<label>Nazwa <input type=text name=name required></label>
<label>Adres strumienia <input type=url name=url required></label>
<label>Logo (opcjonalnie) <input type=url name=logo></label>
<label>Transkodowanie <select name=transcode><option value="">automatycznie</option>
<option value="true">zawsze do MP3</option><option value="false">nigdy</option></select></label>
<button>Dodaj</button></form>"""
        return web.Response(text=page, content_type="text/html")

    @staticmethod
    async def edit(request: web.Request) -> web.Response:
        """Add or remove a station from the web page."""
        form = await request.post()
        STATIONS.load()
        action = form.get("action")
        if action == "add" and form.get("name") and form.get("url"):
            name, url = str(form["name"]).strip(), str(form["url"]).strip()
            transcode = {"true": True, "false": False}.get(str(form.get("transcode", "")))
            sid = station_id(name)
            STATIONS.stations[sid] = {
                "name": name, "url": url, "logo": str(form.get("logo") or "").strip(),
                "transcode": transcode, "id": "",
            }  # fmt: skip
            _LOGGER.info("Added station %s", name)
            background(codec_of(url))
        elif action == "delete" and str(form.get("id")) in STATIONS.stations:
            _LOGGER.info("Removed station %s", STATIONS.stations.pop(str(form["id"]))["name"])
        else:
            raise web.HTTPBadRequest
        STATIONS.save()
        raise web.HTTPSeeOther("/")


def soap(body: str) -> web.Response:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )
    return web.Response(text=xml, content_type="text/xml", charset="utf-8", headers={"EXT": "", "SERVER": SERVER})


def soap_fault(code: int, text: str) -> web.Response:
    response = soap(
        "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        f'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode>'
        f"<errorDescription>{text}</errorDescription></UPnPError></detail></s:Fault>"
    )
    response.set_status(500)
    return response


def _scpd(actions: dict[str, list[tuple[str, str, str]]], variables: list[tuple[str, str, bool]]) -> str:
    acts = "".join(
        f"<action><name>{name}</name><argumentList>"
        + "".join(
            f"<argument><name>{a}</name><direction>{d}</direction><relatedStateVariable>{v}</relatedStateVariable></argument>"
            for a, d, v in args
        )
        + "</argumentList></action>"
        for name, args in actions.items()
    )
    vars_ = "".join(
        f'<stateVariable sendEvents="{"yes" if ev else "no"}"><name>{n}</name><dataType>{t}</dataType></stateVariable>'
        for n, t, ev in variables
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<actionList>{acts}</actionList><serviceStateTable>{vars_}</serviceStateTable></scpd>"
    )


SCPD = {
    "cds": _scpd(
        {
            "Browse": [
                ("ObjectID", "in", "A_ARG_TYPE_ObjectID"),
                ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                ("Filter", "in", "A_ARG_TYPE_Filter"),
                ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                ("RequestedCount", "in", "A_ARG_TYPE_Count"),
                ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                ("Result", "out", "A_ARG_TYPE_Result"),
                ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                ("TotalMatches", "out", "A_ARG_TYPE_Count"),
                ("UpdateID", "out", "A_ARG_TYPE_UpdateID"),
            ],
            "GetSearchCapabilities": [("SearchCaps", "out", "SearchCapabilities")],
            "GetSortCapabilities": [("SortCaps", "out", "SortCapabilities")],
            "GetSystemUpdateID": [("Id", "out", "SystemUpdateID")],
        },
        [
            ("A_ARG_TYPE_ObjectID", "string", False),
            ("A_ARG_TYPE_BrowseFlag", "string", False),
            ("A_ARG_TYPE_Filter", "string", False),
            ("A_ARG_TYPE_Index", "ui4", False),
            ("A_ARG_TYPE_Count", "ui4", False),
            ("A_ARG_TYPE_SortCriteria", "string", False),
            ("A_ARG_TYPE_Result", "string", False),
            ("A_ARG_TYPE_UpdateID", "ui4", False),
            ("SearchCapabilities", "string", False),
            ("SortCapabilities", "string", False),
            ("SystemUpdateID", "ui4", True),
            ("ContainerUpdateIDs", "string", True),
        ],
    ),
    "cms": _scpd(
        {
            "GetProtocolInfo": [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")],
            "GetCurrentConnectionIDs": [("ConnectionIDs", "out", "CurrentConnectionIDs")],
            "GetCurrentConnectionInfo": [
                ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"),
                ("RcsID", "out", "A_ARG_TYPE_RcsID"),
                ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"),
                ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
                ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
                ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"),
                ("Direction", "out", "A_ARG_TYPE_Direction"),
                ("Status", "out", "A_ARG_TYPE_ConnectionStatus"),
            ],
        },
        [
            ("SourceProtocolInfo", "string", True),
            ("SinkProtocolInfo", "string", True),
            ("CurrentConnectionIDs", "string", True),
            ("A_ARG_TYPE_ConnectionID", "i4", False),
            ("A_ARG_TYPE_RcsID", "i4", False),
            ("A_ARG_TYPE_AVTransportID", "i4", False),
            ("A_ARG_TYPE_ProtocolInfo", "string", False),
            ("A_ARG_TYPE_ConnectionManager", "string", False),
            ("A_ARG_TYPE_Direction", "string", False),
            ("A_ARG_TYPE_ConnectionStatus", "string", False),
        ],
    ),
}


# ---------------------------------------------------------------------- SSDP


class SSDP(asyncio.DatagramProtocol):
    """Answer M-SEARCH and announce the server."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.location = f"{server.base}/description.xml"
        udn = server.udn
        self.targets = {
            "upnp:rootdevice": f"{udn}::upnp:rootdevice",
            udn: udn,
            DEVICE_TYPE: f"{udn}::{DEVICE_TYPE}",
            CDS: f"{udn}::{CDS}",
            CMS: f"{udn}::{CMS}",
        }
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not data.startswith(b"M-SEARCH"):
            return
        headers = {}
        for line in data.decode(errors="replace").split("\r\n")[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().upper()] = value.strip()
        st = headers.get("ST", "")
        if st == "ssdp:all":
            targets = list(self.targets.items())
        elif st in self.targets:
            targets = [(st, self.targets[st])]
        else:
            return
        try:
            mx = max(0.0, min(float(headers.get("MX", "1")), 3.0))
        except ValueError:
            mx = 1.0
        _LOGGER.debug("M-SEARCH %s from %s", st, addr[0])
        asyncio.get_running_loop().call_later(random.uniform(0, mx * 0.8), self._reply, targets, addr)

    def _reply(self, targets: list[tuple[str, str]], addr: tuple[str, int]) -> None:
        for st, usn in targets:
            msg = (
                "HTTP/1.1 200 OK\r\n"
                f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
                f"DATE: {formatdate(usegmt=True)}\r\n"
                "EXT:\r\n"
                f"LOCATION: {self.location}\r\n"
                f"SERVER: {SERVER}\r\n"
                f"ST: {st}\r\n"
                f"USN: {usn}\r\n\r\n"
            )
            if self.transport:
                self.transport.sendto(msg.encode(), addr)

    def notify(self, nts: str) -> None:
        if not self.transport:
            return
        for nt, usn in self.targets.items():
            msg = (
                "NOTIFY * HTTP/1.1\r\n"
                f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
                f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
                f"LOCATION: {self.location}\r\n"
                f"NT: {nt}\r\n"
                f"NTS: {nts}\r\n"
                f"SERVER: {SERVER}\r\n"
                f"USN: {usn}\r\n\r\n"
            )
            self.transport.sendto(msg.encode(), (SSDP_GROUP, SSDP_PORT))


def ssdp_socket(ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(AttributeError, OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", SSDP_PORT))
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        struct.pack("4s4s", socket.inet_aton(SSDP_GROUP), socket.inet_aton(ip)),
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.setblocking(False)
    return sock


# ---------------------------------------------------------------------- main


async def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DLNA_RADIO_DEBUG") == "1" else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    STATIONS.load()
    ip = local_ip()
    server = Server(ip)
    server.session = aiohttp.ClientSession()

    app = web.Application()
    app.router.add_get("/", server.index)
    app.router.add_get("/description.xml", server.description)
    app.router.add_get("/{svc}.xml", server.scpd)
    app.router.add_post("/ctl/{svc}", server.control)
    app.router.add_route("SUBSCRIBE", "/evt/{svc}", server.subscribe)
    app.router.add_route("UNSUBSCRIBE", "/evt/{svc}", server.subscribe)
    app.router.add_post("/", server.edit)
    app.router.add_route("GET", "/stream/{name}", stream)
    app.router.add_route("HEAD", "/stream/{name}", stream)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    loop = asyncio.get_running_loop()
    transport, ssdp = await loop.create_datagram_endpoint(lambda: SSDP(server), sock=ssdp_socket(ip))
    _LOGGER.info("%s (%s) at %s/description.xml", STATIONS.name, server.udn, server.base)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # Probe the codecs in the background, so the first play starts quickly.
    for station in list(STATIONS.stations.values()):
        background(codec_of(station["url"]))
    while not stop.is_set():
        ssdp.notify("ssdp:alive")
        await asyncio.sleep(0.3)
        ssdp.notify("ssdp:alive")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), MAX_AGE / 3)
    ssdp.notify("ssdp:byebye")
    transport.close()
    await runner.cleanup()
    await server.session.close()


if __name__ == "__main__":
    asyncio.run(main())
