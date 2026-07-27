# Unraid-Pilot mit Paperless-ngx v3

Diese Anleitung verbindet den Proxy mit dem **Paperless-ngx-Container** über ein
separates benutzerdefiniertes Docker-Bridge-Netz. Der Proxy veröffentlicht
keinen Host-/LAN-Port. Paperless-ngx erreicht ihn per Docker-DNS unter
`http://chatgpt-proxy:8080/v1`.

> Der Proxy verwendet ein ChatGPT-/Codex-OAuth-Token und einen undokumentierten
> Backend-Endpunkt. Das eigene OpenAI-Konto, Planlimits und mögliche Änderungen
> der Nutzungsbedingungen bleiben das Betriebsrisiko. Für den ersten Versuch
> nur ein nicht sensibles, synthetisches Testdokument verwenden.

## 0. Bisherige Paperless-Werte notieren

In Paperless-ngx unter **Einstellungen → Anwendungskonfiguration**, Kategorie
**AI**, die bisherigen Werte für diese Felder notieren:

- `AI Enabled`
- `LLM Backend`
- `LLM Model`
- `LLM API Key`
- `LLM Endpoint`
- `LLM Output Language`
- `LLM Request Timeout`

Diese Datenbankwerte haben Vorrang vor gleichnamigen Container-Variablen und
werden für den Rollback benötigt.

## 1. Paperless-ngx-Container identifizieren

Im Unraid-Terminal:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}' \
  | grep -i 'paperless-ngx'
```

Den Namen aus der ersten Spalte übernehmen. Im Beispiel heißt er
`paperless-ngx`:

```bash
PAPERLESS_CONTAINER=paperless-ngx
PAPERLESS_IMAGE="$(docker inspect "$PAPERLESS_CONTAINER" --format '{{.Config.Image}}')"

case "$PAPERLESS_IMAGE" in
  paperlessngx/paperless-ngx:3.0.3|ghcr.io/paperless-ngx/paperless-ngx:3.0.3)
    printf 'Paperless image verified: %s\n' "$PAPERLESS_IMAGE"
    ;;
  *)
    printf 'STOP: expected Paperless-ngx 3.0.3, found %s\n' "$PAPERLESS_IMAGE" >&2
    exit 1
    ;;
esac

docker inspect "$PAPERLESS_CONTAINER" \
  --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}'
```

Wenn der Container anders heißt, in den folgenden Befehlen den Wert anpassen.
Bei einem anderen Image-Tag zuerst das Unraid-Template bewusst auf Version
`3.0.3` pinnen oder die Anleitung gegen die tatsächlich eingesetzte Version
neu prüfen; nicht einfach über den `STOP` hinweggehen.

## 2. Internen LLM-Endpunkt in Paperless explizit erlauben

Paperless-ngx v3.0.3 erlaubt interne Endpoints standardmäßig. Für ein eindeutiges
und updatesicher nachvollziehbares Deployment trotzdem in Unraid setzen:

1. **Docker → Paperless-ngx → Edit** öffnen.
2. **Add another Path, Port, Variable, Label or Device** wählen.
3. Typ `Variable`:
   - Name: `Allow internal AI endpoints`
   - Key: `PAPERLESS_AI_LLM_ALLOW_INTERNAL_ENDPOINTS`
   - Value: `true`
4. **Apply**.

Unraid erstellt den Paperless-Container dabei neu. Erst danach das gemeinsame
Netz verbinden.

## 3. Separates gemeinsames Docker-Netz anlegen

Das bestehende Netzwerk des Paperless-Containers bleibt unangetastet. Für den
Pilot kommt nur ein zweites Netz hinzu:

```bash
PAPERLESS_CONTAINER=paperless-ngx

docker network inspect paperless-ai-internal >/dev/null 2>&1 \
  || docker network create --driver bridge paperless-ai-internal

docker network connect paperless-ai-internal "$PAPERLESS_CONTAINER" 2>/dev/null \
  || true
