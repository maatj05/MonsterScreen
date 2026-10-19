import datetime as dt
import hashlib
import os
import random
import shutil
import subprocess
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from monsterscreen import netinfo, playlist, settings as settings_mod, web
from monsterscreen.content_config import ConfigError, ContentConfigLoader, parse, parse_ranges
from monsterscreen.dropbox_sync import DropboxError, FolderSync, content_hash
from monsterscreen.state import State, StateStore, SyncStatus
from monsterscreen.transcode import Transcoder
from monsterscreen.wifi import Wifi, WifiError
from monsterscreen.screen_time import (Override, WeekSchedule, base_schedule, describe, is_on,
                                       next_start, next_time_of_day)

EXAMPLE = Path(__file__).resolve().parent.parent / "voorbeeld" / "config.toml"


class ConfigTest(unittest.TestCase):
    def test_example_config_parses(self):
        cfg = parse(tomllib.loads(EXAMPLE.read_text()))
        self.assertEqual(cfg.rule_for("Openingstijden.JPG").duration, 20)
        self.assertTrue(cfg.rule_for("oud-logo.png").skip)
        self.assertEqual(cfg.schedule.days["sunday"], [])
        self.assertEqual(cfg.schedule.dates[dt.date(2026, 12, 31)], [(540, 960)])

    def test_defaults(self):
        cfg = parse({})
        self.assertEqual(cfg.image_duration, 10)
        self.assertTrue(cfg.schedule.is_on(dt.datetime(2026, 1, 1, 3, 0)))

    def test_invalid_values(self):
        for data in ({"display": {"order": "size"}}, {"display": {"image_duration": 0}},
                     {"display": {"rotate": 45}}, {"schedule": {"mondag": "on"}},
                     {"files": {"a.jpg": {"from": "morgen"}}}):
            with self.assertRaises(ConfigError, msg=data):
                parse(data)

    def test_ranges(self):
        self.assertEqual(parse_ranges("07:30-12:00, 13:00-24:00"), [(450, 720), (780, 1440)])
        self.assertEqual(parse_ranges("off"), [])
        self.assertEqual(parse_ranges("aan"), [(0, 1440)])
        for bad in ("18:00-07:00", "7-8", "10:75-11:00"):
            with self.assertRaises(ConfigError):
                parse_ranges(bad)

    def test_schedule(self):
        cfg = parse({"schedule": {"enabled": True, "monday": "08:00-18:00", "sunday": "off",
                                  "dates": {"2026-09-28": "off"}}})
        monday = dt.datetime(2026, 9, 21, 8, 0)
        self.assertTrue(cfg.schedule.is_on(monday))
        self.assertFalse(cfg.schedule.is_on(monday.replace(hour=7, minute=59)))
        self.assertFalse(cfg.schedule.is_on(monday.replace(hour=18)))
        self.assertFalse(cfg.schedule.is_on(dt.datetime(2026, 9, 27, 12)))  # sunday
        self.assertTrue(cfg.schedule.is_on(dt.datetime(2026, 9, 22, 3)))  # tuesday not listed
        self.assertFalse(cfg.schedule.is_on(dt.datetime(2026, 9, 28, 12)))  # date override

    def test_loader_keeps_last_good_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            loader = ContentConfigLoader(path)
            path.write_text("[display]\nimage_duration = 7\n")
            self.assertEqual(loader.load().image_duration, 7)
            path.write_text("[display\nkapot")
            os.utime(path, (1, 1))
            self.assertEqual(loader.load().image_duration, 7)
            path.unlink()
            self.assertEqual(loader.load().image_duration, 10)


class PlaylistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for i, name in enumerate(["b.jpg", "A.png", "c.mp4", "notes.txt", "config.toml", ".x.part"]):
            (self.dir / name).write_bytes(b"x")
            os.utime(self.dir / name, (i, i))

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, cfg, today=dt.date(2026, 1, 1)):
        return [s.path.name for s in playlist.build(self.dir, cfg, today, random.Random(1))]

    def test_name_order_and_filtering(self):
        self.assertEqual(self.names(parse({})), ["A.png", "b.jpg", "c.mp4"])

    def test_newest_first(self):
        self.assertEqual(self.names(parse({"display": {"order": "newest"}})), ["c.mp4", "A.png", "b.jpg"])

    def test_durations_and_rules(self):
        cfg = parse({"display": {"image_duration": 8, "max_video_duration": 30},
                     "files": {"b.jpg": {"duration": 3}, "a.png": {"skip": True},
                               "c.mp4": {"from": dt.date(2026, 2, 1)}}})
        slides = playlist.build(self.dir, cfg, dt.date(2026, 1, 1))
        self.assertEqual([(s.path.name, s.kind, s.duration) for s in slides], [("b.jpg", "image", 3)])
        slides = playlist.build(self.dir, cfg, dt.date(2026, 2, 1))
        self.assertEqual(slides[-1].duration, 30)

    def test_media_kinds(self):
        self.assertEqual(playlist.media_kind(Path("Film.WMV")), "video")
        self.assertEqual(playlist.media_kind(Path("clip.mpeg")), "video")
        self.assertEqual(playlist.media_kind(Path("foto.JPG")), "image")
        self.assertIsNone(playlist.media_kind(Path("notes.txt")))

    def test_interleave(self):
        slides = [playlist.Slide(Path(str(i)), "image", 1) for i in range(5)]
        news = [playlist.Slide(Path("n1"), "image", 1), playlist.Slide(Path("n2"), "image", 1)]
        result = [s.path.name for s in playlist.interleave(slides, news, 2)]
        self.assertEqual(result, ["0", "1", "n1", "2", "3", "n2", "4"])


class FakeClient:
    def __init__(self, files):
        self.files = files  # name -> bytes
        self.downloads = []

    def list_folder(self, path):
        return [{".tag": "file", "name": n, "path_lower": f"{path}/{n}".lower(), "size": len(b),
                 "content_hash": self._hash(b), "server_modified": "2026-01-02T03:04:05Z"}
                for n, b in self.files.items()]

    @staticmethod
    def _hash(data):
        return hashlib.sha256(hashlib.sha256(data).digest()).hexdigest() if data else hashlib.sha256().hexdigest()

    def download(self, path, dest):
        self.downloads.append(path)
        name = path.rsplit("/", 1)[1]
        dest.write_bytes(next(b for n, b in self.files.items() if n.lower() == name))


class SyncTest(unittest.TestCase):
    def test_sync_downloads_only_changes_and_removes_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "media"
            client = FakeClient({"a.jpg": b"one", "B.mp4": b"two"})
            sync = FolderSync(client, "/Scherm/", local)
            self.assertTrue(sync.sync())
            self.assertEqual(sorted(client.downloads), ["/scherm/a.jpg", "/scherm/b.mp4"])
            self.assertEqual((local / "B.mp4").read_bytes(), b"two")
            self.assertEqual(content_hash(local / "a.jpg"), client._hash(b"one"))

            client.downloads.clear()
            self.assertFalse(sync.sync())
            self.assertEqual(client.downloads, [])

            (local / ".manifest.json").unlink()  # lost manifest: hash instead of re-download
            self.assertFalse(sync.sync())
            self.assertEqual(client.downloads, [])

            client.files = {"a.jpg": b"ONE"}
            self.assertTrue(sync.sync())
            self.assertEqual(client.downloads, ["/scherm/a.jpg"])
            self.assertEqual((local / "a.jpg").read_bytes(), b"ONE")
            self.assertFalse((local / "B.mp4").exists())

    def test_switching_folder_clears_old_files_first(self):
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "media"
            FolderSync(FakeClient({"old.jpg": b"1", "same.jpg": b"s"}), "/Present-it/A", local).sync()
            client = FakeClient({"new.jpg": b"2", "same.jpg": b"s"})
            seen_during_download = []
            original = client.download
            client.download = lambda p, dest: (seen_during_download.append(
                sorted(f.name for f in local.iterdir() if not f.name.startswith("."))), original(p, dest))
            FolderSync(client, "/Present-it/B", local).sync()
            self.assertEqual(seen_during_download[0], [])  # old.jpg gone before downloading
            self.assertEqual(sorted(f.name for f in local.iterdir() if not f.name.startswith(".")),
                             ["new.jpg", "same.jpg"])
            client.downloads.clear()
            FolderSync(client, "/present-it/b", local).sync()  # same folder, other case
            self.assertEqual(client.downloads, [])

    def test_root_folder(self):
        self.assertEqual(FolderSync(None, "/", Path("x")).remote_folder, "")
        self.assertEqual(FolderSync(None, "", Path("x")).remote_folder, "")


