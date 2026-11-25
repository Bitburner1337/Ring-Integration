# Ring Plus für Home Assistant

Eigene Ring-Integration (Domain `ring_plus`). Sie basiert auf der offiziellen
Ring-Integration von Home Assistant, ist aber unabhängig von Core-Updates und
bringt Erweiterungen mit: Clips lokal speichern, Mediathek, Event-Verlauf und
Cloud-Archiv in Internxt.

Stand: **v0.7.0**

## Was Ring Plus kann

### Alles aus der offiziellen Integration
Anmeldung mit 2FA, Kameras (Live-Ansicht per WebRTC, letzte Aufnahme),
Event-Entitäten für Klingeln und Bewegung, Lautstärke, Sirene, Licht und
Sensoren.

### Verbesserungen
- **Neue Stick Up Cams** (`cocoa_camera_v2`) werden erkannt und bekommen
  Bewegungs-Events. Die offizielle Integration meldet nur „Unknown kind“.
- **Echtzeit-Events funktionieren wieder.** Ring schickt die Schlüssel der
  Push-Nachrichten inzwischen ohne Base64-Auffüllung, daran brach die
  Bibliothek firebase-messaging ab („Incorrect padding“). Siehe `fcm_fix.py`.
- **Watchdog:** prüft jede Minute den Echtzeit-Empfang. Ist er abgestürzt,
  hängt er (über 3 Minuten kein Lebenszeichen) oder kommt er nicht hoch,
  wird er neu aufgebaut. Die Wartezeit zwischen Versuchen steigt bis
  15 Minuten.

### Clips speichern
Jede fertige Aufnahme (Bewegung, Klingeln) wird gespeichert als

    /media/ring/<Kamera>/<JJJJ-MM-TT_HH-MM-SS>_<motion|ding>.mp4

mit einem Vorschaubild (`.jpg`) daneben. Ring Plus nutzt dafür die
Verlaufsdaten, die ohnehin jede Minute abgefragt werden. Aufnahmen, die
noch nicht fertig sind, werden beim nächsten Durchlauf geholt (bis zu
5 Versuche, nur Aufnahmen der letzten 24 Stunden). Beim Start werden auch
Clips nachgeholt, die zwischen zwei Abfragen untergegangen sind.

Schutz für den Speicher: Unter 2 GB freiem Platz pausiert der Download.

> Tipp: Den Ordner „Medien“ aus den automatischen HA-Backups ausnehmen,
> sonst landen die Clips in jedem Backup.

### Mediathek
Im Medienbrowser (Seitenleiste „Medien“) unter **Ring Plus**:
Kamera → Tag („Heute, 25.11.2026 (3 Clips)“) → Clip
(„11:35:54 · Klingeln“) mit Vorschaubild, neueste zuerst.

### Event-Verlauf
- Kalender **„Ring Plus Verlauf“**: jedes Event als Eintrag, z. B.
  „Hof: Bewegung“. Standard sind 30 Tage. Kalender-Trigger in
  Automationen funktionieren damit auch.
- Sensor **„Ring Plus Events heute“**: Anzahl der Events heute. Das
  Attribut `events` enthält die letzten 20 Events mit Zeit, Kamera, Art und
  Clip (`media-source://ring_plus/...`), gedacht für Dashboards.
- Beim Start werden fehlende Events aus dem Ring-Verlauf nachgeladen
  (50 pro Kamera).

### Cloud-Archiv in Internxt
Voraussetzung ist die Internxt-Integration
([ha-internxt](https://github.com/TheRealSimon42/ha-internxt)).

- Jeder Clip (MP4 und Vorschaubild) wird über deren Aktion
  `internxt.upload` hochgeladen nach `Ring-Clips/<Kamera>/<Datum>/`. Die
  Dateien werden auf dem HA-Gerät verschlüsselt, Ring Plus nutzt die
  Anmeldung der Internxt-Integration.
- Ein Clip nach dem anderen. Bei Fehlern gibt es einen neuen Versuch nach
  1, 5, 15, 30 und 60 Minuten. Der Clip bleibt so lange lokal.
- **Lokal gelöscht wird ein Clip erst, wenn er älter als die
  Aufbewahrungszeit ist und Internxt den Upload bestätigt hat.**
- Ist Internxt nach einem Neustart noch nicht geladen, wartet Ring Plus
  einfach. Eine Warnung gibt es erst nach 5 Minuten.

## Einstellungen

Einstellungen → Geräte & Dienste → Ring Plus → **Konfigurieren**:

| Einstellung | Standard | Bedeutung |
|---|---|---|
| Clips lokal aufbewahren | 7 Tage | Danach werden Clips lokal gelöscht (mit Cloud-Archiv nur nach bestätigtem Upload). |
| Clips nach Internxt hochladen | an | Cloud-Archiv ein/aus. Aus: lokal nur nach Alter löschen. |
| Event-Verlauf im Kalender | 30 Tage | So lange bleiben Events im Kalender. |

Nach dem Speichern lädt die Integration automatisch neu.

## Diagnose

Das Gerät **„Ring Plus“** hat diese Sensoren:

| Sensor | Zeigt |
|---|---|
| Event-Verbindung | Verbunden / Verbindet / Verbindet neu / Getrennt, mit Anzahl und Grund der Neuverbindungen |
| Letztes Event | Zeitpunkt des letzten Echtzeit-Events, mit Gerät und Art |
| Events heute | siehe Event-Verlauf |
| Cloud-Archiv Warteschlange | Clips, die noch auf den Upload warten. Attribute: Status, letzter Upload, letzter Fehler, nächster Versuch |

„Diagnosedaten herunterladen“ enthält zusätzlich den Zustand von
Clip-Download, Archiv, Verlauf und Event-Verbindung, ohne Zugangsdaten.

## Installation

1. HACS / Marketplace → ⋮ → Benutzerdefinierte Repositories →
   `https://github.com/Bitburner1337/Ring-Integration`, Typ „Integration“.
2. „Ring Plus“ installieren, Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → Integration hinzufügen → „Ring Plus“,
   mit Ring-Konto anmelden und den 2FA-Code eingeben.

Ring Plus kann parallel zur offiziellen Ring-Integration laufen. Sie legt bei
ring.com ein eigenes „autorisiertes Gerät“ an.

## Fehlersuche

- **Keine Echtzeit-Events:** Sensor „Event-Verbindung“ ansehen. Bei
  Innenkameras ist die Bewegungserkennung je nach Ring-Modus aus
  (z. B. „Zuhause“).
- **Keine Clips:** Ohne Ring-Abo (Ring Protect) gibt es keine Aufnahmen.
  Unter 2 GB freiem Speicher pausiert der Download.
- **Clips bleiben lokal liegen:** Sensor „Cloud-Archiv Warteschlange“
  ansehen (Status und letzter Fehler). Die Internxt-Integration muss geladen
  sein.

## Lizenz

Apache License 2.0, siehe `LICENSE` und `NOTICE`.
