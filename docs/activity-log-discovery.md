# Registro attività Shelly Cloud: discovery (2026-10-27)

## Esito

Trovato. Il "Registro delle attività" dell'app è `POST /statistics/event-log`, non documentato.
L'ho trovato nel bundle JS della web app `control.shelly.cloud` (v3.31.10, `assets/index-*.js`, funzione `Vx`) e l'ho
verificato dal vivo con la `auth_key` su entrambe le case. Il dispositivo dello screenshot, "bathroom thermostat",
restituisce esattamente le righe viste nell'app: 26/10 14:31:00 OFF, 13:30:01 ON, 25/10 13:44:00 OFF...

## Richiesta

- `POST https://<server>/statistics/event-log?auth_key=<key>`, body JSON `{"tags": [...], "limit": N}`.
  L'app usa JSON più `Authorization: Bearer <token OAuth>`.
- Funziona anche come form: `auth_key=<key>&tags[0]=<tag>&limit=N`, cioè l'array in stile qs, compatibile con
  `ShellyCloud._request`. Con `tags=["..."]` (JSON dentro il form) la risposta è 400 `VALIDATION_ERRORS`.
- `tags`: id del dispositivo (`MAC` = canale 0) più `MAC_N` per i canali N>0, come gli id di `get_all_lists`.
  Si possono passare molti tag in una richiesta: 93 tag hanno dato 38 KB in una sola chiamata.
- Paginazione: se la pagina è piena la risposta include `pointer`, un JWT con `{pointer:{tag:{p:rowid,c:n}}, uid, tagMax, v2, iat}`.
  La pagina successiva si chiede con body `{"pointer": "<jwt>", "limit": N}`, senza `tags`.
- Rate limit: è lo stesso dell'account (1 req/s documentato) e va fatto passare per il `Throttle` condiviso.

## Risposta

```json
{"errors": [], "result": {"<tag>": [{"tag": "<tag>", "t": 1793104441840, "e": 1, "p": "[\"<mac>\",0,false]", "rowid": 55}]}, "pointer": "..."}
```

- `t`: epoch in **millisecondi**, UTC; l'app lo mostra in ora locale. L'ordine è decrescente.
- `rowid`: intero crescente per unit, adatto alla deduplicazione. Su "Home A", casa nuova, vale circa 55; sull'altra
  casa circa 286000.
- `p`: stringa JSON. `p[0]` è il MAC base e il resto si legge secondo `e` (mappa `Qwo` del bundle):
  - `1` turn: `[mac, channel, bool]`. È **la transizione ON/OFF**.
  - `8` brightness: `[mac, channel, 0-100]`.
  - `13` input/reason: `[mac, channel, "S"|"L"|"SSS"|bool, "v_eve"|"input"]` (pressioni pulsante ed eventi di input).
  - `5` cover: `[mac, channel, "open"|"opening"|..., pos]`.
  - `4` sensore: `[mac, "status_update", tmp?, bat?, hum?]`.
  - Altri id nel bundle: 6 scena/allarme, 7, 10, 12, 14-24 (sensori, porte, lock, camera).
- Gli eventi `1` **non portano la sorgente** (app, pulsante, schedule): l'input fisico compare come evento `13` separato.

## Limite importante

**Il cloud conserva solo gli ultimi 5 eventi per tag.** Su 93 tag: 73 ne hanno esattamente 5, nessuno di più. La
paginazione con `limit=4` si esaurisce dopo 5 righe. L'evento più vecchio visto risale al 2024-09-12, su un tag poco
attivo: è un ring buffer per tag, non una finestra temporale. Lo storico pregresso quindi non si può recuperare: serve
interrogarlo spesso, una richiesta per casa con tutti i tag, e accumulare in locale deduplicando su `rowid` (o
`(tag, t)`). Un tag con più di 5 eventi fra due letture perde quelli intermedi. Nel caso d'uso (termostati) succede
raramente.

## Confronto con il DB

kitchen thermostat (`aabbccddee01` ch0):
- l'evento OFF è delle 13:34:01.840 locali;
- nel DB il campione delle 13:34:00 è ancora ON, quello delle 13:35:00 è OFF.

Coincidono al minuto: il polling registra la transizione nel ciclo successivo, con 58 s di ritardo; l'event-log dà il
secondo esatto. Il DB contiene solo circa 3 h (dalle 13:03), quindi è stata confrontabile una sola transizione.

## Prove fatte (11 richieste, distanziate di 6 s o più)

| # | Variante | Esito |
|---|---|---|
| 1 | JSON + `?auth_key`, 1 tag, limit 20 | 200, 5 righe, nessun pointer |
| 2 | idem, limit 2 | 200, 2 righe + `pointer` |
| 3 | `{pointer, limit 2}` | 200, le 2 righe successive |
| 4 | 28 tag (home A), limit 100 | 200, 55 righe, massimo 5 per tag |
| 5 | bathroom thermostat, limit 50 | 200, 5 righe, identiche allo screenshot |
| 6 | 93 tag (altra casa), limit 500 | 200, 369 righe, 73 tag a 5, nessun pointer |
| 7 | 1 tag, limit 5 | 200, 5 righe, nessun pointer |
| 8-9 | limit 4 + pointer | 4 + 1 righe, poi fine |
| 10 | form `tags[0]=` | 200, funziona |
| 11 | form `tags=<json>` | 400 VALIDATION_ERRORS |

La ricerca documentale (cloud-control-api, search) non ha trovato nulla: l'endpoint non è documentato. Non ho provato
la variante Bearer con `auth_key` perché non serviva.

Fixture anonimizzata: `tests/fixtures/activity_log.json`, 5 tag con eventi 1, 8, 13, 5 e 4. I MAC sono sostituiti da
`aabbccddee01..05`.