class StateTest(unittest.TestCase):
    def test_persists_privately_and_notifies(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "state.json"
            store = StateStore(path, State(app_key="default"))
            self.assertFalse(store.get().linked)
            seen = []
            store.on_change(seen.append)
            store.update(refresh_token="tok", folder="/x/y")
            self.assertEqual(seen[-1].folder, "/x/y")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reloaded = StateStore(path, State(app_key="other", folder="/ignored"))
            self.assertEqual(reloaded.get(), State("default", "tok", "/x/y"))
            self.assertTrue(reloaded.get().configured)

    def test_settings_file_is_optional(self):
        s = settings_mod.load(Path("/nonexistent/settings.toml"))
        self.assertEqual(s.base_folder, "/Mediakranten/Present-it")
        self.assertEqual(s.web_port, 8080)


class FakeDropbox:
    """Stands in for DropboxClient on the setup page."""

    BASE = "/Mediakranten/Present-it"

    def __init__(self, folders=("Receptie", "Kantine"), existing=(), can_write=True, tree=None):
        # tree: relative path ("" = base) -> subfolder names
        self.tree = tree if tree is not None else {"": list(folders)}
        self.existing = set(existing)
        self.can_write = can_write
        self.uploads = []

    def __call__(self, app_key, token):  # used as client_factory
        self.last_credentials = (app_key, token)
        return self

    @property
    def folders(self):
        return self.tree[""]

    @folders.setter
    def folders(self, names):
        self.tree[""] = list(names)

    def list_subfolders(self, path):
        assert path == self.BASE or path.startswith(self.BASE + "/"), path
        relative = path[len(self.BASE) + 1:]
        if relative not in self.tree and relative:
            parent, _, name = relative.rpartition("/")
            if name in self.tree.get(parent, []):
                return []  # exists, no subfolders
            raise DropboxError("Dropbox 409: path/not_found/")
        return sorted(self.tree[relative], key=str.lower)

    def upload_if_missing(self, path, content):
        if not self.can_write:
            raise DropboxError("Dropbox 401: missing_scope/files.content.write")
        self.uploads.append((path, content))
        if path in self.existing:
            return False
        self.existing.add(path)
        return True


class SetupWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.json")
        self.dropbox = FakeDropbox()
        self.exchanged = []

        def exchange(app_key, code, verifier):
            self.exchanged.append((app_key, code))
            if code.strip() != "goede-code":
                raise DropboxError("Dropbox 400: invalid_grant")
            return "refresh-123"

        self.app = web.SetupApp(self.store, "Mediakranten/Present-it/", SyncStatus(), "KEY",
                                client_factory=self.dropbox, exchange_code=exchange)
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self):
        with urllib.request.urlopen(self.base + "/") as r:
            return r.read().decode()

    def post(self, url, **fields):
        data = urllib.parse.urlencode(fields).encode()
        with urllib.request.urlopen(self.base + url, data=data) as r:  # follows the 303 to /
            return r.read().decode()

    def test_full_setup_flow(self):
        page = self.get()
        self.assertIn("Dropbox koppelen", page)
        self.assertIn("value='KEY'", page)  # app key from settings.toml prefilled

        page = self.post("/link/start", app_key="KEY")
        self.assertIn("https://www.dropbox.com/oauth2/authorize?", page)
        self.assertIn("code_challenge_method=S256", page)

        page = self.post("/link/finish", code="verkeerd")
        self.assertIn("Dat lukte niet", page)
        self.assertFalse(self.store.get().linked)

        page = self.post("/link/finish", code=" goede-code ")
        self.assertIn("Dropbox is gekoppeld", page)
        self.assertEqual(self.store.get(), State("KEY", "refresh-123", ""))
        self.assertNotIn("refresh-123", page)  # token never shown
        self.assertIn("Receptie", page)
        self.assertIn("Kantine", page)

        page = self.post("/folder", path="Receptie")
        self.assertEqual(self.store.get().folder, "/Mediakranten/Present-it/Receptie")
        self.assertEqual(self.dropbox.uploads[0][0], "/Mediakranten/Present-it/Receptie/config.toml")
        tomllib.loads(self.dropbox.uploads[0][1].decode())  # the template is valid TOML
        self.assertIn("Er staat nu een config.toml", page)
        self.assertIn("✓ op het scherm", page)
        self.assertIn("Status", page)

        self.dropbox.existing.add("/Mediakranten/Present-it/Kantine/config.toml")
        page = self.post("/folder", path="Kantine")
        self.assertIn("bestaande config.toml", page)
        self.assertEqual(self.store.get().folder, "/Mediakranten/Present-it/Kantine")

    def test_template_config_parses_with_schedule_off(self):
        cfg = parse(tomllib.loads(web.config_template().decode()))
        self.assertFalse(cfg.schedule.enabled)
        self.assertEqual(cfg.files, {})

    def test_rejects_unknown_folder(self):
        self.store.update(app_key="KEY", refresh_token="t")
        page = self.post("/folder", path="../../Prive")
        self.assertIn("Ongeldige map", page)
        page = self.post("/folder", path="Bestaatniet")
        self.assertIn("bestaat niet", page)
        self.assertEqual(self.store.get().folder, "")
        self.assertEqual(self.dropbox.uploads, [])

    def test_folder_still_chosen_without_write_permission(self):
        self.store.update(app_key="KEY", refresh_token="t")
        self.dropbox.can_write = False
        page = self.post("/folder", path="Kantine")
        self.assertEqual(self.store.get().folder, "/Mediakranten/Present-it/Kantine")
        self.assertIn("files.content.write", page)

    def test_escapes_folder_names(self):
        self.store.update(app_key="KEY", refresh_token="t")
        self.dropbox.folders = ["<script>x</script>"]
        page = self.get()
        self.assertNotIn("<script>x", page)
        self.assertIn("&lt;script&gt;", page)

    def get_path(self, query):
        with urllib.request.urlopen(self.base + "/?" + urllib.parse.urlencode(query)) as r:
            return r.read().decode()

    def test_browse_and_choose_nested_folder(self):
        self.store.update(app_key="KEY", refresh_token="t")
        self.dropbox.tree = {"": ["Kantine", "Receptie"], "Kantine": ["Zomer 2026", "Winter"],
                             "Kantine/Zomer 2026": []}
        page = self.get()
        self.assertIn("href='/?map=Kantine'", page)
        page = self.get_path({"map": "Kantine"})
        self.assertIn("Zomer 2026", page)
        self.assertIn("Deze map (Kantine) tonen", page)
        page = self.get_path({"map": "Kantine/Zomer 2026"})
        self.assertIn("Geen submappen", page)

        page = self.post("/folder", path="Kantine/Zomer 2026")
        self.assertEqual(self.store.get().folder, "/Mediakranten/Present-it/Kantine/Zomer 2026")
        self.assertEqual(self.dropbox.uploads[-1][0],
                         "/Mediakranten/Present-it/Kantine/Zomer 2026/config.toml")
        self.assertIn("toont nu &#x27;Kantine/Zomer 2026&#x27;", page)
        # After choosing, the page opens next to the chosen folder and marks it.
        self.assertIn("Winter", page)
        self.assertIn("✓ op het scherm", page)
        self.assertIn("href='/?map='", page)  # breadcrumb back to the top level
        page = self.get_path({"map": ""})
        self.assertIn("(bevat de huidige)", page)  # Kantine, seen from the top

        page = self.post("/folder", path="Kantine")  # a parent can be chosen too
        self.assertEqual(self.store.get().folder, "/Mediakranten/Present-it/Kantine")

    def test_browse_bad_path_falls_back(self):
        self.store.update(app_key="KEY", refresh_token="t")
        page = self.get_path({"map": "../x"})
        self.assertIn("Receptie", page)
        page = self.get_path({"map": "Bestaatniet"})
        self.assertIn("Kan de mappen niet ophalen", page)

    def test_default_app_key(self):
        self.assertEqual(settings_mod.Settings().app_key, "q34alcs3we24vx7")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.toml"
            path.write_text('[dropbox]\napp_key = ""\n')
            self.assertEqual(settings_mod.load(path).app_key, "q34alcs3we24vx7")
            path.write_text('[dropbox]\napp_key = "eigen"\n')
            self.assertEqual(settings_mod.load(path).app_key, "eigen")

    def test_finish_without_start(self):
        page = self.post("/link/finish", code="goede-code")
        self.assertIn("Begin opnieuw", page)

    def test_unknown_path(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.base + "/nope")
        self.assertEqual(e.exception.code, 404)

    def test_wifi_networks(self):
        nm = FakeNmcli({"netplan-wlan0-Kantoor": {"ssid": "Kantoor", "active": True},
                        "Oud": {"ssid": "Oud"}}, visible=["Gasten", "Kantoor"])
        self.app.wifi = Wifi("Mwg mobiel", "Welkom01", run=nm)
        self.app.wifi.ensure_default()
        page = self.get()
        self.assertIn("✓ verbonden", page)  # Kantoor, without a remove button
        self.assertIn("standaard", page)  # Mwg mobiel
        self.assertEqual(page.count("Verwijderen"), 1)  # only Oud
        self.assertIn("<option value='Gasten'>", page)

        page = self.post("/wifi/add", ssid=" Gasten ", password="geheim123")
        self.assertIn("Wifi-netwerk &#x27;Gasten&#x27; is opgeslagen", page)
        self.assertEqual(nm.connections["Gasten"]["psk"], "geheim123")
        page = self.post("/wifi/add", ssid="Kort", password="kort")
        self.assertIn("8 tot 63 tekens", page)
        self.assertNotIn("Kort", nm.connections)

        page = self.post("/wifi/remove", name="Oud")
        self.assertIn("verwijderd", page)
        self.assertNotIn("Oud", nm.connections)
        page = self.post("/wifi/remove", name="netplan-wlan0-Kantoor")
        self.assertIn("in gebruik", page)

    def test_screen_off_and_on_until(self):
        self.store.update(app_key="KEY", refresh_token="t", folder=FakeDropbox.BASE + "/Kantine")
        self.app.now = lambda: dt.datetime(2026, 10, 23, 14, 0)  # Friday afternoon
        page = self.get()
        self.assertIn("Aan, volgens schema", page)  # no schedule anywhere: always on
        self.assertIn("action='/screen/off'", page)
        self.assertNotIn("Volgens schema</button>", page)

        page = self.post("/screen/off")
        self.assertIn("uit tot je het weer aanzet", page)  # no schedule to switch it back on
        self.assertEqual(self.store.get().override, {"on": False, "until": None})
        self.assertNotIn("action='/screen/off'", page)
        self.assertIn("Volgens schema</button>", page)

        page = self.post("/screen/on-until", until="03:00")
        self.assertIn("blijft aan tot morgen 03:00", page)
        self.assertEqual(self.store.get().override, {"on": True, "until": "2026-10-24T03:00:00"})
        page = self.post("/screen/on-until", until="25:00")
        self.assertIn("Ongeldige tijd", page)

        page = self.post("/screen/auto")
        self.assertIn("volgt weer het schema", page)
        self.assertIsNone(self.store.get().override)

    def test_week_schedule(self):
        self.store.update(app_key="KEY", refresh_token="t", folder=FakeDropbox.BASE + "/Kantine")
        self.app.now = lambda: dt.datetime(2026, 10, 23, 14, 0)  # Friday afternoon
        page = self.get()
        self.assertIn("altijd aan", page)  # config.toml has no schedule either
        self.assertIn("name='monday_from' value='08:00'", page)

        fields = {"enabled": "on"}
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            fields.update({f"{day}_on": "on", f"{day}_from": "08:00", f"{day}_to": "18:00"})
        page = self.post("/schedule", **fields)
        self.assertIn("weekschema is opgeslagen", page)
        self.assertEqual(self.store.get().week_schedule["friday"], "08:00-18:00")
        self.assertEqual(self.store.get().week_schedule["saturday"], "off")
        self.assertIn("Aan, volgens schema tot 18:00", page)

        page = self.post("/screen/off")  # off until the schedule starts again: Monday
        self.assertIn("uit tot ma 08:00", page)
        self.assertIn("Uit tot ma 08:00", page)

        page = self.post("/schedule", **{**fields, "monday_to": "kwart over"})
        self.assertIn("Maandag: Ongeldige tijd", page)
        self.assertEqual(self.store.get().week_schedule["monday"], "08:00-18:00")  # unchanged

        del fields["enabled"]
        page = self.post("/schedule", **fields)
        self.assertIn("schema uit config.toml geldt", page)
        self.assertIn("name='friday_to' value='18:00'", page)  # times kept for later

    def test_shut_down(self):
        self.assertNotIn("/power/off", self.get())  # no shutdown given: no button
        calls = []
        self.app.shutdown = lambda: calls.append("off")
        page = self.get()
        self.assertIn("action='/power/off'", page)
        self.assertIn("confirm(", page)
        page = self.post("/power/off")
        self.assertEqual(calls, ["off"])
        self.assertIn("De Pi wordt uitgeschakeld", page)

        def refuse():
            raise OSError("geen rechten om de Pi uit te schakelen")
        self.app.shutdown = refuse
        page = self.post("/power/off")
        self.assertIn("Dat lukte niet: geen rechten", page)

    def test_reboot(self):
        self.assertNotIn("/power/reboot", self.get())  # no reboot given: no button
        calls = []
        self.app.reboot = lambda: calls.append("reboot")
        page = self.get()
        self.assertIn("action='/power/reboot'", page)
        self.assertNotIn("/power/off", page)
        page = self.post("/power/reboot")
        self.assertEqual(calls, ["reboot"])
        self.assertIn("De Pi start opnieuw op", page)

        def refuse():
            raise OSError("geen rechten om de Pi te herstarten")
        self.app.reboot = refuse
        page = self.post("/power/reboot")
        self.assertIn("Dat lukte niet: geen rechten om de Pi te herstarten", page)

    def test_wifi_escapes_names(self):
        nm = FakeNmcli({"x": {"ssid": "<b>x</b>"}}, visible=["'><script>"])
        self.app.wifi = Wifi(run=nm)
        page = self.get()
        self.assertNotIn("<b>x", page)
        self.assertNotIn("<script>", page)


