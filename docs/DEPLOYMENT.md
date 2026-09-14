# Deploy

> Este documento es público: usa placeholders. **Nunca** escribir acá IPs (LAN o tailnet), hostnames, usuarios, claves ni tokens reales.

## Resumen

| Evento | Pipeline | Imagen | Destino |
|--------|----------|--------|---------|
| Pull request | `ci.yml` | build arm64 sin push | — |
| Push a `feature/**` | `delivery.yml` | `ghcr.io/emanuelturtula/trading-bot:vX.Y.Z-beta.<sha7>` | contenedor `trading-bot-beta`, puerto **8082** |
| Push a `main` (merge) | `delivery.yml` | `...:sha-<sha>`; tras deploy sano: `:vX.Y.Z` y `:latest` | contenedor `trading-bot-prod`, puerto **8081**, tag git + GitHub Release |

Flujo de `delivery.yml`: `ci.yml` (gitleaks → lint/mypy → tests) → `next_version.py` → build arm64 nativo (`ubuntu-24.04-arm`) → push a GHCR → `remote-deploy.yml` → release (solo main).

`remote-deploy.yml` une el runner al tailnet con Tailscale OIDC (nodo efímero con `tag:trading-bot-ci`), y `scripts/remote_deploy.py`:

1. valida todos los inputs y que el run sea un push de este repo (`main` → prod, `feature/**` → beta);
2. copia `deploy/deploy.py` y `deploy/compose.yml` a un directorio temporal de la Pi;
3. hace `docker login ghcr.io` con el token efímero del job **por stdin**;
4. ejecuta `deploy.py` y borra el directorio temporal.

`deploy/deploy.py` (en la Pi):

- toma un lock de host (beta y prod nunca deployan en paralelo);
- rechaza runs más viejos que el deployado (un rerun viejo no pisa uno nuevo);
- hace `docker pull` por digest y verifica los labels OCI `revision` y `version`;
- hace backup de la base SQLite del contenedor actual;
- `docker compose up --wait` y verifica que corra el digest exacto con healthcheck `healthy`;
- si falla, restaura el deploy anterior; si no había, baja el contenedor y conserva el volumen;
- escribe `current.json` solo si el deploy quedó sano y conserva los últimos 10 intentos.

## Layout en la Raspberry

```
~/trading-bot-deploy/                 (chmod 700)
  deploy.lock
  beta/  secrets.env (600)  current.json  attempts/<id>/{compose.yml,request.json,previous.json,database.sqlite3,result.json}
  prod/  secrets.env (600)  current.json  attempts/...
```

Proyectos compose: `trading-bot-beta` (8082 → 8000) y `trading-bot-prod` (8081 → 8000), cada uno con su volumen `data`.

## Setup inicial (una sola vez)

Requisitos en la Pi: Docker con Compose v2, `python3` y la Pi unida al tailnet.

### 1. Tailscale

1. En la policy del tailnet, **agregar** (sin reemplazar la policy existente) el tag `tag:trading-bot-ci` con un tag owner adecuado y acceso **solo** al puerto TCP 22 de la Raspberry.
2. Crear una credencial **OpenID Connect trust** con issuer GitHub y subject:
   `repo:emanuelturtula@<OWNER_ID>/trading-bot@<REPO_ID>:ref:refs/heads/*`
   Los IDs se obtienen con `gh api repos/emanuelturtula/trading-bot --jq '.owner.id, .id'`. Scope: solo *Auth Keys write* con `tag:trading-bot-ci`. No ampliar el subject a otros repos ni a pull requests.
3. Guardar el client ID y el audience como secrets del repo: `TS_OAUTH_CLIENT_ID` y `TS_AUDIENCE`.

### 2. Clave SSH de deploy

1. Generar una clave Ed25519 **dedicada** (no reutilizar tu clave personal ni la de GitHub).
2. Agregar la mitad pública al `authorized_keys` del usuario de deploy en la Pi.
3. Guardar la privada como secret `DEPLOY_SSH_KEY`.
4. Armar la entrada `known_hosts` de la Pi **para su hostname o IP del tailnet** desde una sesión SSH de confianza (no con un `ssh-keyscan` sin verificar) y guardarla como `DEPLOY_KNOWN_HOSTS`.

### 3. Secrets y variables de GitHub

| Nombre | Tipo | Contenido |
|--------|------|-----------|
| `TS_OAUTH_CLIENT_ID` | secret | Client ID de la credencial OIDC de Tailscale |
| `TS_AUDIENCE` | secret | Audience de la credencial OIDC |
| `DEPLOY_HOST` | secret | Hostname o IP del tailnet de la Pi (es secret para que se enmascare en los logs públicos) |
| `DEPLOY_USER` | secret | Usuario de deploy en la Pi |
| `DEPLOY_SSH_KEY` | secret | Clave privada de deploy |
| `DEPLOY_KNOWN_HOSTS` | secret | Entrada known_hosts de la Pi |
| `DEPLOY_ENABLED` | **variable** | `true` cuando todo lo anterior está listo |

Cargar los valores con `gh secret set <NOMBRE>` (pide el valor por stdin, así no queda en el historial del shell). Mientras `DEPLOY_ENABLED` no sea `true`, el pipeline buildea la imagen y reporta "deployment NOT performed".

### 4. Secretos de la aplicación (en la Pi)

Crear un bot distinto por entorno con @BotFather. En la Pi:

```sh
mkdir -p ~/trading-bot-deploy/beta ~/trading-bot-deploy/prod
chmod 700 ~/trading-bot-deploy
umask 077
cat > ~/trading-bot-deploy/beta/secrets.env <<'EOF'
TB_TELEGRAM_BOT_TOKEN=<token del bot beta>
TB_TELEGRAM_ALLOWED_CHAT_IDS=<chat id>
TB_DASHBOARD_PASSWORD_HASH=<hash>
TB_SESSION_SECRET=<valor aleatorio largo>
EOF
chmod 600 ~/trading-bot-deploy/beta/secrets.env
```

Repetir para `prod/secrets.env` con el token del bot de producción. `deploy.py` nunca lee este archivo; solo lo crea vacío si falta y se niega a deployar si tiene permisos más abiertos que 600. Los cambios aplican al reiniciar: `docker compose --project-name trading-bot-beta restart app` (o `trading-bot-prod`).

### 5. Protección del repositorio

- Secret scanning y push protection: **activos** (verificado).
- Ruleset para `main`: PR obligatorio, checks requeridos (`Secrets scan`, `Lint & types`, `Tests`), sin force push ni borrado.
- Actions: permisos por defecto del `GITHUB_TOKEN` en solo lectura y aprobación requerida para workflows de forks.

### 6. Setup local (cada clon)

```sh
winget install Gitleaks.Gitleaks      # o: brew install gitleaks
pip install uv                        # o el instalador oficial de uv
uv sync
uv run pre-commit install
```

## Operación

- Estado: `curl http://<DEPLOY_HOST>:8082/health` (beta) y `:8081/health` (prod) desde la LAN o el tailnet.
- Logs: `docker compose --project-name trading-bot-prod logs -f app`.
- Último deploy: `~/trading-bot-deploy/prod/current.json`; historial en `attempts/`.
- Rollback manual: re-ejecutar el workflow del commit anterior no está permitido (protección de orden de runs). Revertir con un PR (`git revert`) y mergear.
- Restaurar la base: el backup previo a cada deploy queda en `attempts/<id>/database.sqlite3`.
