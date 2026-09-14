# 001 — Dependabot sin saltos de Python y documentación de la protección del repo

- **Estado:** implementada
- **Branch:** `feature/dependabot-python-pin-docs`
- **Autor de la spec:** tech-lead
- **Tipo de commit esperado:** `chore:` (`.github/dependabot.yml`) y `docs:` (documentación). Ninguno es `feat` → bump patch: beta `v0.1.1-beta.<sha7>`, prod `v0.1.1`.

## Objetivo

Evitar que Dependabot vuelva a proponer saltos minor/major de la imagen `python` (el PR #2, `3.12-slim` → `3.14-slim`, rompió `Docker build (arm64)` porque el proyecto fija Python 3.12) y dejar la documentación alineada con la protección real de `main`. Esa protección hace que los PRs de Dependabot no puedan mergearse directamente, así que se formaliza un camino liviano para procesarlos (decisión D1) como excepción explícita al flujo del agent team. Además, se corrige la guía de carga de secrets en PowerShell, que guardaba valores vacíos sin avisar.

Configuración real verificada por el tech-lead con `gh api` (solo lectura) el 2026-09-14:

| Ítem | Valor real |
|------|------------|
| Ruleset | nombre `main`, `enforcement: active`, target `~DEFAULT_BRANCH`, `bypass_actors: []` |
| Pull request | obligatorio, `required_approving_review_count: 0` |
| Status checks | `Secrets scan`, `Lint & types`, `Tests`, `Docker build (arm64)`, `Deploy beta (8082) / Deploy beta` (GitHub Actions), `strict_required_status_checks_policy: true` |
| Code scanning | CodeQL, `alerts_threshold: errors`, `security_alerts_threshold: high_or_higher`; default setup `configured`, lenguajes `actions` y `python` |
| Otras reglas | `deletion` y `non_fast_forward` (sin borrado ni force push); `copilot_code_review` con `review_on_push: true`, `review_draft_pull_requests: false` |
| Seguridad del repo | secret scanning `enabled`, push protection `enabled`; Dependabot alerts activos (`vulnerability-alerts` → 204); Dependabot security updates `enabled` (`automated-security-fixes`: `enabled: true`, `paused: false`), activados por el lead (D2) |
| Actions | `default_workflow_permissions: read`, `can_approve_pull_request_reviews: false`, aprobación de workflows de forks: `first_time_contributors` |

## Fuera de alcance

- Cualquier cambio en `src/`, `tests/`, `scripts/`, `deploy/`, `.github/workflows/`, `.claude/`, `.gitleaks.toml`.
- `Dockerfile`, `pyproject.toml`, `uv.lock`, `.python-version`: Python sigue en 3.12.
- Los ecosistemas `github-actions` y `uv` de `dependabot.yml`, y los `schedule`/`commit-message` existentes.
- Modificar el ruleset o cualquier setting de GitHub: solo se documenta lo que ya existe, incluidos los alerts y las security updates que activó el lead.
- Fijar la imagen base por digest o automatizar la creación de branches `feature/deps-*` para PRs de Dependabot.
- Actualizar `.claude/skills/feature/SKILL.md` o las definiciones de agentes: el camino liviano lo ejecuta el lead y no usa ese skill.
- `README.md`, `docs/ARCHITECTURE.md`.

## Criterios de aceptación

### `.github/dependabot.yml`

- [ ] **CA1:** la entrada `package-ecosystem: docker` tiene `ignore` con exactamente un elemento: `dependency-name: python` y `update-types` = `version-update:semver-major` y `version-update:semver-minor`. No incluye `version-update:semver-patch` ni `versions`.
- [ ] **CA2:** fuera de esa clave `ignore`, el archivo es semánticamente idéntico al de `origin/main`. Además es YAML válido: pasa el script V1 y el hook `check-yaml`.
- [ ] **CA3:** un comentario YAML en inglés, sobre `ignore`, explica que Python está fijado en 3.12 y que subirlo es una feature explícita, no un bump de Dependabot.

### `docs/DEPLOYMENT.md`

- [ ] **CA4 (sección 5, configuración):** la sección "5. Protección del repositorio" reemplaza la línea vieja del ruleset (que listaba 3 checks) y describe la configuración real de la tabla de arriba:
  - ruleset `main` sobre la branch por defecto y sin bypass;
  - PR obligatorio con 0 aprobaciones;
  - los 5 checks con su nombre literal entre backticks y el requisito de branch actualizada con `main` (strict);
  - CodeQL obligatorio (default setup, `python` y `actions`, umbrales `errors` / `high_or_higher`);
  - borrado y force push bloqueados;
  - Copilot code review en cada push;
  - secret scanning y push protection activos;
  - Dependabot alerts y Dependabot security updates activos (D2);
  - la línea de Actions con los valores reales.

  Indica la fecha de verificación (2026-09-14).
- [ ] **CA5 (sección 5, consecuencias):** la sección explica:
  - (a) `Docker build (arm64)` solo corre en eventos `pull_request` y `Deploy beta (8082) / Deploy beta` solo en push a `feature/**`, así que todo PR mergeable sale de una branch `feature/**` con beta deployada;
  - (b) por strict, si `main` avanzó hay que actualizar la branch, y ese push vuelve a disparar CI y el deploy a beta;
  - (c) si `DEPLOY_ENABLED` no es `true`, el check de deploy beta no se reporta y ningún PR puede mergearse.

  La frase de la sección 3 sobre `DEPLOY_ENABLED` remite a (c).
- [ ] **CA6 (procedimiento Dependabot, camino liviano, D1):** existe una subsección `### PRs de Dependabot` (anchor `#prs-de-dependabot`), enlazada desde la sección 5, que contiene:
  - **el motivo:** las branches `dependabot/**` no disparan `delivery.yml` y `scripts/remote_deploy.py` solo acepta beta desde `refs/heads/feature/**`, así que nunca cumplen el check obligatorio de deploy beta;
  - **la condición del camino liviano:** el cambio es **solo** el bump de Dependabot (`Dockerfile`, `pyproject.toml`/`uv.lock` o SHAs de actions), sin cambios de código ni de config. Lo ejecuta el lead, sin agent team;
  - **los 6 pasos, numerados y en este orden:**
    1. el lead crea `feature/deps-<slug>` desde `origin/main` (`git fetch origin` + `git switch -c feature/deps-<slug> origin/main`);
    2. `git cherry-pick <sha>` del commit de Dependabot, o el mismo bump a mano con commit propio `build(deps): …` o `ci(deps): …`. Si el ruleset exige aprobación extra por commits no atribuidos, se usa la opción manual;
    3. `uv run python scripts/check.py`;
    4. push → CI + deploy beta (8082) → verificar `/health` (con placeholder `<DEPLOY_HOST>`) y la versión beta;
    5. PR desde `feature/deps-<slug>` que referencia al de Dependabot; el de Dependabot se cierra con un comentario que apunta al PR nuevo;
    6. merge solo con aprobación explícita del usuario;
  - **la salida del camino liviano:** si el bump rompe tests o requiere cambios de código o config, se usa el flujo completo con `/feature`;
  - **las security updates** de Dependabot siguen el mismo camino, con prioridad;
  - **la nota de Python:** fijado en 3.12 en `.python-version`, `requires-python`, `[tool.ruff] target-version`, `[tool.mypy] python_version` y los dos `FROM` del `Dockerfile`. Dependabot ignora sus saltos minor/major; subirlo es una feature explícita con `/feature` (nunca camino liviano) que actualiza esos puntos, `uv.lock` y la regla `ignore`.
- [ ] **CA7 (sección 3, secrets):** se elimina la recomendación del prompt interactivo (`gh secret set <NOMBRE>` "pide el valor por stdin"). En su lugar hay:
  - una advertencia: en PowerShell ese prompt puede guardar un valor vacío sin error;
  - un bloque `powershell` con las tres formas verificadas: `--body "<valor>"` solo para valores no sensibles; `$v = Read-Host ...; gh secret set ... --body $v` y luego `Remove-Variable v`; y para archivos, `` --body ((Get-Content "$HOME\.ssh\<clave>" -Raw) -replace "`r", "") ``, aclarando que `<` no existe en PowerShell;
  - los síntomas en CI: el mensaje de la action de Tailscale, `ValueError: Invalid host` de `remote_deploy.py` y el valor en blanco en lugar de `***` en el log;
  - el remedio: recargar el secret y re-ejecutar el job.

  Todo con placeholders (`<NOMBRE>`, `<valor>`, `<clave>`, `<archivo>`).

### `SECURITY.md`

- [ ] **CA8:** la fila `GitHub` de la tabla "Enforcement", en inglés como el resto del archivo, conserva secret scanning + push protection y agrega: Dependabot alerts y security updates activos (D2), ruleset de `main` sin bypass con PR obligatorio, status checks obligatorios (CI y deploy beta) sobre branch actualizada, CodeQL obligatorio y sin force push ni borrado. La tabla sigue teniendo 2 columnas.

### `docs/ROADMAP.md`

- [ ] **CA9:** la tabla tiene una columna `Estado` después de `Feature`: F0 = `Completada (v0.1.0)` y F1–F8 = `Pendiente`. Todas las filas tienen la misma cantidad de columnas.

### `CLAUDE.md`

- [ ] **CA10a (excepción en el flujo, D1):** en "Flujo obligatorio de features (Agent Team)", inmediatamente después del párrafo "Toda feature, fix o cambio… pasa por el equipo…" y antes de la tabla de roles, hay un párrafo de **excepción explícita** para PRs de Dependabot que dice:
  - aplica solo si el cambio es únicamente el bump (`Dockerfile`, `pyproject.toml`/`uv.lock` o SHAs de actions), sin cambios de código ni de config;
  - lo ejecuta el lead sin agent team, en `feature/deps-<slug>`: cherry-pick o bump manual `build(deps)`/`ci(deps)` → `scripts/check.py` → beta 8082 con `/health` verificado → PR que referencia al de Dependabot, que se cierra con comentario → merge solo con aprobación explícita del usuario;
  - las security updates van por el mismo camino, con prioridad;
  - si rompe tests o requiere código/config, se usa el flujo completo con `/feature`;
  - enlaza a `docs/DEPLOYMENT.md#prs-de-dependabot`.
- [ ] **CA10b (Git, versionado y entrega):** hay **un** bullet nuevo (≤ 3 líneas) que dice:
  - los PRs de Dependabot no se mergean directo: van por una branch `feature/deps-*` según la excepción de "Flujo obligatorio de features";
  - Python está fijado en 3.12: Dependabot ignora sus saltos minor/major y subir de versión es una feature explícita (nunca camino liviano).
- [ ] **CA10c (sin otros cambios):** en `CLAUDE.md` solo se agregan esos dos bloques. Las reglas inquebrantables, la tabla de roles, los pasos 1–9, "Los teammates nunca…" y "Protecciones activas" no cambian.

### Transversales

- [ ] **CA11 (scope):** los únicos archivos modificados o nuevos respecto de `origin/main` son `.github/dependabot.yml`, `docs/DEPLOYMENT.md`, `SECURITY.md`, `docs/ROADMAP.md`, `CLAUDE.md` y esta spec.
- [ ] **CA12 (datos sensibles):** ningún archivo cambiado contiene IPs, hostnames, usuarios de infraestructura, claves, tokens ni valores reales de secrets; solo placeholders. `gitleaks dir` pasa sobre cada archivo cambiado y la búsqueda por regex (V8) no encuentra nada.
- [ ] **CA13 (gate):** `uv run python scripts/check.py` y `uv run pre-commit run --files <archivos cambiados>` en verde.

## Diseño

### Archivos y dueños

| Archivo | Dueño | Cambio |
|---------|-------|--------|
| `.github/dependabot.yml` | developer | `ignore` en el ecosistema `docker` (CA1–CA3) |
| `docs/DEPLOYMENT.md` | developer | Secciones 3 y 5 y subsección `### PRs de Dependabot` en "Operación" (CA4–CA7) |
| `SECURITY.md` | developer | Fila `GitHub` (CA8) |
| `docs/ROADMAP.md` | developer | Columna `Estado` (CA9) |
| `CLAUDE.md` | developer | Párrafo de excepción en "Flujo obligatorio de features" y un bullet en "Git, versionado y entrega" (CA10a–CA10c) |
| — | tester | No agrega ni modifica archivos: ejecuta el plan de verificación y reporta |

**Autorización explícita:** esta spec asigna al developer `.github/dependabot.yml` (el único archivo de `.github/`) y `CLAUDE.md`. No hay Protocols, interfaces, migraciones ni variables `TB_*` nuevas.

**TDD aplicado a configuración:** antes de editar, el developer corre V1 y confirma que falla (el tech-lead ya lo verificó: `CA1 ignore mismatch: None`). Después de editar debe pasar.

### `.github/dependabot.yml` (texto esperado del bloque docker)

```yaml
  - package-ecosystem: docker
    directory: /
    schedule:
      interval: weekly
    commit-message:
      prefix: build
    # Python is pinned to 3.12 (.python-version, pyproject.toml). Upgrading it is an
    # explicit feature, never a Dependabot bump; patch updates are still allowed.
    ignore:
      - dependency-name: python
        update-types:
          - version-update:semver-major
          - version-update:semver-minor
```

### `docs/DEPLOYMENT.md`

**Sección 3.** Reemplazar el párrafo "Cargar los valores con `gh secret set <NOMBRE>`…" por la advertencia, este bloque y los síntomas. La oración de `DEPLOY_ENABLED` se mantiene y se le agrega la remisión a la sección 5.

```powershell
# Valor no sensible (queda escrito en la línea de comandos)
gh secret set <NOMBRE> --repo emanuelturtula/trading-bot --body "<valor>"

# Valor pegado a la vista, sin escribirlo en la línea de comandos
$v = Read-Host "<NOMBRE>"
gh secret set <NOMBRE> --repo emanuelturtula/trading-bot --body $v
Remove-Variable v

# Archivo: PowerShell no tiene redirección de entrada `<`; se quitan los CR de Windows
gh secret set DEPLOY_SSH_KEY --repo emanuelturtula/trading-bot --body ((Get-Content "$HOME\.ssh\<clave>" -Raw) -replace "`r", "")
```

Opcional: una línea para bash/zsh con `gh secret set <NOMBRE> --repo emanuelturtula/trading-bot < <archivo>`.

**Sección 5.** Estructura sugerida (el developer puede ajustar la redacción mientras cubra CA4 y CA5):

1. "Estado verificado el 2026-09-14."
2. Bullet "Ruleset `main`" con sub-bullets: PR obligatorio (0 aprobaciones), checks + strict, CodeQL, borrado/force push, Copilot review.
3. Bullets de secret scanning/push protection, de Dependabot alerts + security updates y de Actions.
4. Párrafo o lista "Consecuencias" con (a), (b), (c) y el link a `#prs-de-dependabot`.

Opcional: cómo re-verificarlo con `gh ruleset list --repo emanuelturtula/trading-bot`.

**Operación → `### PRs de Dependabot`.** Va al final de "Operación" porque es un procedimiento recurrente, no de setup. Contenido según CA6. El orden sugerido es: motivo → condición del camino liviano → 6 pasos → salida a `/feature` → security updates con prioridad → nota de Python. Es la versión detallada de la excepción de `CLAUDE.md` (CA10a): las dos tienen que decir lo mismo.

### `SECURITY.md` (texto sugerido)

```markdown
| GitHub | Secret scanning and push protection enabled; Dependabot alerts and security updates enabled; `main` ruleset without bypass: pull request required, required status checks (`Secrets scan`, `Lint & types`, `Tests`, `Docker build (arm64)`, beta deploy) on an up-to-date branch, required CodeQL code scanning, no force push or deletion |
```

### `CLAUDE.md` (textos sugeridos)

Excepción, justo después del párrafo introductorio de "Flujo obligatorio de features (Agent Team)" (CA10a):

```markdown
**Excepción — PRs de Dependabot (camino liviano).** Si el cambio es solo el bump de Dependabot (`Dockerfile`, `pyproject.toml`/`uv.lock` o SHAs de actions), sin cambios de código ni de config, el lead lo procesa sin agent team: branch `feature/deps-<slug>` desde `origin/main` → cherry-pick del commit de Dependabot (o el mismo bump con commit propio `build(deps)`/`ci(deps)`) → `scripts/check.py` → push, beta 8082 y `/health` verificado → PR que referencia al de Dependabot, que se cierra con comentario → merge solo con aprobación explícita del usuario. Las security updates van por el mismo camino, con prioridad. Si rompe tests o requiere código/config: flujo completo con `/feature`. Detalle en [Deploy](docs/DEPLOYMENT.md#prs-de-dependabot).
```

Bullet nuevo en "Git, versionado y entrega" (CA10b):

```markdown
- Dependabot: sus PRs no se mergean directo (no deployan a beta, que es check obligatorio); se traen a `feature/deps-<slug>` según la excepción de "Flujo obligatorio de features". Python está fijado en 3.12: Dependabot ignora sus saltos minor/major y subir de versión es una feature explícita (nunca camino liviano).
```

## Plan de tests

No hay comportamiento de runtime: no se agregan tests a `tests/`. La verificación son comandos reproducibles; V1 cumple el rol de "test que falla sin la implementación". Casos obligatorios de la plantilla:

- anti look-ahead, idempotencia y autorización: **N/A** (no hay indicadores, reglas, señales, Telegram ni API);
- redacción de secretos: aplica al contenido de la documentación y la cubre V8.

**Importante para tester y review:** los teammates no commitean, así que los cambios están en el working tree. El `main` local de este worktree está desactualizado, por lo que hay que comparar contra **`origin/main`** con `git diff origin/main` + `git status --porcelain` (no `git diff main...HEAD`). `scripts/secret_scan.py --history` solo escanea commits y no ve cambios sin commitear; por eso V8 usa `gitleaks dir`.

| Caso | Tipo | Qué verifica | CA |
|------|------|--------------|----|
| V1 | config | Script de abajo con el Python del venv: debe imprimir `dependabot.yml OK`. Sobre `origin/main` falla | CA1, CA2 |
| V2 | config | `uv run pre-commit run --files .github/dependabot.yml docs/DEPLOYMENT.md SECURITY.md docs/ROADMAP.md CLAUDE.md docs/specs/001-dependabot-python-pin-docs.md` en verde (`check-yaml`, EOF, trailing whitespace, `detect-private-key`) | CA2, CA13 |
| V3 | docs | En `docs/DEPLOYMENT.md` aparecen literalmente los 5 nombres de checks, `python`, `actions`, `errors`, `high_or_higher`, `Copilot`, `push protection`, `Dependabot alerts`, `security updates` y `2026-09-14`, y **no** queda la línea vieja con 3 checks. Lectura de (a), (b) y (c) | CA4, CA5 |
| V4 | docs | Existe `### PRs de Dependabot` y la sección 5 enlaza `#prs-de-dependabot`. La subsección incluye: `refs/heads/feature/**`; la condición "solo el bump" con `Dockerfile`, `uv.lock` y SHAs de actions; `feature/deps-`; `cherry-pick`; `build(deps)` y `ci(deps)`; el fallback por aprobación extra de commits no atribuidos; `scripts/check.py`; `8082` y `/health`; el cierre del PR de Dependabot con comentario; la aprobación explícita del usuario; `/feature` como salida; security updates con prioridad; y los puntos donde está fijado Python. Los 6 pasos están en el orden de CA6 | CA6 |
| V5 | docs | Sección 3: sin "pide el valor por stdin"; con `Read-Host`, `Remove-Variable`, `--body`, `Get-Content`, `-replace`, el mensaje de Tailscale, `Invalid host` y `***` | CA7 |
| V6 | docs | Fila `GitHub` de `SECURITY.md` con `Dependabot`, `ruleset` y `CodeQL` (CA8); columnas de `ROADMAP.md` con el one-liner de abajo (CA9); `git diff origin/main -- CLAUDE.md` muestra exactamente dos bloques agregados, sin líneas borradas: el párrafo de excepción entre el párrafo introductorio y la tabla de roles de "Flujo obligatorio de features" (CA10a) y un bullet en "Git, versionado y entrega" (CA10b, CA10c). La excepción coincide con los 6 pasos de `docs/DEPLOYMENT.md` y el anchor enlazado existe | CA8–CA10c |
| V7 | scope | `git diff origin/main --name-only` + `git status --porcelain` listan solo los 6 archivos de CA11 | CA11 |
| V8 | seguridad | `gitleaks dir --config .gitleaks.toml --redact --no-banner --ignore-gitleaks-allow --exit-code 1 <archivo>` **archivo por archivo** (con varios paths devuelve error) con exit 0 cada uno; grep de abajo sin matches (exit 1); revisión manual de que solo hay placeholders | CA12 |
| V9 | gate | `uv run python scripts/check.py` y `python scripts/secret_scan.py --history` con exit 0 | CA13 |
| V10 | post-merge (lead, manual, no bloquea el PR) | Dependabot lee la config desde `main`: en Insights → Dependency graph → Dependabot, el ecosistema docker queda sin error de configuración | CA1 |

`docker build` no es obligatorio porque el `Dockerfile` no cambia; igual lo cubre `Docker build (arm64)` en el PR.

**V1** (guardar en un archivo temporal fuera del repo y correrlo desde la raíz con `.venv/Scripts/python.exe`; PyYAML ya está en el venv como dependencia transitiva de pre-commit):

```python
import copy
import subprocess

import yaml

EXPECTED_IGNORE = [
    {
        "dependency-name": "python",
        "update-types": ["version-update:semver-major", "version-update:semver-minor"],
    }
]

new = yaml.safe_load(open(".github/dependabot.yml", encoding="utf-8"))
old = yaml.safe_load(
    subprocess.run(
        ["git", "show", "origin/main:.github/dependabot.yml"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
)
docker = [u for u in new["updates"] if u["package-ecosystem"] == "docker"]
assert len(docker) == 1, "expected exactly one docker entry"
assert docker[0].get("ignore") == EXPECTED_IGNORE, f"CA1 ignore mismatch: {docker[0].get('ignore')}"
stripped = copy.deepcopy(new)
for u in stripped["updates"]:
    if u["package-ecosystem"] == "docker":
        u.pop("ignore")
assert stripped == old, "CA2: anything besides docker.ignore changed"
print("dependabot.yml OK")
```

**V6 (columnas de ROADMAP):**

```bash
.venv/Scripts/python.exe -c "import sys; rows=[l for l in open('docs/ROADMAP.md',encoding='utf-8') if l.startswith('|')]; n={l.count('|') for l in rows}; print(n); sys.exit(len(n)!=1)"
```

**V8 (grep de datos sensibles; lo esperado es exit 1, sin matches):**

```bash
grep -nEi '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b|\.ts\.net\b|ssh-(ed25519|rsa)|BEGIN [A-Z ]*PRIVATE KEY|tske[y]-|gh[p]_|github_pa[t]_|\b[a-z_][a-z0-9_-]*@[a-z0-9][a-z0-9.-]*\.[a-z]{2,}\b' CLAUDE.md SECURITY.md docs/DEPLOYMENT.md docs/ROADMAP.md .github/dependabot.yml docs/specs/001-dependabot-python-pin-docs.md
```

Los corchetes (`tske[y]-`, `gh[p]_`) evitan que el patrón se encuentre a sí mismo en esta spec. Sobre los archivos actuales y esta spec, el grep ya da exit 1 (sin ruido), así que cualquier match nuevo es un hallazgo a revisar.

## Riesgos y seguridad

- **Datos de infraestructura en docs públicas.** Los ejemplos de PowerShell y del procedimiento usan solo placeholders. Nunca se pegan salidas reales de `gh secret`, logs de Actions, hostnames del tailnet ni usuarios. El slug `emanuelturtula/trading-bot` es público y ya está en el repo.
- **`--body "<valor>"` deja el valor en la línea de comandos** (historial del shell, lista de procesos). Por eso la doc lo limita a valores no sensibles y usa `Read-Host` para el resto.
- **La efectividad del `ignore` recién se ve después del merge a `main`** (Dependabot lee la config de la branch por defecto). Como el PR #2 se cerró, Dependabot tampoco reabriría la 3.14. La prueba real es la próxima minor de Python.
- **Patches de Python.** Con el tag actual `3.12-slim` (sin componente patch), lo esperable es que Dependabot no proponga PRs para `python`: los parches 3.12.x llegan por el tag flotante en cada rebuild. La regla sigue siendo correcta si en el futuro se fija `3.12.x-slim` o un digest, pero eso queda fuera de alcance.
- **`require_extra_approval_for_unattributed_changes: true` en el ruleset.** No está verificado cómo interactúa con commits de autoría `dependabot[bot]` traídos por cherry-pick y 0 aprobaciones requeridas. El paso 2 del procedimiento ya prevé la opción manual con commit propio.
- **El camino liviano no tiene review del tech-lead ni tester.** El riesgo es de supply chain: una dependencia o action comprometida entra sin segunda revisión del equipo. Mitigaciones:
  - la condición es estricta: cualquier cambio fuera del bump va por `/feature`;
  - siguen siendo obligatorios gitleaks, lint/mypy, tests, `Docker build (arm64)`, CodeQL, Copilot review y la beta con `/health`;
  - las actions están fijadas por SHA;
  - el merge requiere aprobación explícita del usuario.

  Recomendado, aunque no forma parte de los CA: que el lead lea las release notes del PR de Dependabot antes del cherry-pick.
- **Cerrar el PR de Dependabot en el paso 5, antes del merge del reemplazo.** Si el PR de reemplazo se abandona, Dependabot no vuelve a proponer esa misma versión (sí las siguientes). Es aceptable por la decisión D1, pero conviene tenerlo presente.
- **`DEPLOY_ENABLED` distinto de `true` bloquea todos los merges** (el check de deploy beta nunca se reporta). Queda documentado en CA5 (c).
- **Deploy:** sin impacto funcional. No hay migraciones, variables `TB_*` ni cambios en `secrets.env`. El push de la branch genera una beta `v0.1.1-beta.<sha7>` con la misma imagen funcional.
- **Reglas inquebrantables:** no aplica código de runtime; signal-only, `domain/` puro, UTC y un solo worker quedan intactos.

## Decisiones del usuario (2026-09-14)

- **D1 (antes P1): camino liviano para PRs de Dependabot.** No pasan por el agent team cuando el cambio es solo el bump de Dependabot (`Dockerfile`, `pyproject.toml`/`uv.lock` o SHAs de actions), sin cambios de código ni de config. Procedimiento, a cargo del lead:
  1. `feature/deps-<slug>` desde `origin/main`;
  2. cherry-pick, o el mismo bump a mano con commit propio `build(deps)`/`ci(deps)`, que es la opción a usar si el ruleset exige aprobación extra por commits no atribuidos;
  3. `uv run python scripts/check.py`;
  4. push → CI + beta 8082 → `/health`;
  5. PR que referencia al de Dependabot, que se cierra con comentario;
  6. merge solo con aprobación explícita del usuario.

  Si rompe tests o requiere código/config, se usa el flujo completo con `/feature`. Se documenta como excepción explícita en `CLAUDE.md` (CA10a) y en detalle en `docs/DEPLOYMENT.md` (CA6).
- **D2 (antes P2): Dependabot alerts y security updates activados** por el lead. El tech-lead también lo verificó: `automated-security-fixes` `enabled: true`, `vulnerability-alerts` → 204 y `dependabot_security_updates: enabled`. Las security updates siguen el camino liviano con prioridad. Se documentan en `docs/DEPLOYMENT.md` sección 5 (CA4), en `SECURITY.md` (CA8) y en la excepción de `CLAUDE.md` (CA10a).

## Checklist de revisión (tech-lead)

- [ ] Cumple las reglas inquebrantables de CLAUDE.md
- [ ] Diff sin secretos, IPs, hostnames ni usuarios (V8 + revisión manual)
- [ ] Cada CA verificado por el tester con evidencia (V1–V9); V1 falla sin la implementación
- [ ] Scope limitado a los 6 archivos de CA11
- [ ] La excepción de `CLAUDE.md` está acotada a bumps puros de Dependabot y coincide con `docs/DEPLOYMENT.md` (D1)
- [ ] `scripts/check.py` en verde
