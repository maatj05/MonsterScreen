"""Setup page, served by the Pi on the local network.

From a laptop or phone: link Dropbox, then pick which presentation (a
subfolder of the base folder) this screen shows. Picking one also puts a
default config.toml in it when there is none yet. The wifi networks the Pi
connects to are managed here too, as are the weekly on/off schedule of the
screen and temporary exceptions to it, and the Pi can be shut down.

Anyone on the local network can open this page, the same as anyone who can
walk up to the screen; it never shows the Dropbox token.
"""

import datetime as dt
import html
import logging
import socket
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import dropbox_sync, screen_time
from .content_config import CONFIG_FILENAME, WEEKDAYS, Schedule
from .state import StateStore
from .wifi import WifiError

log = logging.getLogger(__name__)

CSS = """
:root { --fg:#1d232b; --muted:#5b6674; --bg:#f4f5f7; --card:#fff; --line:#dde1e6;
        --accent:#0061fe; --ok:#1a7f37; --err:#c62828; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8eaed; --muted:#9aa3ad; --bg:#16191d; --card:#20242a; --line:#343a42;
          --accent:#5b9bff; --ok:#4cc26a; --err:#ff6b6b; } }
* { box-sizing: border-box; }
body { margin:0; font:16px/1.5 system-ui, sans-serif; color:var(--fg); background:var(--bg); }
main { max-width:640px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:1.5rem; margin:0 0 20px; }
h2 { font-size:1.1rem; margin:0 0 12px; } h2.sub { margin-top:20px; }
section { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:18px; margin-bottom:16px; }
p { margin:0 0 12px; } .muted { color:var(--muted); }
.ok { color:var(--ok); font-weight:600; } .err { color:var(--err); font-weight:600; }
.flash { border-left:4px solid var(--accent); }
.flash.bad { border-left-color:var(--err); }
input[type=text], input[type=password] { width:100%; padding:10px; font:inherit; color:inherit; background:var(--bg);
                   border:1px solid var(--line); border-radius:6px; margin-bottom:10px; }
button, a.button { display:inline-block; padding:10px 16px; font:inherit; font-weight:600;
          border:0; border-radius:6px; background:var(--accent); color:#fff;
          text-decoration:none; cursor:pointer; }
button.danger { background:var(--err); }
button.secondary { background:transparent; color:var(--accent); border:1px solid var(--line); }
ul.folders { list-style:none; padding:0; margin:0; }
ul.folders li { display:flex; align-items:center; gap:12px; border-top:1px solid var(--line); }
ul.folders li:first-child { border-top:0; }
ul.folders a.open { flex:1; min-width:0; display:flex; justify-content:space-between; gap:8px;
                    padding:12px 4px; color:var(--fg); text-decoration:none; overflow-wrap:anywhere; }
ul.folders a.open:hover { background:var(--bg); }
ul.folders .chev { color:var(--muted); }
ul.folders button { padding:6px 12px; }
ul.folders li > .ok { white-space:nowrap; }
ul.folders li > span:first-child { flex:1; min-width:0; padding:12px 4px; overflow-wrap:anywhere; }
.crumbs { overflow-wrap:anywhere; } .crumbs a { color:var(--accent); }
section > form { margin-bottom:12px; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:0; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
ol { padding-left:20px; margin:0 0 12px; }
input[type=time] { padding:8px; font:inherit; color:inherit; background:var(--bg);
                   border:1px solid var(--line); border-radius:6px; }
.buttons { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
.buttons form { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0; }
table.week { border-collapse:collapse; margin-bottom:12px; }
table.week td { padding:4px 8px 4px 0; white-space:nowrap; }
"""


