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

> **Atención (PowerShell):** no cargar secrets con el prompt interactivo de `gh secret set <NOMBRE>` (sin `--body`). En PowerShell ese prompt puede guardar un valor **vacío** sin mostrar ningún error, y el problema recién aparece cuando falla el deploy.

Formas verificadas en PowerShell:

```powershell
# Valor no sensible (queda escrito en la línea de comandos y en el historial)
gh secret set <NOMBRE> --repo emanuelturtula/trading-bot --body "<valor>"

# Valor pegado a la vista, sin escribirlo en la línea de comandos
$v = Read-Host "<NOMBRE>"
gh secret set <NOMBRE> --repo emanuelturtula/trading-bot --body $v
Remove-Variable v

# Archivo: PowerShell no tiene redirección de entrada `<`; se quitan los CR de Windows
gh secret set DEPLOY_SSH_KEY --repo emanuelturtula/trading-bot --body ((Get-Content "$HOME\.ssh\<clave>" -Raw) -replace "`r", "")
```

`DEPLOY_KNOWN_HOSTS` se carga igual que la clave, con la ruta de su `<archivo>`. En bash/zsh, donde `<` sí existe, alcanza con `gh secret set <NOMBRE> --repo emanuelturtula/trading-bot < <archivo>`.

Síntomas de un secret vacío en el job de deploy (`Deploy beta (8082) / Deploy beta` o `Deploy production (8081) / Deploy prod`):

- el step `Join tailnet (OIDC, ephemeral)` falla con `Please provide either an auth key, OAuth secret and tags, or federated identity client ID and audience with tags.` (`TS_OAUTH_CLIENT_ID` o `TS_AUDIENCE` vacíos);
- el step `Deploy and verify health` falla con `ValueError: Invalid host` (o `Invalid user`) de `scripts/remote_deploy.py` (`DEPLOY_HOST` o `DEPLOY_USER` vacíos);
- `DEPLOY_SSH_KEY` o `DEPLOY_KNOWN_HOSTS` vacíos o con CR de Windows terminan en errores de `ssh` (`Permission denied (publickey)`, `Host key verification failed`);
- en el log, los inputs y variables de entorno del step muestran el valor **en blanco** en lugar de `***` (GitHub solo enmascara secrets con contenido).

Remedio: recargar el secret con una de las formas de arriba y re-ejecutar el job fallido (*Re-run failed jobs* o `gh run rerun <run-id> --failed --repo emanuelturtula/trading-bot`). Los secrets se leen al ejecutar el job, así que no hace falta un push nuevo.

Mientras `DEPLOY_ENABLED` no sea `true`, el pipeline buildea la imagen y reporta "deployment NOT performed", y ningún PR puede mergearse porque el check obligatorio de deploy beta nunca se reporta (ver [sección 5, consecuencia (c)](#5-protección-del-repositorio)).

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

Estado verificado el 2026-09-14 (solo lectura). Para re-verificarlo: `gh ruleset list --repo emanuelturtula/trading-bot` y `gh ruleset view <id> --repo emanuelturtula/trading-bot`.

- **Ruleset `main`**: activo sobre la branch por defecto, **sin bypass** (nadie puede saltearlo, tampoco los administradores).
  - Pull request obligatorio, con 0 aprobaciones requeridas.
  - Status checks obligatorios (GitHub Actions): `Secrets scan`, `Lint & types`, `Tests`, `Docker build (arm64)` y `Deploy beta (8082) / Deploy beta`. Modo strict: la branch del PR tiene que estar actualizada con `main`.
  - Code scanning obligatorio con CodeQL (default setup, lenguajes `python` y `actions`): bloquea el merge con alertas de severidad `errors` o alertas de seguridad `high_or_higher`.
  - Borrado de la branch y force push bloqueados.
  - Copilot code review automático en cada push (no en PRs draft).
- Secret scanning y push protection: **activos**.
- Dependabot alerts y security updates: **activos**. Sus PRs, igual que los de version updates, se procesan según [PRs de Dependabot](#prs-de-dependabot).
- Actions: permisos por defecto del `GITHUB_TOKEN` en solo lectura (`read`), GitHub Actions no puede aprobar pull requests y los workflows de forks requieren aprobación para contribuidores primerizos (`first_time_contributors`).

Consecuencias:

- **(a) Todo PR mergeable sale de una branch `feature/**` con beta deployada.** `Docker build (arm64)` solo corre en eventos `pull_request` (`ci.yml`) y `Deploy beta (8082) / Deploy beta` solo en push a `feature/**` (`delivery.yml`). Un PR desde otra branch nunca reporta el check de deploy beta; los de Dependabot se procesan según [PRs de Dependabot](#prs-de-dependabot).
- **(b) Branch actualizada (strict).** Si `main` avanzó, hay que actualizar la branch (botón *Update branch* o `git merge origin/main` + push). Ese push vuelve a disparar CI y el deploy a beta, y hay que esperar a que terminen en verde antes de mergear.
- **(c) `DEPLOY_ENABLED`.** Si la variable no es `true`, el job `Deploy beta (8082)` se saltea, el check obligatorio `Deploy beta (8082) / Deploy beta` no se reporta (queda esperando el status) y **ningún PR puede mergearse**.

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

### PRs de Dependabot

Los PRs de Dependabot (version updates y security updates) **no se mergean directo**. Sus branches `dependabot/**` no disparan `delivery.yml` (solo corre en push a `feature/**` y `main`) y `scripts/remote_deploy.py` solo acepta deploys beta desde `refs/heads/feature/**`, así que el check obligatorio `Deploy beta (8082) / Deploy beta` nunca se reporta y el PR queda bloqueado (ver [sección 5](#5-protección-del-repositorio)). El cambio se trae a una branch `feature/deps-<slug>`.

**Camino liviano (sin agent team).** Aplica solo si el cambio es exclusivamente el bump generado por Dependabot: tag o digest en el `Dockerfile`, versiones en `pyproject.toml`/`uv.lock` o SHAs de actions en `.github/workflows/`, sin cambios de código ni de configuración. Las security updates siguen este mismo camino, con prioridad. Lo ejecuta el lead:

1. Revisar el PR de Dependabot (changelog de la dependencia, diff y CI) y crear la branch desde `main` actualizado:
   ```sh
   git fetch origin
   git switch -c feature/deps-<slug> origin/main
   ```
2. Traer el cambio con `git cherry-pick <sha>` del commit de Dependabot (`gh pr view <número> --repo emanuelturtula/trading-bot --json commits --jq '.commits[].oid'` lista los SHAs), o aplicar el mismo bump a mano en un commit propio (`build(deps): ...` o `ci(deps): ...`). Usar la opción manual si el cherry-pick no aplica limpio (`git cherry-pick --abort`) o si el ruleset pide aprobación extra por commits no atribuidos (autoría `dependabot[bot]`).
3. Correr `uv run python scripts/check.py`.
4. Pushear la branch: corre CI y el deploy a beta (puerto 8082). Verificar con `curl http://<DEPLOY_HOST>:8082/health` que la respuesta traiga `status` `ok`, `environment` `beta` y la `version` de ese run (`vX.Y.Z-beta.<sha7>`, la del summary del run). Un `/health` sano con la versión anterior no prueba que el bump esté deployado.
5. Abrir el PR desde `feature/deps-<slug>` referenciando el de Dependabot (por ejemplo, "Reemplaza #<número>") y cerrar el de Dependabot con un comentario que apunte al reemplazo: `gh pr close <número> --repo emanuelturtula/trading-bot --comment "Reemplazado por #<número del PR nuevo>"`. Al cerrarlo, Dependabot no vuelve a proponer esa versión: si el reemplazo no llega a mergearse, retomar el bump desde `feature/deps-<slug>` o aplicarlo a mano (Dependabot borra su branch al cerrar el PR, así que reabrirlo no es una vía confiable).
6. Merge solo con aprobación explícita del usuario.

Si el bump rompe tests o requiere cambios de código o de configuración, deja de ser liviano y pasa por el flujo completo del agent team (`/feature`, ver [CLAUDE.md](../CLAUDE.md)).

**Python está fijado en 3.12** en `.python-version`, `requires-python` (`pyproject.toml`), `[tool.ruff] target-version`, `[tool.mypy] python_version` y los dos `FROM` del `Dockerfile`. Dependabot ignora sus saltos minor/major (regla `ignore` del ecosistema `docker` en `.github/dependabot.yml`). Subir de versión es una feature explícita que actualiza todos esos puntos, `uv.lock` y la regla `ignore`.