```

Prüfen:

```bash
docker network inspect paperless-ai-internal \
  --format '{{range .Containers}}{{println .Name}}{{end}}'
```

Jetzt muss der Paperless-ngx-Container in der Ausgabe stehen.

**Unraid-Hinweis:** Eine manuell hinzugefügte zweite Netzwerkverbindung kann bei
einem Update/Recreate des Paperless-Containers verloren gehen. Für den Pilot
reicht das. Falls Paperless den Proxy später nicht mehr findet, den
`docker network connect`-Befehl erneut ausführen.

## 4. Quellcode nach Appdata klonen

Nach der Veröffentlichung des Repositories funktioniert das anonym:

```bash
mkdir -p /mnt/user/appdata/paperless-chatgpt-proxy
cd /mnt/user/appdata/paperless-chatgpt-proxy

git clone https://github.com/geminiaipro865-star/paperless-ai-proxy.git source
```

Bei einer bereits vorhandenen Installation stattdessen:

```bash
cd /mnt/user/appdata/paperless-chatgpt-proxy/source
git pull --ff-only origin main
```

## 5. Secret und persistentes Tokenverzeichnis anlegen

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
PROXY_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

umask 077
cat > "$APPDIR/.env" <<EOF
PAPERLESS_NETWORK=paperless-ai-internal
APPDATA_PATH=$APPDIR
PROXY_API_KEY=$PROXY_API_KEY
CHATGPT_MODEL=gpt-5.6-luna
CHATGPT_REASONING_EFFORT=low
CHATGPT_REQUEST_TIMEOUT=300
EOF

mkdir -p "$APPDIR/data"
chown -R 10001:999 "$APPDIR/data"
chmod 700 "$APPDIR/data"
unset PROXY_API_KEY
```

Das Proxy-Secret liegt nur in
`/mnt/user/appdata/paperless-chatgpt-proxy/.env`. Diese Datei nicht posten und
nicht ins Repository kopieren.

## 6. Proxy bauen und starten

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
cd "$APPDIR/source"

docker compose \
  --env-file "$APPDIR/.env" \
  -f deploy/unraid/compose.yml \
  up -d --build
```

Status prüfen:

```bash
docker ps --filter name=chatgpt-proxy
docker logs --tail 100 chatgpt-proxy
docker inspect chatgpt-proxy --format '{{.State.Health.Status}}'
```

Erwartet: Der Container läuft, der Health-Status wird nach kurzer Zeit
`healthy`, und unter `docker ps` ist **kein** Host-Port veröffentlicht.

Netz prüfen:

```bash
docker network inspect paperless-ai-internal \
  --format '{{range .Containers}}{{println .Name}}{{end}}'
```

Jetzt müssen der Paperless-ngx-Container und `chatgpt-proxy` erscheinen.

## 7. Separaten ChatGPT-Login durchführen

Nicht das Token eines gleichzeitig laufenden Codex CLI importieren; rotierende
Refresh-Tokens würden sich gegenseitig ungültig machen.

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
cd "$APPDIR/source"

docker compose \
  --env-file "$APPDIR/.env" \
  -f deploy/unraid/compose.yml \
  exec chatgpt-proxy python -m chatgpt_proxy login
```

Die angezeigte URL auf iPad/PC öffnen, den Einmalcode eingeben und den Login
abschließen. Danach:

```bash
docker compose \
  --env-file "$APPDIR/.env" \
  -f deploy/unraid/compose.yml \
  exec chatgpt-proxy python -m chatgpt_proxy status
```

Die Token-Datei liegt danach im geschützten `data`-Verzeichnis.

## 8. Verbindung aus dem Paperless-Container testen

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
set -a
. "$APPDIR/.env"
set +a

PAPERLESS_CONTAINER=paperless-ngx

docker exec \
  -e PROXY_TEST_KEY="$PROXY_API_KEY" \
  "$PAPERLESS_CONTAINER" \
  python -c 'import json,os,urllib.request; r=urllib.request.Request("http://chatgpt-proxy:8080/v1/models",headers={"Authorization":"Bearer "+os.environ["PROXY_TEST_KEY"]}); print(json.dumps(json.load(urllib.request.urlopen(r,timeout=30)),indent=2))'