def local_ip() -> str:
    """The address other devices on the network can reach this Pi at."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packets are sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname() + ".local"


def config_template() -> bytes:
    return resources.files(__package__).joinpath("config_template.toml").read_bytes()


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def split_relative(path: str) -> list[str]:
    """"Kantine/Zomer" -> ["Kantine", "Zomer"]; refuses anything that could climb out."""
    parts = [p for p in path.strip("/").split("/")] if path.strip("/") else []
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"Ongeldige map: {path!r}")
    return parts


def browse_link(parts: list[str]) -> str:
    # Always explicit: a bare "/" opens next to the presentation on screen instead.
    return "/?" + urllib.parse.urlencode({"map": "/".join(parts)})


class SetupApp:
    def __init__(self, store: StateStore, base_folder: str, status, default_app_key: str = "",
                 client_factory=dropbox_sync.DropboxClient,
                 exchange_code=dropbox_sync.exchange_code, wifi=None, shutdown=None,
                 config_schedule=Schedule):
        self.store = store
        self.config_schedule = config_schedule  # returns the [schedule] from config.toml
        self.now = dt.datetime.now
        self.wifi = wifi
        self.shutdown = shutdown
        self.base_folder = "/" + base_folder.strip("/")
        self.status = status
        self.default_app_key = default_app_key
        self.client_factory = client_factory
        self.exchange_code = exchange_code
        self._pending = None  # (app_key, verifier) while the user is in the Dropbox tab
        self._flash = None  # (text, good) shown once on the next page view
        self._lock = threading.Lock()

    # --- actions ------------------------------------------------------------

    def _client(self):
        state = self.store.get()
        return self.client_factory(state.app_key, state.refresh_token)

    def start_link(self, app_key: str) -> str:
        app_key = app_key.strip()
        if not app_key:
            raise ValueError("Vul de App key in.")
        verifier = dropbox_sync.new_pkce_verifier()
        with self._lock:
            self._pending = (app_key, verifier)
        return dropbox_sync.authorize_url(app_key, verifier)

    def finish_link(self, code: str) -> None:
        with self._lock:
            pending = self._pending
        if not pending:
            raise ValueError("Begin opnieuw bij stap 1.")
        if not code.strip():
            raise ValueError("Plak de code die Dropbox toont.")
        app_key, verifier = pending
        token = self.exchange_code(app_key, code, verifier)
        self.store.update(app_key=app_key, refresh_token=token)
        with self._lock:
            self._pending = None

    def _path(self, parts: list[str]) -> str:
        return "/".join([self.base_folder, *parts])

    def relative_to_base(self, folder: str) -> list[str] | None:
        """Parts of `folder` below the base folder, or None when it lies outside it."""
        if folder.lower() == self.base_folder.lower():
            return []
        prefix = self.base_folder.lower() + "/"
        if not folder.lower().startswith(prefix):
            return None
        return folder[len(prefix):].split("/")

    def choose_folder(self, relative: str) -> str:
        parts = split_relative(relative)
        if not parts:
            raise ValueError("Kies een map.")
        client = self._client()
        # Only accept a folder that really exists under the base, so the form can't point elsewhere.
        parent, name = self._path(parts[:-1]), parts[-1]
        if name not in client.list_subfolders(parent):
            raise ValueError(f"Map '{name}' bestaat niet (meer) in {parent}.")
        folder = self._path(parts)
        try:
            created = client.upload_if_missing(f"{folder}/{CONFIG_FILENAME}", config_template())
            note = (f"Er staat nu een {CONFIG_FILENAME} in die map." if created
                    else f"De bestaande {CONFIG_FILENAME} in die map wordt gebruikt.")
        except dropbox_sync.DropboxError as e:
            log.error("Could not create %s in %s: %s", CONFIG_FILENAME, folder, e)
            note = (f"Let op: {CONFIG_FILENAME} kon niet worden aangemaakt ({e}). Geef de app het "
                    "recht files.content.write en koppel opnieuw; tot dan gelden de standaardinstellingen.")
        self.store.update(folder=folder)
        return f"Het scherm toont nu '{'/'.join(parts)}'. {note}"

    def _schedule(self, state):
        return screen_time.base_schedule(state.week_schedule, self.config_schedule())

    def screen_off(self) -> str:
        now = self.now()
        until = screen_time.next_start(self._schedule(self.store.get()), now)
        self.store.update(override=screen_time.Override(False, until).to_state())
        if until is None:
            return "Het scherm is uit tot je het weer aanzet."
        return f"Het scherm is uit tot {screen_time.describe_moment(until, now)}."

    def screen_on_until(self, time: str) -> str:
        now = self.now()
        until = screen_time.next_time_of_day(now, time)
        self.store.update(override=screen_time.Override(True, until).to_state())
        return f"Het scherm blijft aan tot {screen_time.describe_moment(until, now)}."

    def screen_auto(self) -> str:
        self.store.update(override=None)
        return "Het scherm volgt weer het schema."

    def save_schedule(self, form: dict) -> str:
        days = {}
        for day, name in zip(WEEKDAYS, screen_time.DAY_NAMES):
            if not form.get(f"{day}_on"):
                days[day] = None
                continue
            try:
                days[day] = (screen_time.parse_time(form.get(f"{day}_from", "")),
                             screen_time.parse_time(form.get(f"{day}_to", "")))
            except ValueError as e:
                raise ValueError(f"{name.capitalize()}: {e}") from None
        enabled = bool(form.get("enabled"))
        self.store.update(week_schedule=screen_time.WeekSchedule(days, enabled).to_state())
        return ("Het weekschema is opgeslagen." if enabled else
                f"Het weekschema staat uit; het schema uit {CONFIG_FILENAME} geldt.")

    # --- pages --------------------------------------------------------------

    def page(self, body: str) -> str:
        return ("<!doctype html><html lang='nl'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>Scherm instellen</title><style>{CSS}</style></head>"
                f"<body><main><h1>Scherm instellen</h1>{body}</main></body></html>")

    def take_flash(self) -> str:
        with self._lock:
            flash, self._flash = self._flash, None
        if not flash:
            return ""
        text, good = flash
        return f"<section class='flash{'' if good else ' bad'}'><p>{esc(text)}</p></section>"

    def flash(self, text: str, good: bool = True) -> None:
        with self._lock:
            self._flash = (text, good)

    def index(self, browse: str | None = None) -> str:
        state = self.store.get()
        parts = [self.take_flash()]
        if state.linked:
            parts.append(
                "<section><h2>1. Dropbox</h2><p class='ok'>Gekoppeld</p>"
                "<form method='post' action='/link/start'>"
                f"<input type='hidden' name='app_key' value='{esc(state.app_key)}'>"
                "<button class='secondary'>Opnieuw koppelen</button></form></section>")
            parts.append(self.folder_section(state, browse))
            if state.folder:
                parts.append(self.screen_section(state))
                parts.append(self.schedule_section(state))
                parts.append(self.status_section(state))
        else:
            parts.append(self.link_section(state.app_key or self.default_app_key))
        if self.wifi:
            parts.append(self.wifi_section())
        if self.shutdown:
            parts.append(
                "<section><h2>Pi uitschakelen</h2><p class='muted'>Zet de hele Pi uit, bijvoorbeeld "
                "om de stekker eruit te halen. Alleen het scherm op zwart? Gebruik dan "
                "<b>Scherm uit</b>.</p><form method='post' action='/power/off' "
                "onsubmit=\"return confirm('De Pi uitschakelen? Het scherm gaat uit en de "
                "presentatie stopt.')\"><button class='danger'>Pi uitschakelen</button></form></section>")
        return self.page("".join(parts))

    def off_page(self) -> str:
        return self.page(
            "<section><h2>De Pi wordt uitgeschakeld</h2>"
            "<p>Wacht tot het groene lampje op de Pi niet meer knippert; daarna kun je de "
            "stekker eruit halen.</p>"
            "<p class='muted'>Weer aanzetten: stekker eruit en er weer in.</p></section>")

    def link_section(self, app_key: str) -> str:
        return (
            "<section><h2>1. Dropbox koppelen</h2>"
            "<p class='muted'>De App key staat op <a href='https://www.dropbox.com/developers/apps' "
            "target='_blank' rel='noopener'>dropbox.com/developers/apps</a> bij je app. Vink daar "
            "onder <b>Permissions</b> aan: files.metadata.read, files.content.read en "
            "files.content.write (dat laatste om config.toml aan te maken).</p>"
            "<form method='post' action='/link/start'>"
            f"<input type='text' name='app_key' placeholder='App key' value='{esc(app_key)}' "
            "autocomplete='off' required>"
            "<button>Verder</button></form></section>")

    def code_page(self, url: str) -> str:
        return self.page(
            "<section><h2>1. Dropbox koppelen</h2><ol>"
            f"<li><a class='button' href='{esc(url)}' target='_blank' rel='noopener'>Open Dropbox</a>"
            " en klik op <b>Toestaan</b>.</li>"
            "<li>Kopieer de code die Dropbox daarna toont en plak hem hier:</li></ol>"
            "<form method='post' action='/link/finish'>"
            "<input type='text' name='code' placeholder='Code van Dropbox' autocomplete='off' required>"
            "<button>Koppelen</button></form></section>")

    def folder_section(self, state, browse: str | None) -> str:
        current = self.relative_to_base(state.folder) if state.folder else None
        if browse is None:  # start next to the presentation that is on screen now
            parts = current[:-1] if current else []
        else:
            try:
                parts = split_relative(browse)
            except ValueError:
                parts = []

        crumbs = [f"<a href='{browse_link([])}'>{esc(self.base_folder.rsplit('/', 1)[-1])}</a>"]
        crumbs += [f"<a href='{esc(browse_link(parts[:i + 1]))}'>{esc(p)}</a>" for i, p in enumerate(parts)]
        head = ("<section><h2>2. Presentatie kiezen</h2>"
                f"<p class='crumbs'>{' › '.join(crumbs)}</p>")
        if parts:
            if current is not None and [p.lower() for p in parts] == [p.lower() for p in current]:
                head += "<p class='ok'>Deze map staat nu op het scherm</p>"
            else:
                head += self._pick_form(parts, f"Deze map ({parts[-1]}) tonen", "secondary")

        try:
            names = self._client().list_subfolders(self._path(parts))
        except Exception as e:
            return head + f"<p class='err'>Kan de mappen niet ophalen: {esc(e)}</p></section>"
        if not names:
            text = ("Geen submappen." if parts else
                    "Er staan nog geen mappen in. Maak er een aan in Dropbox en ververs deze pagina.")
            return head + f"<p class='muted'>{text}</p></section>"

        current_lower = [p.lower() for p in current] if current else None
        items = []
        for name in names:
            child = [*parts, name]
            on_screen = current_lower == [p.lower() for p in child]
            inside = (current_lower is not None and len(current_lower) > len(child)
                      and current_lower[:len(child)] == [p.lower() for p in child])
            label = esc(name) + (" <span class='muted'>(bevat de huidige)</span>" if inside else "")
            action = ("<span class='ok'>✓ op het scherm</span>" if on_screen
                      else self._pick_form(child, "Tonen"))
            items.append(f"<li><a class='open' href='{esc(browse_link(child))}'>"
                         f"<span>📁 {label}</span><span class='chev'>›</span></a>{action}</li>")
        return head + f"<ul class='folders'>{''.join(items)}</ul></section>"

    def _pick_form(self, parts: list[str], label: str, css: str = "") -> str:
        return ("<form method='post' action='/folder'>"
                f"<input type='hidden' name='path' value='{esc('/'.join(parts))}'>"
                f"<button class='{css}'>{esc(label)}</button></form>")

    def wifi_section(self) -> str:
        head = ("<section><h2>Wifi</h2><p class='muted'>De Pi maakt vanzelf verbinding met een "
                "bekend netwerk dat in de buurt is.</p>")
        try:
            networks = self.wifi.saved()
        except WifiError as e:
            return head + f"<p class='err'>Kan de wifi-netwerken niet ophalen: {esc(e)}</p></section>"
        items = []
        for n in networks:
            if n.active:
                action = "<span class='ok'>✓ verbonden</span>"
            elif n.ssid == self.wifi.default_ssid:
                action = "<span class='muted'>standaard</span>"
            else:
                action = ("<form method='post' action='/wifi/remove'>"
                          f"<input type='hidden' name='name' value='{esc(n.name)}'>"
                          "<button class='secondary'>Verwijderen</button></form>")
            items.append(f"<li><span>📶 {esc(n.ssid)}</span>{action}</li>")
        known = (f"<ul class='folders'>{''.join(items)}</ul>" if items
                 else "<p class='muted'>Nog geen wifi-netwerken bekend.</p>")
        options = "".join(f"<option value='{esc(name)}'>" for name in self.wifi.visible())
        return (head + known +
                "<h2 class='sub'>Netwerk toevoegen</h2>"
                "<form method='post' action='/wifi/add'>"
                "<input type='text' name='ssid' list='ssids' placeholder='Naam van het netwerk' "
                "autocomplete='off' required>"
                f"<datalist id='ssids'>{options}</datalist>"
                "<input type='password' name='password' placeholder='Wachtwoord (leeg bij een open netwerk)' "
                "autocomplete='new-password'>"
                "<button>Opslaan</button></form></section>")

    def screen_section(self, state) -> str:
        now = self.now()
        schedule = self._schedule(state)
        override = screen_time.Override.from_state(state.override)
        on = screen_time.is_on(schedule, override, now)
        text = esc(screen_time.describe(schedule, override, now))
        buttons = []
        if on:
            buttons.append("<form method='post' action='/screen/off'>"
                           "<button>Scherm uit</button></form>")
        buttons.append("<form method='post' action='/screen/on-until'>"
                       "<button class='secondary'>Aan houden tot</button>"
                       "<input type='time' name='until' value='03:00' required></form>")
        if override and override.active(now):
            buttons.append("<form method='post' action='/screen/auto'>"
                           "<button class='secondary'>Volgens schema</button></form>")
        return ("<section><h2>Scherm</h2>"
                + (f"<p class='ok'>{text}</p>" if on else f"<p><b>{text}</b></p>")
                + f"<div class='buttons'>{''.join(buttons)}</div>"
                "<p class='muted'><b>Scherm uit</b> maakt het scherm zwart tot het schema het "
                "weer aanzet; de Pi blijft draaien.</p></section>")

    def schedule_section(self, state) -> str:
        week = screen_time.WeekSchedule.from_state(state.week_schedule)
        rows = []
        for day, name in zip(WEEKDAYS, screen_time.DAY_NAMES):
            period = week.days.get(day) if week else (8 * 60, 18 * 60)
            start, end = period or (8 * 60, 18 * 60)
            rows.append(
                f"<tr><td><label><input type='checkbox' name='{day}_on'{' checked' if period else ''}>"
                f" {name.capitalize()}</label></td>"
                f"<td><input type='time' name='{day}_from' value='{screen_time.format_time(start)}'>"
                f" – <input type='time' name='{day}_to' value='{screen_time.format_time(end)}'></td></tr>")
        enabled = " checked" if week and week.enabled else ""
        config_note = ("" if self.config_schedule().enabled
                       else " (daar staat het nu uit: het scherm is dan altijd aan)")
        return ("<section><h2>Weekschema</h2><form method='post' action='/schedule'>"
                f"<p><label><input type='checkbox' name='enabled'{enabled}> "
                "Weekschema gebruiken</label></p>"
                f"<table class='week'>{''.join(rows)}</table>"
                "<p class='muted'>Een dag zonder vinkje is het scherm de hele dag uit. Een eindtijd "
                "vóór de begintijd loopt door na middernacht (bv. 18:00 – 02:00); 00:00 – 00:00 is "
                "de hele dag aan.</p><button>Opslaan</button></form>"
                f"<p class='muted'>Zonder weekschema geldt het schema uit {CONFIG_FILENAME} in "
                f"Dropbox{config_note}.</p></section>")

    def status_section(self, state) -> str:
        s = self.status
        last = s.last_sync.strftime("%d-%m-%Y %H:%M") if s.last_sync else "nog niet"
        rows = [("Map", state.folder), ("Bestanden", s.file_count), ("Laatst bijgewerkt", last)]
        if s.converting:
            rows.append(("Bezig met omzetten", s.converting))
        error = f"<p class='err'>Laatste fout: {esc(s.last_error)}</p>" if s.last_error else ""
        dl = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
        return f"<section><h2>Status</h2>{error}<dl>{dl}</dl></section>"


def make_handler(app: SetupApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _send(self, body: str, status=HTTPStatus.OK):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict:
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
            return {k: v[0] for k, v in fields.items()}

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/":
                return self._send(app.page("<p>Niet gevonden. <a href='/'>Terug</a></p>"),
                                  HTTPStatus.NOT_FOUND)
            browse = urllib.parse.parse_qs(url.query, keep_blank_values=True).get("map", [None])[0]
            self._send(app.index(browse))

        def do_POST(self):
            form = self._form()
            try:
                if self.path == "/link/start":
                    return self._send(app.code_page(app.start_link(form.get("app_key", ""))))
                if self.path == "/link/finish":
                    app.finish_link(form.get("code", ""))
                    app.flash("Dropbox is gekoppeld. Kies nu welke presentatie dit scherm toont.")
                elif self.path == "/folder":
                    app.flash(app.choose_folder(form.get("path", "")))
                elif self.path == "/wifi/add" and app.wifi:
                    ssid = form.get("ssid", "").strip()
                    app.wifi.add(ssid, form.get("password", ""))
                    app.flash(f"Wifi-netwerk '{ssid}' is opgeslagen. De Pi gebruikt het zodra het "
                              "in de buurt is.")
                elif self.path == "/screen/off":
                    app.flash(app.screen_off())
                elif self.path == "/screen/on-until":
                    app.flash(app.screen_on_until(form.get("until", "")))
                elif self.path == "/screen/auto":
                    app.flash(app.screen_auto())
                elif self.path == "/schedule":
                    app.flash(app.save_schedule(form))
                elif self.path == "/power/off" and app.shutdown:
                    app.shutdown()
                    return self._send(app.off_page())
                elif self.path == "/wifi/remove" and app.wifi:
                    app.wifi.remove(form.get("name", ""))
                    app.flash("Het wifi-netwerk is verwijderd.")
                else:
                    return self._send(app.page("<p>Niet gevonden.</p>"), HTTPStatus.NOT_FOUND)
            except (ValueError, dropbox_sync.DropboxError, WifiError, OSError) as e:
                log.warning("Setup step failed: %s", e)
                app.flash(f"Dat lukte niet: {e}", good=False)
            self._redirect()

    return Handler


def serve(app: SetupApp, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("", port), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True, name="setup-web").start()
    log.info("Setup page on http://%s:%d", local_ip(), port)
    return server
