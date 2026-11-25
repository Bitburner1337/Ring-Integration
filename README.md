# Ring Plus für Home Assistant

Eigene Ring-Integration (Domain `ring_plus`), basierend auf der offiziellen
Home-Assistant-Ring-Integration, aber unabhängig von Core-Updates und mit
Erweiterungen.

## Stand (v0.1.0)

- Alles, was die offizielle Ring-Integration kann: Anmeldung mit 2FA,
  Kameras (Live-Ansicht per WebRTC, letzte Aufnahme), Event-Entitäten für
  Klingeln und Bewegung, Lautstärke, Sirene, Licht, Sensoren.
- **Neu:** Stick Up Cams vom Typ `cocoa_camera_v2` werden erkannt und bekommen
  Bewegungs-Events (die offizielle Integration meldet nur „Unknown kind“).

## Geplant

Watchdog für den Event-Listener, Clip-Download nach `/media/ring`, Mediathek,
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
