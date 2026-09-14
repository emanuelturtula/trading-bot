---
name: feature
description: Desarrolla una feature del trading-bot de punta a punta con el agent team (tech-lead, developer, tester), desde la spec hasta el PR verificado en beta.
disable-model-invocation: true
argument-hint: "<descripción de la feature>"
---

# /feature — flujo obligatorio con Agent Team

Pedido: $ARGUMENTS

Seguí `CLAUDE.md` (sección "Flujo obligatorio de features"). Vos sos el **lead**.

## 0. Preparación
1. Leé `CLAUDE.md`, `docs/ARCHITECTURE.md` y `docs/ROADMAP.md`. Si el pedido contradice una regla inquebrantable (por ejemplo, ejecutar órdenes), frená y explicáselo al usuario.
2. Verificá `git status` limpio y partí de `main` actualizado: `git fetch origin` y `git switch -c feature/<slug> origin/main`.
3. Si el pedido es ambiguo en lo funcional, preguntale al usuario antes de spawnear el equipo.

## 1. Equipo
Spawneá tres teammates **usando las definiciones de agente**, con nombres fijos:
- `tech-lead` (agent type `tech-lead`)
- `developer` (agent type `developer`)
- `tester` (agent type `tester`)

Los teammates no heredan esta conversación: en el prompt de cada uno incluí el pedido completo, la branch, las respuestas del usuario y qué tarea le toca.

## 2. Tareas compartidas
Creá las tareas con estos prefijos exactos (el hook `TaskCompleted` depende de ellos) y dependencias en cadena:
1. `[spec] <slug>` → tech-lead
2. `[impl] <slug>` → developer (bloqueada por spec)
3. `[test] <slug>` → tester (bloqueada por impl)
4. `[review] <slug>` → tech-lead (bloqueada por test)

## 3. Coordinación
- Cuando la spec esté lista, leela. Si surgen decisiones de producto, consultá al usuario y reenviá las respuestas.
- Dejá que developer, tester y tech-lead iteren por mensajes (FAIL → developer; REQUEST CHANGES → developer).
- No implementes vos: si un teammate se traba, redirigilo o reasigná la tarea.

## 4. Integración y entrega (solo el lead)
1. Con `[review]` en APPROVE, corré `uv run python scripts/check.py` y revisá el diff completo buscando secretos, IPs, hostnames o usuarios.
2. Commits en Conventional Commits en inglés, stageando paths explícitos (nunca `git add -A` a ciegas ni `commit -a`).
3. `git push -u origin feature/<slug>`. Los hooks corren gitleaks; si bloquean, arreglá la causa y nunca uses `--no-verify`.
4. Seguí `delivery.yml` con `gh run watch`. Un push o una imagen buildeada no son un deploy verificado: confirmá el job de beta en verde y, si hay acceso, `/health` en el puerto 8082 con la versión `vX.Y.Z-beta.<sha7>`.
5. Abrí el PR con `gh pr create` usando `.github/PULL_REQUEST_TEMPLATE.md`: link a la spec, veredictos y evidencia de beta.
6. Cerrá el equipo (pedile a cada teammate que termine).
7. Reportale al usuario: qué se hizo, versión beta, link al PR y qué probar. **No mergees sin aprobación explícita.**

## 5. Merge (solo con aprobación explícita del usuario)
`gh pr merge <n> --merge`, seguí el run de `main` y verificá deploy prod en 8081 (`vX.Y.Z`) y la GitHub Release.
