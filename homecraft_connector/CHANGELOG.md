# Changelog

## 0.24.0

- Se souhlasem **Posílat chybová hlášení Homecraftu** doplněk zapíše
  `packages/homecraft_system_log.yaml` se `system_log: fire_event: true`, aby
  chyby automatizací a skriptů Homecraftu z logu HA opravdu dorazily
  (homecraftcz/homecraft#906); po vypnutí souhlasu soubor smaže. Souhlas
  zapnutý už v 0.23.0 soubor doplní při startu doplňku.
- Změna platí po restartu Home Assistantu: v dlaždici **Chybová hlášení** je
  tlačítko **Restartovat Home Assistant** (jen po čisté kontrole konfigurace)
  a **Zkontrolovat znovu**; doplněk po restartu ověří, že události chodí.
  HA se sám nerestartuje nikdy.
- Vlastní `system_log:` s `fire_event`, `!include` nebo konfigurace bez
  `packages/`: doplněk nic nezapíše a panel ukáže, co doplnit. Konflikt
  nahlášený v kontrole konfigurace HA soubor hned smaže.
- Recorder ukládá `system_log_event` do historie; vyloučit je jde
  `recorder: exclude: event_types: [system_log_event]` (popsáno v dokumentaci).

## 0.23.0

- Přepínač **Posílat chybová hlášení Homecraftu** na Přehledu (ve výchozím
  stavu vypnutý): se souhlasem doplněk posílá Homecraftu chyby automatizací
  a skriptů Homecraftu (`system_log_event` úrovně ERROR a CRITICAL) a chyby
  z Node-RED (`homecraft_error`), aby šlo zjistit příčinu a automatizaci
  opravit. Jen povolená pole z pevného seznamu v doplňku, stejná chyba nejvýš
  jednou za 10 minut s počtem výskytů. Vlastní automatizace zákazníka se
  neposílají nikdy: rozhoduje ID z konfigurace, ne ID entity. Bez spojení
  nebo při pauze se hlášení zahodí. V auditu jeden záznam `errors.report`
  na odeslanou zprávu, jen s počtem a kódy.

## 0.22.0

- Nová záložka **Features**: co Homecraft nainstaloval do Home Assistantu,
  jen pro čtení. Každá feature má dlaždici s verzí (a dostupnou novější),
  instancemi, časem posledního Apply, stavem (**Běží**, **Aktualizace**,
  **Chyba doplňku**), soubory (`packages/hfb_*.yaml`) a síťovými zařízeními,
  která používá. Odkaz **Otevřít projekt v Homecraftu** vede do projektu.
- Souhrn posílá Homecraft po každém Apply a po připojení novou operací
  `features.report` (i během pauzy: týká se jen panelu, ne Home Assistantu);
  doplněk ho uloží do `/data/features.json` a panel ho čte při každém
  načtení, takže se změna ukáže bez restartu. V auditu **Features (počet)**.
- Přehled má dlaždici **Features** (počet a počet dostupných aktualizací),
  sloupec **Používá** v záložce **Síť** ukazuje features podle souhrnu.

## 0.21.0

- Jednorázový přesun síťových zařízení z Homecraftu: nová operace
  `network_devices.import` sloučí seznam z Homecraftu do záložky **Síť**.
  Shoda podle IP adresy nebo MAC: uložené zařízení vyhrává, doplní se jen
  jeho prázdná pole. Nová zařízení mají zdroj **manual**, neplatná se
  zahodí. Opakovaný import nic nezmění. V auditu **Síť (import z Homecraftu)**.

## 0.20.0

- Homecraft čte seznam síťových zařízení ze záložky **Síť** novou operací
  `network_devices.get` (jen čtení). Feature, která adresu potřebuje, se
  posílá s požadavkem a v auditu je vidět jako **Síť (feature …)**. Homecraft
  seznam nemůže změnit; spravuje se jen v panelu doplňku.
- Odpověď nese i slug doplňku, aby Homecraft mohl odkázat na tento panel
  („Spravovat v Home Assistantu“).

## 0.19.0

- Záložka **Síť** má tlačítko **Doporučený sken**: doplněk sám proskenuje
  domácí síť podle zaškrtnutých profilů – **Shelly** (port 80, `GET /shelly`:
  model, generace, MAC, FW; jméno v síti `<id>.local`), **Kamery** (RTSP 554
  `OPTIONS`, ONVIF `GetDeviceInformation` na 80/8000 bez přihlášení, jinak
  „Kamera, výrobce neznámý“), **NAS** (Synology 5000/5001, SMB 445) a
  **Ostatní web** (80/443, ve výchozím stavu vypnuto). Výsledky jsou v dialogu
  se zaškrtávátky jako u **Načíst z HA**; přidaná mají zdroj **Nové · jen sken**.
- Podsíť bere z Supervisoru (`/network/info`, IPv4 rozhraní hostitele); jen
  soukromé rozsahy (10/8, 172.16/12, 192.168/16) nejvýš /22, jinak sken
  odmítne. Nejvýš 64 spojení najednou, každé 500 ms; doplněk zůstává v síti
  hassio (bez `host_network`).
- Už uložená zařízení (shoda IP, MAC nebo jména `.local`) se znovu nepřidají;
  sken jim jen doplní chybějící jméno v síti (**Doplněno .local**).
- Sken spouští jen člověk z panelu (`POST /api/network-devices/scan` s tajnou
  hodnotou panelu), Homecraft na něj operaci nemá. Nic se neposílá ven,
  hesla se nezadávají.

## 0.18.0

- Záložka **Síť** má tlačítko **Načíst z HA**: doplněk přečte registr
  zařízení HA (`configuration_url`, MAC z `connections`) a objevování v síti
  (`dhcp`/`zeroconf`/`ssdp` `*/subscribe_discovery`) a nabídne kandidáty v
  dialogu se zaškrtávátky, stejně jako dnes Homecraft. Už uložená zařízení
  (shoda IP nebo MAC) jsou označená **Už v seznamu** a znovu se nepřidají;
  přidaná mají zdroj **Z HA**. Jen čtení, nový endpoint panelu
  `GET /api/network-devices/ha`; URL s přihlašovacími údaji se vynechají.

## 0.17.0

- Panel má záložku **Síť**: seznam síťových zařízení domácnosti (název, typ,
  adresa, jméno v síti `*.local`, MAC, webové rozhraní, poznámka, zdroj) s
  filtry podle typu, přidáním, úpravou a smazáním. Seznam zůstává v doplňku
  (`/data/network_devices.json`, přežije restart), API panelu
  `GET/PUT /api/network-devices`. Validace stejná jako v Homecraftu (délky,
  URL jen http/https/rtsp/rtsps) a navíc odmítne URL s přihlašovacími údaji
  (`jméno:heslo@`). Sloupec **Používá** zatím prázdný (vyplní záložka Features).
- Dlaždice **Síťová zařízení** na Přehledu: počet podle typu a počet nových ze
  skenu (zatím 0, sken přijde později).

## 0.16.0

- Homecraft smí obsluhovat doplněk Tailscale (feature Tailscale, vzdálený
  přístup k HA bez veřejné adresy): přečíst jeho info, nainstalovat, spustit
  a restartovat ho a změnit jeho volby. Změnit smí jen `advertise_routes`
  (jen soukromé IPv4 sítě: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) a
  `share_homeassistant` (`serve` / `disabled`, nikdy `funnel`); doplněk to
  před každým zápisem porovná s aktuálními volbami ze Supervisoru a cokoli
  jiného (nebo nepovedené čtení) odmítne.
- Balíček s `http:` projde jen s přesným obsahem `use_x_forwarded_for: true`
  a `trusted_proxies: [127.0.0.1]`, aby HA věřil proxy doplňku Tailscale na
  localhostu. Jakékoli jiné `http:` zůstává zakázané.
- Ke čtení nově příkaz WebSocketu `network` (podsíť z adaptérů HA pro
  `advertise_routes`) a `GET /host/info` Supervisoru (název stroje v tailnetu).

## 0.15.0

- Panel má na Přehledu sekci **Aktualizace**: zastaralé karty Homecraftu, které
  Homecraft v HA spravuje, s tlačítkem **Aktualizovat**. Kartu nainstaluje
  Homecraft (podepsaný soubor jako při Apply), bez Plan/Apply v Homecraftu.
  Seznam posílá Homecraft (`updates.set`), doplněk ho drží jen v paměti.
  Tlačítko pošle Homecraftu `update.run` (audit `update.run`); doplněk sám nic
  nestahuje ani nezapisuje. Pozastavený nebo odpojený doplněk nic nepošle.

## 0.14.0

- Integrace Scheduler component a Browser Mod jdou na HA 2026.11+ nainstalovat
  přes Marketplace (vestavěný nástupce HACS): doplněk pouští
  `marketplace/repositories/list`, `marketplace/warning/accept` a
  `marketplace/repository/install` jen pro připnuté repozitáře a verze jako
  u HACS, nikdy s náhradou vestavěné integrace. Nová integrace se načte bez
  restartu Home Assistantu.

## 0.13.0

- Homecraft může nechat HACS stáhnout Scheduler component (v3.3.9) a Browser
  Mod (v3.4.2) v připnutých verzích (tag vydání) a založit jejich integraci.
  Repozitáře i tagy jsou pevně v kódu doplňku, kód stahuje HACS z GitHubu.
- Apply nově zakládá i integraci System Monitor (observability).

## 0.12.4

- Start doplňku už v HA nevytváří notifikaci o nepovedeném přihlášení.
  Kontrola při startu ověřovala dosažitelnost HA požadavkem bez tokenu
  (HTTP 401), teď používá `/manifest.json`, který přihlášení nevyžaduje.

## 0.12.3

- Ingress a kontrola karet fungují i s HA na jiném portu než 8123 (`http:`
  `server_port`, např. 80): port HA zjistí doplněk při startu od Supervisoru.
  Dřív plán hlásil `ha_unreachable ClientConnectorError`.

## 0.12.2

- Instalace doplňku přes Použít může trvat až 15 minut (dřív Connector
  utnul volání Supervisoru po 10 minutách a Použít selhalo s `TimeoutError`).

## 0.12.1

- Repozitář i obrazy doplňku jsou v organizaci `homecraftcz`
  (`ghcr.io/homecraftcz/homecraft-connector-*`). Stávající instalace s URL
  `eMalinos/homecraft-addon` fungují dál přes přesměrování, nic neměň.

## 0.12.0

- Homecraft si po instalaci karty ověří, že ji HA servíruje (jen `HEAD` na
  `/local/homecraft/<id>.js?v=…`), a zjistí, zda jsou zdroje Lovelace v režimu
  YAML (`lovelace/info`).

## 0.11.0

- Karty Homecraftu (`www/homecraft/<id>.js`) doplněk zapíše jen s platným
  podpisem Homecraftu; kompromitovaný server Homecraftu tak do HA nepodstrčí
  vlastní JavaScript.

## 0.10.0

- Homecraft smí přes Použít nainstalovat a aktualizovat své karty (soubory
  `www/homecraft/<id>.js`, zdroje Lovelace `/local/homecraft/…`) a posílat
  oznámení o nových verzích karet (jen s ID `homecraft_…`). Nic jiného ve
  `www/` ani ve zdrojích Lovelace nesmí; smazat smí kterýkoli zdroj Lovelace
  (převzetí karty z HACS).
- Zápis souboru nad 9 MiB se odmítne (`too_large`), stejně jako čtení.

## 0.9.0

- Když v `configuration.yaml` chybí načítání balíčků, doplní ho Použít přes
  doplněk: doplněk sám přidá do bloku `homeassistant:` řádek
  `packages: !include_dir_named packages`, nechá Home Assistant zkontrolovat
  konfiguraci a při chybě vrátí původní soubor. Homecraft do
  `configuration.yaml` nic jiného zapsat nemůže (žádný volný zápis), soubor
  zůstává pro Homecraft jen ke čtení.

## 0.8.1

- Homecraft smí přes Použít založit i integrace z knihovny: **Speedtest.net**,
  **Ping**, **Holiday** a **SQL** (velikost databáze recorderu). Kroky průvodce
  smí poslat jen pole a hodnoty, které knihovna používá: SQL jen s dotazem na
  velikost databáze recorderu (bez `db_url`), Ping jen na `1.1.1.1`.
- U nové integrace smí Homecraft vypnout automatické dotazování
  (`pref_disable_polling`), jinak nic na záznamu integrace nemění.

## 0.8.0

- Panel má záložky **Přehled · Zabezpečení · Audit**. Přehled ukazuje dlaždice
  se stavem spojení, tokenů a počtem operací za 24 h (s počtem odmítnutých);
  přepínač pozastavení je přímo na dlaždici. Spojení, spárování a tokeny jsou
  v záložce Zabezpečení, tabulka operací v záložce Audit.
- Na telefonu jsou dlaždice ve dvou sloupcích a záložky se dají posouvat.

## 0.7.0

- Spárování bez kopírování kódu: nespárovaný doplněk se Homecraftu sám ohlásí
  jako žádost o spárování a panel nahoře ukáže krátký **ověřovací kód**
  (`XXX-XXX`, i v logu). Operátor ho v Homecraftu porovná a žádost schválí.
  Dlouhý párovací kód zůstává jako záložní cesta.
- Úvodní zpráva navíc nese veřejný klíč, název Home Assistantu a otisky
  hostitele a instalace HA (jen 16 znaků SHA-256, nikdy hodnota sama).
- Na „čeká na schválení“ (kód 4403) panel ukáže odznak a doplněk to zkouší
  znovu po 10 s, pak každých 30 s a po 15 minutách každou minutu.
- Nový spouštěcí skript `run.sh` jednou při startu přečte ID instalace HA
  (`.storage/core.uuid`) a předá ho agentovi; bez souboru ho načte až restart.
  Agent sám `.storage/` dál nečte (podprofil `connector` ho odepírá i v režimu
  `complain`).
- Panel se ukáže hned po startu; název HA a otisk hostitele doplněk dohledá
  při připojení (každý nejvýš 5 s) a co chybí, zkouší znovu, dokud není
  spárovaný.

## 0.6.1

- Dotazy na Supervisor (`supervisor/api`: stav, instalace a spuštění doplňků)
  jdou z doplňku rovnou na Supervisor. Supervisor je doplňkům přes WebSocket
  jádra HA nepouští (`unauthorized`). Homecraft dostane odpověď ve stejném
  tvaru jako dřív.
- Ingress do doplňků (Node-RED, Grafana, InfluxDB, go2rtc): relaci ingressu
  vytvoří Supervisor jen jádru HA a jádro jen správci. Doplněk proto o ni žádá
  jádro jako správce, s **tokenem správce Home Assistantu** z panelu (nová
  sekce, jen zadat a smazat, `/data/ha_token`). Na nic jiného ho nepoužije
  a Homecraftu ho nepošle. Bez tokenu skončí ingress chybou `no_ha_token`.

## 0.6.0

- Vlastní AppArmor profil (`apparmor.txt`): jádro HA OS pustí agenta jen
  k Pythonu, `/data`, `configuration.yaml` (čtení) a složkám `packages/`,
  `custom_templates/` a `themes/`. Shell ani jiný program spustit nesmí,
  `secrets.yaml` a `.storage/` nepřečte. Hodnocení zabezpečení doplňku +1.
- Zatím v režimu `complain` (porušení se jen zapisují do logu); po vyladění
  na živém HA OS ho další verze přepne do vynucování.

## 0.5.1

- `server_url` smí být `ws://` na privátní IP v LAN (vývojový Homecraft bez
  Cloudflare, jen pro test). Doplněk při startu varuje, že spojení i přístupový
  token jdou nešifrovaně. Názvy hostitelů, loopback a veřejné adresy dál jen
  `wss://`.

## 0.5.0

- Homecraft může doplňku poslat nový přístupový token (`access_token.set`,
  rotace tokenu Cloudflare bez obíhání domů). Token se ověří stejně jako
  v panelu a uloží jako čekající. Doplněk se s ním připojí znovu, až nic
  neběží (nejpozději za 15 minut), takže nepřeruší rozběhnutý požadavek.
  Pustí-li ho Cloudflare, platí. Odmítne-li ho, doplněk se v tomtéž pokusu
  připojí s původním a čekající token zkusí příště znovu.
- `hello` posílá `access_token_fp` (prvních 16 znaků SHA-256 celého tokenu,
  nikdy token), aby Homecraft poznal i výměnu samotného secretu.
- Spojení s Homecraftem bez cookies (cookie od Access nenahradí token).
- Panel ukazuje, kdy token aktualizoval Homecraft, a čekající token. Token se
  dál nikde nezobrazuje, nepíše do logu ani do auditu (jen operace a výsledek).

## 0.4.0

- Kontrola obsahu: doplněk odmítne zápis, který by v Home Assistantu spouštěl
  příkazy nebo posílal data ven (chyba `refused_content`, záznam v auditu a logu).
  - Soubory v `packages/`: zakázané integrace (`shell_command`, `command_line`,
    `rest`, `python_script`, `frontend`, `lovelace`…), platformy
    `command_line`/`rest`/`scrape` a akce `shell_command.*`, `hassio.*`,
    `homeassistant.restart` a další. Žádné tagy HA (`!include`, `!secret`,
    `!env_var`), ani v `themes/`. `secrets.yaml` zapsat nelze.
  - Stejná kontrola akcí pro automatizace a skripty přes REST a pro akce karet
    v dashboardech, vždy proti živé konfiguraci: obsah uživatele Homecraft
    neblokuje.
  - Node-RED: nové a změněné uzly a vše, kam vedou, jen typů, které používá
    knihovna (nikdy `exec`). Volání služeb pevně v uzlu, flow uživatele beze
    změny.
  - go2rtc: Homecraft smí měnit jen klíč `webrtc` ve tvaru knihovny, ostatní
    klíče (streamy s `exec:`, `echo:`, `expr:`, `api`…) zůstávají přesně
    uživatele.

## 0.3.0

- Panel doplňku v postranním menu HA (ingress): stav spojení, párovací kód
  s tlačítkem Kopírovat, tlačítko **Připojit znovu** (bez čekání na další pokus).
- Přístupový token Homecraftu se zadává v panelu. Jde jen uložit, změnit nebo
  smazat, zpátky ho panel nikdy neukáže.
- Přepínač **Pozastavit Homecraft**: každý požadavek pak skončí chybou `paused`.
  Pauza hned zavře otevřená sezení do HA a přežije restart; volba `paused`
  ji při startu zapne.
- Audit posledních 200 operací (čas, operace, cíl, výsledek, trvání) bez obsahu
  souborů a tokenů; `/data/audit.jsonl` drží posledních 1000.

## 0.2.0

- Provádí operace z Homecraftu: WebSocket sezení do Home Assistantu (`ws.open`,
  `ws.send`, `ws.close`), REST a ingress doplňků (`http`) a soubory
  (`file.read`, `file.write`, `file.delete`, `file.list`).
- Vše jen podle seznamu povolených operací v kódu doplňku; ostatní odmítne
  s chybou `not_allowed`.

## 0.1.0

- První verze: identita doplňku, párovací kód, připojení k Homecraftu
  (výzva a podpis), udržování spojení a opětovné připojení, self-check při startu.
  Požadavky z Homecraftu zatím odmítá (`not_implemented`).
