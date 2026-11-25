# Ring Plus für Home Assistant

Eigene Ring-Integration (Domain `ring_plus`), basierend auf der offiziellen
Home-Assistant-Ring-Integration, aber unabhängig von Core-Updates und mit
Erweiterungen.

## Stand (v0.2.0)

- Alles, was die offizielle Ring-Integration kann: Anmeldung mit 2FA,
  Kameras (Live-Ansicht per WebRTC, letzte Aufnahme), Event-Entitäten für
  Klingeln und Bewegung, Lautstärke, Sirene, Licht, Sensoren.
- **Neu:** Stick Up Cams vom Typ `cocoa_camera_v2` werden erkannt und bekommen
  Bewegungs-Events (die offizielle Integration meldet nur „Unknown kind“).
- **Fix:** Echtzeit-Events (Klingeln, Bewegung) kommen wieder an. Ring schickt
  die Schlüssel der Push-Nachrichten inzwischen ohne Base64-Auffüllung; die
  Bibliothek firebase-messaging brach daran ab und beendete den Listener
  („Incorrect padding“). Siehe `fcm_fix.py`.
- **Watchdog:** prüft jede Minute den Echtzeit-Empfang. Ist er abgestürzt,
  hängt er (über 3 Minuten kein Lebenszeichen) oder kommt er nicht hoch, wird
  er neu aufgebaut (Wartezeit zwischen Versuchen steigt bis 15 Minuten).
- **Diagnose-Gerät „Ring Plus“** mit zwei Sensoren:
  - *Event-Verbindung*: Verbunden / Verbindet / Verbindet neu / Getrennt,
    Attribute: Anzahl und Grund der Neuverbindungen, letztes Lebenszeichen
  - *Letztes Event*: Zeitpunkt des letzten Echtzeit-Events, mit Gerät und Art

## Geplant

Clip-Download nach `/media/ring`, Mediathek,
Event-Verlauf, Cloud-Archiv über die Internxt-Integration, Optionen-Dialog.

## Installation

1. HACS / Marketplace → ⋮ → Benutzerdefinierte Repositories →
   `https://github.com/Bitburner1337/Ring-Integration`, Typ „Integration“.
2. „Ring Plus“ installieren, Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → Integration hinzufügen → „Ring Plus“,
   mit Ring-Konto anmelden und den 2FA-Code eingeben.

Ring Plus kann parallel zur offiziellen Ring-Integration laufen. Sie legt bei
ring.com ein eigenes „autorisiertes Gerät“ an.

## Lizenz

Apache License 2.0, siehe `LICENSE` und `NOTICE`.
