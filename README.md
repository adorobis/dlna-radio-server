# dlna-radio-server

[![CI][ci-badge]][ci-url]

A tiny DLNA/UPnP media server that lists internet radio streams, so network
speakers can browse them on their **Server** input, play them and **save them
as favourites / presets**. It was written for Yamaha MusicCast, but works with
any DLNA renderer that browses media servers.

[Po polsku ↓](#po-polsku)

## Why

- MusicCast can play a URL pushed to it over DLNA (by Home Assistant,
  BubbleUPnP, …), but it **can't save pushed content as a favourite**. It
  can save items it found on a media server itself, so this server turns any
  stream URL into such an item.
- Many stations stream **HE-AAC** (raw ADTS), which MusicCast can't decode.
  The server detects the codec and **transcodes it to MP3** with ffmpeg. MP3
  streams are passed through untouched.

## Features

- Stations from a YAML file or added on the server's web page; changes apply
  without a restart.
- Codec detection with ffprobe: passed through when the speakers play it
  (`mp3` by default), otherwise transcoded to MP3 (256 kbps by default).
  Either can be forced per station.
- Reconnects when the source drops, for as long as a speaker listens. ffmpeg
  only runs while someone is listening.
- A status page, `/status.json`, with what each speaker is playing: station,
  stream URL, the source format (codec, profile, bitrate, sample rate,
  channels) and whether it is transcoded. Handy for Home Assistant sensors.
- SSDP discovery and announcements, ContentDirectory and ConnectionManager.
  Stable IDs, so favourites keep working after restarts.
- Small: Python, aiohttp and ffmpeg on Alpine, about 30 MB of RAM. Images for
  `linux/amd64` and `linux/arm64`.

## Installation

The server has to be on the same network segment as the speakers (SSDP uses
multicast). Run the container with **host networking**, or give it its own LAN
address with a **macvlan** network. Bridge networking with published ports
does not work.

```yaml
services:
  dlna-radio:
    image: ghcr.io/adorobis/dlna-radio-server:latest
    container_name: dlna-radio
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./config:/config
```

See [`compose.example.yaml`](compose.example.yaml) for a macvlan example.
Then open `http://<server-ip>:8357/` to add stations, or create
`config/stations.yaml` (see below).

On the speaker, pick the **Server** input (in the MusicCast app: *Server*),
then **Radio → Stacje** and a station. While it plays, add it to favourites in
the app or with the remote.

## Configuration

### Stations: `/config/stations.yaml`

```yaml
name: Radio                 # server name shown on the speakers
stations:
  - name: Radio 357
    url: https://stream.radio357.pl/?s=app
    logo: https://...       # optional, cover shown on the speaker
    id: r357                # optional, see below
    transcode: true         # optional: true = always MP3, false = never
```

- The file is re-read when it changes. The web page edits the same file.
- `id` is the object ID the speakers store in favourites. Without it, the ID is
  derived from the name, so renaming a station breaks its favourite. Set an
  `id` if you want to rename stations later.
- [`stations.example.yaml`](stations.example.yaml) has a few working examples.

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `DLNA_RADIO_CONFIG` | `/config/stations.yaml` | Stations file |
| `DLNA_RADIO_PORT` | `8357` | HTTP port |
| `DLNA_RADIO_HOST` | outgoing IP | IP announced to the speakers |
| `DLNA_RADIO_BITRATE` | `256k` | MP3 bitrate when transcoding |
| `DLNA_RADIO_PASSTHROUGH` | `mp3` | Codecs served as they are (`mp3`, `flac`) |
| `DLNA_RADIO_FOLDER` | `Stacje` | Name of the folder with the stations |
| `DLNA_RADIO_DEBUG` | | `1` logs every request |

## Status for Home Assistant

`http://<server-ip>:8357/status.json` lists the speakers listening right now,
keyed by IP:

```json
{"server": "Radio", "players": {"192.168.1.20": {
  "station": "Radio 357 (AAC)", "id": "r357-aac", "url": "https://stream.radio357.pl/?s=web",
  "source": {"codec": "aac", "profile": "HE-AAC", "bitrate": 128, "sample_rate": 44100,
             "channels": 2, "channel_layout": "stereo", "icy_name": "Radio 357",
             "icy_genre": null, "summary": "HE-AAC 128 kb/s 44.1 kHz stereo"},
  "transcoded": true, "output": {"codec": "mp3", "mime": "audio/mpeg", "bitrate": 256},
  "since": "2026-11-14T13:47:08+0000"}}}
```

`bitrate` is in kbit/s: the station's `icy-br` header, or ffprobe's estimate.
A REST sensor in Home Assistant:

```yaml
rest:
  - resource: http://192.168.1.10:8357/status.json
    scan_interval: 15
    sensor:
      - name: DLNA Radio
        value_template: "{{ value_json.players | count }}"
        json_attributes: [players]
```

Then e.g. `state_attr('sensor.dlna_radio', 'players')['192.168.1.20'].source.summary`
in a template sensor per speaker. The status includes the stream URLs, so
personal links are visible to anyone who can reach the server.

## Notes and limitations

- **Now playing.** MusicCast reads an item's title, artist and cover once, when
  playback starts, and ignores ICY metadata from a media server. The speaker
  shows the station name, not the current song.
- **Favourites need a folder.** MusicCast favourites of items at the server's
  root fail with "favorites not available", so the stations are in one folder.
- **The web page has no authentication.** Anyone on your network can see the
  stream URLs and change the list. Don't expose the port to the internet.
- **Personal stream links** (e.g. patron streams with a token) only live in
  your `stations.yaml`; keep that file private.

## Troubleshooting

- The server is not listed on the speaker: check that the container has its own
  LAN IP or host networking, and that UDP port 1900 is not blocked.
- Set `DLNA_RADIO_DEBUG=1` and watch `docker logs` while browsing and playing.
- `http://<server-ip>:8357/stream/<id>.mp3` plays a station in any player.

---

## Po polsku

Mały serwer DLNA, który udostępnia strumienie radiowe głośnikom sieciowym,
przede wszystkim Yamaha MusicCast. Stacje widać na wejściu **Server** i można je
**zapisać w ulubionych**. Tego nie da się zrobić ze strumieniem wysłanym na
głośnik z Home Assistanta.

- Strumienie w formacie, którego MusicCast nie obsługuje (np. HE-AAC), są
  przerabiane w locie na MP3 256 kb/s. Strumienie MP3 idą bez zmian.
- Stacje dodaje się na stronie `http://<ip-serwera>:8357/` albo w pliku
  `config/stations.yaml`. Zmiany działają bez restartu.
- Kontener musi mieć własny adres w sieci LAN (macvlan) albo działać w trybie
  `network_mode: host`.
- Na głośniku: wejście **Server** → **Radio** → **Stacje** → stacja, a w trakcie
  grania zapis w ulubionych.
- `http://<ip-serwera>:8357/status.json` pokazuje, co gra który głośnik: stację,
  adres strumienia, format źródła (kodek, bitrate, częstotliwość, kanały) i czy
  jest transkodowany. Przydaje się do sensorów w Home Assistant.
- Głośnik pokazuje nazwę stacji, a nie aktualny utwór. MusicCast czyta opis raz,
  przy starcie, i ignoruje metadane ICY.
- Strona nie ma hasła, więc nie wystawiaj jej do internetu. Osobiste linki
  (np. dla patronów) trzymaj tylko w swoim `stations.yaml`.

## License

[MIT](LICENSE)

[ci-badge]: https://github.com/adorobis/dlna-radio-server/actions/workflows/ci.yml/badge.svg
[ci-url]: https://github.com/adorobis/dlna-radio-server/actions/workflows/ci.yml
