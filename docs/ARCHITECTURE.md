# Arquitectura

## Solicitud del Record Creator

```mermaid
stateDiagram-v2
    [*] --> draft
    draft --> parsing: action_parse()
    parsing --> preview: LLM (nivel cheap) devuelve JSON + confianza
    parsing --> failed: sin plantilla / error del LLM
    preview --> executed: action_confirm_and_execute() → materialize()
    preview --> failed: error al materializar
    executed --> [*]
    failed --> [*]
```

El campo `confidence` y `warnings` se muestran en la vista previa: con confianza baja o campos obligatorios faltantes el usuario completa los huecos antes de confirmar.

## Router de LLM y auditoría

```mermaid
sequenceDiagram
    participant C as Código llamador
    participant R as ov.ai.llm.router
    participant L as Proveedor LLM
    participant A as Audit
    C->>R: complete(system, user, tier, require_json)
    R->>R: elegir modelo del tier (cheap / balanced / expensive)
    R->>L: _dispatch(model, system, user, max_tokens)
    alt éxito
        L-->>R: texto + tokens
        R->>A: fila (tokens in/out, costo USD, latencia, modelo)
        R-->>C: text, tokens, cost_usd, latency_ms, audit_id
    else excepción
        R->>A: fila con error y 0 tokens
        R-->>C: error controlado
    end
```

## Ciclo de un Run de empleado virtual

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> planning: action_start()
    planning --> failed: error al percibir / planear
    planning --> awaiting_approval: hay acciones en confirm
    planning --> executing: solo acciones auto / readonly
    awaiting_approval --> executing: action_approve_and_execute()
    awaiting_approval --> rejected: action_reject()
    executing --> completed: ninguna acción falló
    executing --> failed: alguna acción falló
    completed --> [*]
```

### Estados de una acción

| Estado | Significado |
|---|---|
| `suggested` | Capacidad en `readonly`: solo propone, nunca escribe |
| `awaiting_confirmation` | Capacidad en `confirm`: espera aprobación humana |
| `planned` | Capacidad en `auto`: se ejecuta en `_execute_all` |
| `executed` / `confirmed` | Ejecutada (directa / vía el *creator gate*) |
| `rejected` | Fuera de política, deshabilitada, sin cuota o rechazada por el operador |
| `failed` | El handler falló |

### Guardas de un empleado

```mermaid
flowchart TD
    S([Disparo]) --> A{"¿Estado del empleado?"}
    A -- "paused / archived / draft" --> X1[No corre]
    A -- activo --> B{"¿Presupuesto diario superado?"}
    B -- sí --> X2["over_budget → pausa automática"]
    B -- no --> C{"¿Dentro del horario laboral<br/>de su zona horaria?"}
    C -- no --> X3["No corre en este disparo"]
    C -- sí --> D[Run: percibir → planear]
```

### Trust dial

| Nivel | Autonomía por defecto |
|---|---|
| `shadow` | `readonly` |
| `assisted` | `confirm` |
| `autonomous` | `auto` |
| `trusted` | `auto` |

Sobrescribible por capacidad, con cuota diaria (`0` = ilimitada).