class FakeNmcli:
    """Stands in for nmcli: connection name -> {"ssid", "psk", "active", "priority"}."""

    def __init__(self, connections=(), visible=()):
        self.connections = {name: {"psk": None, "active": False, "priority": 0, **c}
                            for name, c in dict(connections).items()}
        self.visible = list(visible)

    @staticmethod
    def escape(value):
        return value.replace("\\", "\\\\").replace(":", "\\:")

    def __call__(self, *args):
        if args == ("-t", "-f", "NAME,TYPE,ACTIVE", "connection", "show"):
            return "netplan-eth0:802-3-ethernet:yes\n" + "".join(
                f"{self.escape(n)}:802-11-wireless:{'yes' if c['active'] else 'no'}\n"
                for n, c in self.connections.items())
        if args[:2] == ("-g", "802-11-wireless.ssid"):
            return self.escape(self.connections[args[5]]["ssid"]) + "\n"
        if args[:4] == ("-t", "-f", "SSID", "device"):
            return "".join(self.escape(s) + "\n" for s in self.visible) + "\n"  # + a hidden one
        if args == ("device", "wifi", "rescan"):
            return ""
        if args[:2] == ("connection", "add"):
            opts = dict(zip(args[2::2], args[3::2]))
            self.connections[opts["con-name"]] = {
                "ssid": opts["ssid"], "psk": opts.get("wifi-sec.psk"), "active": False,
                "priority": int(opts["connection.autoconnect-priority"])}
            return ""
        if args[:3] == ("connection", "modify", "id"):
            rest = args[4:]
            self.connections[args[3]]["psk"] = (None if rest[0] == "remove"
                                                else dict(zip(rest[::2], rest[1::2]))["wifi-sec.psk"])
            return ""
        if args[:3] == ("connection", "delete", "id"):
            del self.connections[args[3]]
            return ""
        raise WifiError(f"unexpected nmcli {args}")


