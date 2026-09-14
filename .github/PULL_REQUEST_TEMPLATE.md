## Qué cambia

<!-- Resumen corto. Link a la spec: docs/specs/NNN-<slug>.md -->

## Tipo

- [ ] feat
- [ ] fix
- [ ] refactor / chore / docs / ci / build
- [ ] breaking change (`!`)

## Flujo del agent team

- [ ] Spec aprobada por tech-lead
- [ ] Implementación (developer) con TDD
- [ ] Verificación (tester): PASS con evidencia
- [ ] Review final (tech-lead): approve

## Checklist

- [ ] `uv run python scripts/check.py` en verde
- [ ] Sin secretos, IPs, hostnames ni usuarios de infraestructura en el diff (gitleaks OK)
- [ ] `domain/` sigue siendo puro; sin look-ahead (solo velas cerradas)
- [ ] Sin código de ejecución de órdenes
- [ ] Beta verificada en el puerto 8082 (`/health` con la versión beta)

## Evidencia de beta

<!-- versión, digest de imagen, link al run -->