unset PROXY_API_KEY
```

Wenn der Containername abweicht, `PAPERLESS_CONTAINER` ändern. Erwartet ist ein
JSON-Objekt mit `"object": "list"` und einer Modellliste. Falls
`gpt-5.6-luna` dort nicht erscheint, ein tatsächlich zurückgegebenes Modell
verwenden.

## 9. Paperless-ngx konfigurieren

In Paperless-ngx unter **Einstellungen → Anwendungskonfiguration**, Kategorie
**AI**, diese Werte speichern:

| Feld | Wert |
| --- | --- |
| `AI Enabled` | aktiviert |
| `LLM Backend` | `OpenAI-compatible` / `openai-like` |
| `LLM Endpoint` | `http://chatgpt-proxy:8080/v1` |
| `LLM API Key` | Wert `PROXY_API_KEY` aus `$APPDIR/.env` |
| `LLM Model` | `gpt-5.6-luna` |
| `LLM Output Language` | `German` |
| `LLM Request Timeout` | `300` |

Wichtig:

- Bereits in der UI gespeicherte Werte überschreiben Container-Environment.
- Die von Paperless gesendete `temperature` wird vom Proxy entfernt.
- `LLM Embedding Backend` nicht auf diesen Proxy stellen. Für RAG lokal
  `huggingface` verwenden; für den ersten Vorschlags-Test die bisherige
  Embedding-Konfiguration unverändert lassen.
- Ein Container-Neustart ist nach dem Speichern normalerweise nicht nötig.

Danach zunächst mit einem synthetischen Testdokument eine AI-Vorschlagsanalyse
auslösen und parallel beobachten:

```bash
docker logs -f --tail 100 chatgpt-proxy
```

Erfolg bedeutet: kein `temperature`-Fehler, HTTP 200 und Vorschläge in
Paperless-ngx. `429` bedeutet Planlimit; `401 chatgpt_login_required` erfordert
einen neuen Device-Code-Login.

## 10. Update

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
cd "$APPDIR/source"
git pull --ff-only origin main

docker compose \
  --env-file "$APPDIR/.env" \
  -f deploy/unraid/compose.yml \
  up -d --build
```

Danach Netzwerk und Health erneut prüfen. Wurde Paperless-ngx ebenfalls neu
erstellt, seine zweite Netzverbindung wiederherstellen:

```bash
PAPERLESS_CONTAINER=paperless-ngx
docker network connect paperless-ai-internal "$PAPERLESS_CONTAINER" 2>/dev/null \
  || true
```

## 11. Rollback

1. In Paperless-ngx die zuvor notierten AI-/LLM-Werte wiederherstellen.
2. Optional vor dem Stoppen die lokal gespeicherten OAuth-Tokens löschen:

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
cd "$APPDIR/source"

docker compose \
  --env-file "$APPDIR/.env" \
  -f deploy/unraid/compose.yml \
  exec chatgpt-proxy \
  python -m chatgpt_proxy logout
```

Ohne diesen optionalen Schritt bleibt das Tokenverzeichnis für einen späteren
Versuch erhalten.

3. Proxy stoppen und entfernen:

```bash
APPDIR=/mnt/user/appdata/paperless-chatgpt-proxy
cd "$APPDIR/source"

docker compose \
  --env-file "$APPDIR/.env" \
  -f deploy/unraid/compose.yml \
  down
```

4. Optional die zusätzliche Netzwerkverbindung entfernen:

```bash
PAPERLESS_CONTAINER=paperless-ngx
docker network disconnect paperless-ai-internal "$PAPERLESS_CONTAINER"
```

Für eine vollständige Entfernung anschließend
`/mnt/user/appdata/paperless-chatgpt-proxy` bewusst löschen. Das entfernt auch
Checkout, Konfiguration und persistierte Tokens.