class WifiTest(unittest.TestCase):
    def test_default_network_added_once_at_lowest_priority(self):
        nm = FakeNmcli({"thuis": {"ssid": "Thuis"}})
        wifi = Wifi("Mwg mobiel", "Welkom01", run=nm)
        wifi.ensure_default()
        self.assertEqual(nm.connections["Mwg mobiel"],
                         {"ssid": "Mwg mobiel", "psk": "Welkom01", "active": False, "priority": -10})
        wifi.ensure_default()
        self.assertEqual(len(nm.connections), 2)
        Wifi("", "", run=nm).ensure_default()  # no default configured: nothing happens
        self.assertEqual(len(nm.connections), 2)
        with self.assertRaises(WifiError):
            wifi.remove("Mwg mobiel")

    def test_add_updates_known_network(self):
        nm = FakeNmcli({"netplan-wlan0-Thuis": {"ssid": "Thuis", "psk": "oudwachtwoord"}})
        wifi = Wifi(run=nm)
        wifi.add("Thuis", "nieuwwachtwoord")
        self.assertEqual(list(nm.connections), ["netplan-wlan0-Thuis"])
        self.assertEqual(nm.connections["netplan-wlan0-Thuis"]["psk"], "nieuwwachtwoord")
        wifi.add("Thuis", "")  # now an open network
        self.assertIsNone(nm.connections["netplan-wlan0-Thuis"]["psk"])
        for ssid, password in (("  ", "geheim123"), ("Thuis", "x" * 64)):
            with self.assertRaises(WifiError):
                wifi.add(ssid, password)

    def test_names_with_colons(self):
        nm = FakeNmcli({"Café: boven": {"ssid": "Café: boven"}}, visible=["A:B", "A:B", "C"])
        wifi = Wifi(run=nm)
        self.assertEqual([(n.name, n.ssid) for n in wifi.saved()], [("Café: boven", "Café: boven")])
        self.assertEqual(wifi.visible(), ["A:B", "C"])
        wifi.remove("Café: boven")
        self.assertEqual(nm.connections, {})

    def test_settings_default_network(self):
        self.assertEqual(settings_mod.Settings().wifi_ssid, "Mwg mobiel")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.toml"
            path.write_text('[wifi]\ndefault_ssid = ""\n')
            self.assertEqual(settings_mod.load(path).wifi_ssid, "")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class TranscodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name) / "media"
        self.out = Path(self.tmp.name) / "converted"
        self.media.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_wmv(self, name, size="320x240", seconds=1):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=duration={seconds}:size={size}:rate=60", "-f", "lavfi", "-i",
                        f"sine=duration={seconds}", "-c:v", "wmv2", "-c:a", "wmav2",
                        str(self.media / name)], check=True)

    def probe(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name,width,height,avg_frame_rate", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True, check=True).stdout
        return out.strip()

    def test_converts_and_playlist_uses_mp4(self):
        self.make_wmv("film.wmv", size="2560x1440")
        (self.media / "foto.jpg").write_bytes(b"x")
        status = SyncStatus()
        t = Transcoder(self.media, self.out, status)
        cfg = parse({"files": {"film.wmv": {"duration": 7}}})

        before = playlist.build(self.media, cfg, dt.date.today(), converted=t.playable)
        self.assertEqual([s.path.name for s in before], ["foto.jpg"])  # not ready: skipped

        t.run_once()
        self.assertEqual(status.converting, "")
        mp4 = t.playable(self.media / "film.wmv")
        self.assertEqual(self.probe(mp4), "h264,1920,1080,30/1")  # downscaled, 30 fps cap
        slides = playlist.build(self.media, cfg, dt.date.today(), converted=t.playable)
        self.assertEqual([(s.path, s.kind, s.duration) for s in slides],
                         [(mp4, "video", 7), (self.media / "foto.jpg", "image", 10)])
        self.assertEqual(t.pending(), [])

        # A replaced source is converted again and the old conversion removed.
        self.make_wmv("film.wmv", seconds=2)
        os.utime(self.media / "film.wmv", (5, 5))
        self.assertEqual(t.pending(), [self.media / "film.wmv"])
        t.run_once()
        self.assertEqual(len(list(self.out.iterdir())), 1)
        self.assertEqual(self.probe(t.playable(self.media / "film.wmv")), "h264,320,240,30/1")

        # A deleted source takes its conversion with it.
        (self.media / "film.wmv").unlink()
        t.run_once()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_decoder_choice_by_width(self):
        from monsterscreen import player
        self.assertEqual(player.hwdec_for(1920), "v4l2m2m")
        self.assertEqual(player.hwdec_for(1280), "v4l2m2m")
        self.assertEqual(player.hwdec_for(720), "v4l2m2m-copy")  # black with zero-copy on a Pi 3
        self.assertEqual(player.hwdec_for(None), "v4l2m2m")  # unknown: keep the configured one
        self.make_wmv("pal.wmv", size="720x576")
        self.assertEqual(player.video_width(self.media / "pal.wmv"), 720)
        self.assertIsNone(player.video_width(self.media / "missing.mp4"))

    def test_broken_file_is_not_retried_forever(self):
        (self.media / "kapot.wmv").write_bytes(b"geen video")
        t = Transcoder(self.media, self.out)
        t.run_once()
        self.assertIsNone(t.playable(self.media / "kapot.wmv"))
        self.assertEqual(t.pending(), [])
        self.assertEqual([f for f in self.out.iterdir()], [])  # no leftover .part file


class NetinfoTest(unittest.TestCase):
    def test_split_terse(self):
        self.assertEqual(netinfo.split_terse(r"*:Mijn\:Netwerk"), ["*", "Mijn:Netwerk"])
        self.assertEqual(netinfo.split_terse("eth0:ethernet:unavailable"),
                         ["eth0", "ethernet", "unavailable"])

    def test_summary(self):
        wifi = netinfo.Link("wifi", ["192.168.1.23"], "Thuis")
        lan = netinfo.Link("ethernet", ["192.168.1.40"])
        self.assertEqual(netinfo.format_summary("pisignage", True, [wifi], 8080),
                         'pisignage\n\n'
                         'Wifi "Thuis": 192.168.1.23\n\n'
                         'Instellen: http://192.168.1.23:8080\n'
                         'of http://pisignage.local:8080')
        self.assertEqual(netinfo.format_summary("pisignage", False, [lan, wifi], 80),
                         'pisignage\n\n'
                         'Kabel (LAN): 192.168.1.40\n'
                         'Wifi "Thuis": 192.168.1.23\n\n'
                         'Instellen: http://192.168.1.40:80')
        self.assertEqual(netinfo.format_summary("pisignage", True, [], 8080),
                         "pisignage\n\nGeen netwerkverbinding")
        self.assertEqual(netinfo.format_summary("pisignage", False, [netinfo.Link("wifi")], 8080),
                         "pisignage\n\nWifi: nog geen IP-adres")

    def test_summary_without_wifi_chip(self):
        lan = netinfo.Link("ethernet", ["192.168.1.40"])
        self.assertEqual(netinfo.format_summary("pisignage", False, [lan], 8080, wifi_found=False),
                         'pisignage\n\n'
                         'Kabel (LAN): 192.168.1.40\n'
                         'Wifi: geen wifi-chip gevonden\n\n'
                         'Instellen: http://192.168.1.40:8080')
        self.assertEqual(netinfo.format_summary("pisignage", False, [], 8080, wifi_found=False),
                         "pisignage\n\nGeen netwerkverbinding\nWifi: geen wifi-chip gevonden")


class ScreenTimeTest(unittest.TestCase):
    FRIDAY = dt.datetime(2026, 10, 23, 14, 0)

    def week(self, **days):
        return WeekSchedule.from_state({"enabled": True, **days})

    def test_overnight_and_all_day(self):
        week = self.week(friday="18:00-02:00", sunday="00:00-00:00")
        at = lambda d, h, m=0: dt.datetime(2026, 10, d, h, m)  # 23 = Friday
        self.assertFalse(week.is_on(at(23, 17, 59)))
        self.assertTrue(week.is_on(at(23, 18)))
        self.assertTrue(week.is_on(at(24, 1, 59)))  # Saturday night, still Friday's period
        self.assertFalse(week.is_on(at(24, 2)))
        self.assertFalse(week.is_on(at(24, 12)))  # Saturday not set: off
        self.assertTrue(week.is_on(at(25, 0)))
        self.assertTrue(week.is_on(at(25, 23, 59)))
        self.assertEqual(WeekSchedule.from_state(week.to_state()), week)

    def test_next_start_and_time_of_day(self):
        week = self.week(**{d: "08:00-18:00" for d in
                            ("monday", "tuesday", "wednesday", "thursday", "friday")})
        self.assertEqual(next_start(week, self.FRIDAY), dt.datetime(2026, 10, 26, 8, 0))
        self.assertIsNone(next_start(self.week(), self.FRIDAY))  # never on
        self.assertEqual(next_time_of_day(self.FRIDAY, "03:00"), dt.datetime(2026, 10, 24, 3, 0))
        self.assertEqual(next_time_of_day(self.FRIDAY, "23:30"), dt.datetime(2026, 10, 23, 23, 30))
        with self.assertRaises(ValueError):
            next_time_of_day(self.FRIDAY, "3 uur")

    def test_override_and_description(self):
        week = self.week(friday="08:00-18:00", monday="08:00-18:00")
        now = self.FRIDAY
        self.assertEqual(describe(week, None, now), "Aan, volgens schema tot 18:00")
        self.assertEqual(describe(week, None, now.replace(hour=19)), "Uit, volgens schema tot ma 08:00")
        late = Override(True, dt.datetime(2026, 10, 24, 3, 0))
        self.assertTrue(is_on(week, late, now.replace(hour=23)))
        self.assertEqual(describe(week, late, now.replace(hour=23)), "Aan tot morgen 03:00")
        self.assertFalse(is_on(week, late, dt.datetime(2026, 10, 24, 3, 0)))  # expired
        off = Override(False, None)
        self.assertFalse(is_on(week, off, now))
        self.assertEqual(describe(week, off, now), "Uit tot je het weer verandert")
        self.assertEqual(Override.from_state(late.to_state()), late)

    def test_base_schedule(self):
        from_config = parse({"schedule": {"enabled": True, "friday": "off"}}).schedule
        self.assertIs(base_schedule(None, from_config), from_config)
        self.assertIs(base_schedule({"enabled": False, "friday": "08:00-18:00"}, from_config),
                      from_config)
        self.assertTrue(base_schedule({"enabled": True, "friday": "08:00-18:00"},
                                      from_config).is_on(self.FRIDAY))


if __name__ == "__main__":
    unittest.main()
