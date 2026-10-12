# Idee: Device-Panel nach Druckstart öffnen

Noch nicht implementiert. Stand: 12.10.2026.

Wenn nach **Print** in Orcas Preview ein neuer Druckauftrag auf dem ausgewählten
Drucker beginnt, soll das Device-Panel einmal automatisch aufgehen. Manuelles
Schließen während dieses Auftrags bleibt wirksam; Pause/Fortsetzen öffnet es
nicht erneut. Beim Laden der Browserseite oder Wechsel des Druckerprofils soll
ein bereits laufender Druck nicht automatisch das Panel öffnen.

Moonraker liefert Druckstatus und Job-Historie bereits über unseren Proxy mit
dem in Orca gespeicherten API-Key. Eine kleine Erweiterung des Panels kann neue
Jobs erkennen, etwa per WebSocket-Ereignis `notify_history_changed` oder durch
Abfrage der neuesten Job-ID. Orca und Selkies brauchen dafür keine Änderungen.

Diese Variante reagiert auch auf Druckstarts aus anderen Anwendungen. Öffnen
nach einem reinen Upload ohne Druckstart wäre eine getrennte Variante und
müsste erfolgreiche Dateiübertragungen erkennen.

Referenz: [Moonraker: History Changed](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/#history-changed).
