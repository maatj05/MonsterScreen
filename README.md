# MonsterScreen

Dropbox op het scherm.

Toont de foto's en video's uit een Dropbox-map op het scherm dat aan een
Raspberry Pi 3 hangt. Elke submap van `Mediakranten/Present-it` is een
presentatie; welke dit scherm toont kies je op een instelpagina. Wie de map
beheert, stuurt verder alles aan via Dropbox: bestanden toevoegen of
weghalen, en bijzonderheden regelen in `config.toml`.

## Hoe het werkt

```
Mediakranten/Present-it/<presentatie>
    │  (elke 5 min, alleen wijzigingen)
    ▼
lokale kopie op de Pi ──▶ mpv ──▶ HDMI-scherm
    ▲
instelpagina http://<pi>:8080: Dropbox koppelen, presentatie kiezen
```

- **Offline bestendig:** er wordt afgespeeld van de lokale kopie. Valt het
  internet weg, dan blijft het scherm gewoon draaien.
- **Licht genoeg voor een Pi 3:** mpv speelt direct op het scherm af (geen
  desktop of browser nodig) en gebruikt de hardware-videodecoder.
- **Fouten in config.toml** zetten het scherm niet op zwart: de vorige
  instellingen blijven gelden en de fout komt in het logboek.

Ondersteund: `jpg jpeg png gif webp bmp` en
`mp4 m4v mov mkv avi webm wmv asf mpg mpeg ts mts m2ts 3gp flv`.
Andere bestanden worden genegeerd. WMV/ASF-video's kan de Pi niet vloeiend
afspelen; die zet hij na het downloaden zelf om naar MP4 (dat duurt op een
Pi 3 een paar minuten per video, de instelpagina toont de voortgang). Tot
dan wordt zo'n video overgeslagen. Houd video's voor de Pi 3 op maximaal
1080p H.264 (zoals de meeste telefoons en camera's opnemen).

## Installatie

1. **Pi voorbereiden.** Installeer *Raspberry Pi OS Lite (Bookworm)* met
   Raspberry Pi Imager, met wifi en SSH ingesteld. Controleer de tijdzone
   (`sudo raspi-config` → Localisation → Timezone), anders klopt het
   aan/uit-schema niet.

2. **Dropbox-app** (de BMSignage-app met App key `q34alcs3we24vx7` bestaat
   al en is standaard ingesteld; alleen nodig voor een eigen app) op
   <https://www.dropbox.com/developers/apps>:
   - *Scoped access* → **Full Dropbox** (de presentaties staan in
     `Mediakranten/Present-it`, buiten een app-map).
   - Tabblad *Permissions*: vink `files.metadata.read`,
     `files.content.read` en `files.content.write` aan en klik *Submit*.
     Het schrijfrecht is alleen nodig om `config.toml` in een nieuw
     gekozen map te zetten.
   - Noteer de **App key**.

3. **Code op de Pi zetten en installeren:**
   ```sh
   sudo apt install -y git
   git clone https://github.com/maatj05/MonsterScreen.git && cd MonsterScreen
   ./install.sh
   ```

4. **Instellen via de instelpagina.** Het scherm toont nu
   *"Dit scherm is nog niet ingesteld"* met een adres zoals
   `http://192.168.1.83:8080`. Open dat adres op een laptop of telefoon in
   hetzelfde netwerk en:
   1. **Verder** (de App key van BMSignage staat al ingevuld) →
      **Open Dropbox** → *Toestaan* → plak de code;
   2. kies de presentatie: een map onder `Mediakranten/Present-it`, op elke diepte.

   Een map met submappen kun je openen (›) om een submap te kiezen; met
   **Tonen** kies je de map zelf. Het scherm begint direct. Staat er in die map nog geen `config.toml`, dan
   wordt er een aangemaakt met de standaardinstellingen en uitleg.

Later een andere presentatie tonen of opnieuw koppelen: open dezelfde
pagina weer. Die toont ook de status (laatste update, aantal bestanden,
fouten).

Logboek bekijken: `journalctl -u monsterscreen -f`

Bij het opstarten toont het scherm 20 seconden hoe je de Pi bereikt: de
naam, per verbinding wifi (met netwerknaam) of kabel (LAN) met het
IP-adres, en het adres van de instelpagina. Hetzelfde staat in het logboek.

### Wifi

Onder **Wifi** op de instelpagina staan de netwerken die de Pi kent; voeg
er een toe (kies uit de netwerken in de buurt of typ de naam) of verwijder
er een. De Pi maakt vanzelf verbinding met een bekend netwerk in de buurt.

De Pi kent altijd het netwerk **Mwg mobiel** (wachtwoord `Welkom01`), met de
laagste voorkeur. Op een nieuwe plek: zet een hotspot met die naam aan op
je telefoon, verbind je laptop of telefoon ermee, en open het adres dat het
scherm toont om de wifi van die plek toe te voegen. Een ander of geen
standaardnetwerk stel je in onder `[wifi]` in `settings.toml`.

### Uitschakelen

Met **Uitschakelen** onderaan de instelpagina sluit de Pi netjes af. Wacht
tot het groene lampje niet meer knippert voordat je de stekker eruit
haalt; weer aanzetten doe je door de stekker eruit en er weer in te doen.

`install.sh` geeft de service het recht om wifi-netwerken te beheren en de
Pi uit te schakelen (polkit-regel `/etc/polkit-1/rules.d/50-monsterscreen.rules`).

De instelpagina is bereikbaar voor iedereen in het lokale netwerk. Hij toont
nooit het Dropbox-token, maar wie hem kan openen kan wel een andere
presentatie kiezen. Zet de Pi dus niet in een openbaar gastnetwerk.

`settings.toml` op de Pi is optioneel; daar staan hardware-instellingen
(zie [`settings.example.toml`](settings.example.toml)), zoals de basismap
en hoe het scherm uit gaat. Zonder instelpagina koppelen kan ook nog:
`python3 get_refresh_token.py <app key>`.

## config.toml (in Dropbox)

Zie [`voorbeeld/config.toml`](voorbeeld/config.toml) voor alle opties met
uitleg. Samengevat:

| Onderdeel | Wat |
|---|---|
| `[display]` | duur per foto, volgorde (naam / nieuwste / willekeurig), geluid, max. videoduur, beeldvullend of niet, draaien |
| `[sync]` | hoe vaak Dropbox gecontroleerd wordt |
| `[files."naam.jpg"]` | per bestand: andere `duration`, `skip`, of alleen tonen `from`/`until` een datum |
| `[schedule]` | automatisch aan/uit per weekdag, met uitzonderingen per datum in `[schedule.dates]` |
| `[news]` | gereserveerd voor nieuwsberichten (latere versie) |

## Scherm automatisch aan en uit

Buiten de tijden in `[schedule]` gaat het scherm uit. *Hoe* dat gebeurt hangt
van het scherm af en stel je in `settings.toml` op de Pi in (`[power] method`):

| Methode | Wanneer |
|---|---|
| `black` | Standaard. Toont zwart; het scherm zelf blijft aan. |
| `cec` | TV's: zet de TV in stand-by via HDMI-CEC en weer aan. Meestal de beste keuze voor een TV. Zet CEC aan in het TV-menu (heet vaak Anynet+, Bravia Sync, SimpLink). |
| `vcgencmd` | Monitoren: zet het HDMI-signaal uit zodat de monitor gaat slapen. Werkt alleen met de fkms-driver (`dtoverlay=vc4-fkms-v3d` in `/boot/firmware/config.txt`). |
| `command` | Eigen commando's in `on_command` / `off_command`. |

## Nieuws (latere versie)

De code is erop voorbereid: `playlist.interleave()` voegt extra dia's toe na
elke *n* foto's/video's, en `[news]` in `config.toml` wordt al ingelezen. Een
nieuwsbron hoeft alleen berichten van de API op te halen, ze als afbeelding
te renderen (bv. met Pillow) en als `Slide` aan te leveren; de speler en de
rest blijven ongewijzigd.

## Ontwikkelen

```sh
python3 -m unittest discover -s tests
```

Lokaal draaien (met desktop, in een venster): zet in `settings.toml`
`mpv_args = []` en start `python3 -m monsterscreen.main`.
