# NNN — <título de la feature>

- **Estado:** borrador | aprobada | implementada
- **Branch:** `feature/<slug>`
- **Autor de la spec:** tech-lead
- **Tipo de commit esperado:** feat | fix | refactor | …

## Objetivo

Qué problema resuelve y para quién. Una o dos oraciones.

## Fuera de alcance

Lo que explícitamente no se hace en esta feature.

## Criterios de aceptación

- [ ] CA1: …
- [ ] CA2: …

## Diseño

- Módulos y archivos afectados (con dueño: developer o tester).
- Interfaces/Protocols nuevos o modificados (firmas).
- Modelo de datos o migraciones.
- Configuración nueva (`TB_*`): nombre, tipo, si es secreto.

## Plan de tests

| Caso | Tipo (unit/integration) | Qué verifica |
|------|--------------------------|--------------|
| … | … | … |

Obligatorios según el caso: anti look-ahead (indicadores/reglas), idempotencia (señales), autorización (Telegram/API), redacción de secretos (config/logs).

## Riesgos y seguridad

- Datos sensibles involucrados y cómo se protegen.
- Impacto en deploy (migraciones, variables nuevas en `secrets.env`).

## Checklist de revisión (tech-lead)

- [ ] Cumple las reglas inquebrantables de CLAUDE.md
- [ ] Diff sin secretos, IPs, hostnames ni usuarios
- [ ] Tests cubren los criterios de aceptación
- [ ] `scripts/check.py` en verde
