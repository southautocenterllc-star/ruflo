# ROK VPN — Especificación de la app móvil para Lovable

Este documento es el brief completo para reconstruir la app móvil de ROK VPN en
Lovable. Está sacado del build de Claude Design (`templates/mobile-screens.html`)
y del backend real (`portal/app.py`), no de memoria.

Se usa así: copia el bloque **Prompt inicial** en Lovable, y ten a mano el resto
como referencia para los siguientes turnos.

---

## Antes de empezar: dos decisiones que hay que tomar

**1. El idioma.** El diseño de referencia está en español en cinco pantallas
(Login, Dashboard Off, Dashboard On, Planes, Config) y en inglés sólo en
Locations. Es una inconsistencia del propio diseño. La recomendación es
**español en toda la app**, porque es lo que usan cinco de las seis pantallas y
también la landing. Las etiquetas de estado quedarían: `Conectado`, `Disponible`,
`Premium`, `Próximamente`.

**2. Los datos de las pantallas son maqueta.** El diseño muestra 11 países con
Canadá y Reino Unido como «Available», velocidades de 42.3/18.7 Mbps y el correo
`juan@correo.com`. Nada de eso viene del backend: el catálogo real tiene 20
ubicaciones y hoy sólo Nueva York está activa. Si la app va a ser real, los datos
tienen que salir de `/api/v1/servers` y `/api/v1/me`, no del diseño.

---

## Prompt inicial para Lovable

> Construye una app móvil de VPN llamada **ROK VPN**, en español, con React +
> TypeScript + Tailwind. Es una app de cliente para un servicio VPN con
> WireGuard. Diseño oscuro, denso, estilo iOS nativo.
>
> **Paleta** (usar exactamente estos valores como tokens):
> - Fondo app: `#050D18` · Superficie/tarjeta: `#0F2030` · Superficie elevada: `#0B1820`
> - Texto principal: `#F0F2F5` · Texto secundario: `#C7D6E2` · Texto atenuado: `#5C7A94`
> - Azul de marca: `#4A80A8` · Azul profundo: `#2D5F8A` · Azul claro: `#8AB4CC`
> - Verde de acción: `#2ECC71` · Verde «conectado»: `#00FF88`
> - Ámbar premium: `#F5A623` · Rojo error: `#EC3013`
>
> **Tipografía**: Inter. Pesos 400/500/600/700/800/900. Tamaños 10, 11, 12, 13,
> 14, 15, 16, 18, 20, 22, 28, 32 px. Los títulos de sección van en mayúsculas,
> 10-11px, peso 700, `letter-spacing: 0.12em`, color `#5C7A94`.
>
> **Radios**: 4, 8, 10, 12, 14, 20 y 24px. Las tarjetas usan 14px, las píldoras
> de estado 20px, los botones grandes 12px.
>
> **Navegación**: barra inferior fija con cuatro pestañas — **VPN**, **Servidor**,
> **Planes**, **Config**. La pantalla de Login va fuera de esa barra.
>
> Crea estas cinco pantallas: Login, Dashboard (con estado conectado y
> desconectado), Ubicaciones, Planes y Configuración. Empieza por el Dashboard.

---

## Pantallas

### 1. Login / Registro

Fuera de la navegación inferior. Estructura vertical, centrada:

- Logo de ROK VPN (la roca con el ojo de cerradura), unos 72px
- Wordmark **ROK VPN** — «ROK» en `#F0F2F5`, «VPN» en `#4A80A8`, peso 900, 32px
- Subtítulo «Seguro. Confiable. Privado.» en `#5C7A94`, 14px
- Conmutador de dos pestañas: **Crear Cuenta** / **Iniciar Sesión**. La activa
  con fondo `#2D5F8A` y borde `#4A80A8`, radio 10px
- Campos con etiqueta encima (13px, peso 600, `#C7D6E2`):
  - Correo electrónico — placeholder `tu@correo.com`
  - Contraseña — placeholder `Mínimo 8 caracteres`
  - Confirmar contraseña — placeholder `Repite tu contraseña` (solo en registro)
- Botón principal verde `#2ECC71`, texto oscuro `#0F2030`, peso 800, ancho completo
- Pie: «© 2025 LUROK BUSINESS GROUP LLC», 11px, `#5C7A94`

Validación: la contraseña mínima es de 8 caracteres — el backend rechaza menos
con 400, así que conviene validarlo antes de enviar.

### 2. Dashboard

Es la pantalla principal (pestaña **VPN**) y tiene dos estados.

**Desconectado:**
- Escudo o círculo grande, apagado, en `#5C7A94`
- Título **DESCONECTADO** en mayúsculas, peso 800
- Subtítulo «Tu conexión no está protegida»
- Botón **Conectar**, verde `#2ECC71`, ancho completo, radio 12px
- Tres tarjetas de métrica en fila: `PLAN` / Gratis · `DISPOSITIVOS` / 1/1 ·
  `VELOCIDAD` / 5 Mbps. Etiqueta en 10px mayúsculas `#5C7A94`, valor en 16px peso 700

**Conectado:**
- El mismo círculo, ahora en verde `#00FF88` con un halo suave
- Título **CONECTADO**, subtítulo «Tu conexión está protegida»
- Línea de sesión: `00:14:32 · Miami, FL` — cronómetro que corre y ubicación actual
- Botón **Desconectar** (variante secundaria, no verde)
- Métricas: `DESCARGA` 42.3 Mbps · `SUBIDA` 18.7 Mbps · `PLAN` Mensual ·
  `DISPOSITIVOS` 2/5 · `PROTOCOLO` WireGuard

### 3. Ubicaciones (pestaña **Servidor**)

- Cabecera con el título centrado y un icono de filtro a la derecha
- Buscador tipo píldora, radio 24px, fondo `#0B1820`, placeholder «Buscar país o ciudad»
- Lista agrupada por región, con encabezado en mayúsculas 10px `#8AB4CC`:
  Norteamérica · Sudamérica · Europa · Asia
- Cada fila: bandera (emoji, 22px) · nombre del país (15px, peso 700) con la
  ciudad debajo (13px, `#8AB4CC`) · píldora de estado · chevron `>`
- La fila del servidor conectado lleva **borde izquierdo verde de 3px** y un
  fondo con tinte verde muy leve

Píldoras de estado:

| Estado | Texto | Color | Fondo |
|---|---|---|---|
| Conectado | `CONECTADO` | `#00FF88` | verde al 14% |
| Disponible | `Disponible` | `#2ECC71` | verde al 13% |
| Premium | `Premium 🔒` | `#F5A623` | ámbar al 14% |
| Próximamente | `Próximamente` | `#5C7A94` | gris al 15% |

El buscador debe filtrar **sin distinguir tildes**: escribir «mexico» tiene que
encontrar «México». La forma corta es normalizar ambos lados:

```ts
const norm = (s: string) =>
  s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
```

### 4. Planes

- Título «Planes», subtítulo «Elige el plan que mejor se adapte»
- Banda del plan actual: «Plan Gratis activo · 1 dispositivo · 5 Mbps»
- Tarjeta **Mensual**, con etiqueta `POPULAR`: `$4.99` grande + `/ mes`
- Tarjeta **Anual**, con etiqueta `MEJOR VALOR`: `$29.99` + `/ año` + `~$2.50/mes`
- Ambas listan, con palomita verde: 5 dispositivos · Velocidad sin límite ·
  Modo wstunnel TCP 443 · Soporte prioritario. La anual añade «Equivale a $2.50/mes»
- Botones: **Suscribir Mensual** / **Suscribir Anual**

Los precios reales salen de `/api/v1/plans` en centavos (`499` y `2999`).

### 5. Configuración

- Cabecera de perfil: avatar circular con la inicial, correo, y «Plan Mensual · Activo»
- Sección **VPN**: Protocolo (WireGuard) · Kill Switch (interruptor) ·
  Modo wstunnel (interruptor) · DNS personalizado (Automático)
- Sección **GENERAL**: Conectar al iniciar (interruptor) · Notificaciones
  (interruptor) · Idioma (Español)
- Sección **ACERCA DE**: Versión 1.0.0 · Política de Privacidad · Términos de Servicio
- Botón **Cerrar Sesión** en rojo `#EC3013`
- Pie: «© 2025 LUROK BUSINESS GROUP LLC»

---

## API

Base: `https://rokvpn.com` (configurable por variable de entorno).

La autenticación es un JWT que se manda en cada petición protegida:

```
Authorization: Bearer <token>
```

### Endpoints

| Método | Ruta | Auth | Qué hace |
|---|---|---|---|
| POST | `/api/v1/register` | — | Crea la cuenta. Devuelve **201** con `{token, tier}` |
| POST | `/api/v1/login` | — | Devuelve 200 con `{token, tier}` |
| GET | `/api/v1/me` | Sí | Perfil: `{id, email, tier, devices, device_limit, subscription}` |
| GET | `/api/v1/servers` | Opcional | Catálogo por región |
| GET | `/api/v1/devices` | Sí | Dispositivos del usuario |
| POST | `/api/v1/devices` | Sí | Alta de dispositivo. Devuelve la config WireGuard |
| DELETE | `/api/v1/devices/<id>` | Sí | Revoca un dispositivo |
| GET | `/api/v1/plans` | — | Planes y precios |
| POST | `/api/v1/subscribe` | Sí | Cobro con Square. Body: `{plan, nonce}` |
| POST | `/api/v1/subscription/cancel` | Sí | Vuelve al plan gratuito |

### Forma de `/api/v1/servers`

```json
{
  "regions": [
    {
      "region": "north_america",
      "label": "North America",
      "servers": [
        {
          "id": 1,
          "code": "US",
          "flag": "🇺🇸",
          "country": "United States",
          "city": "New York",
          "region": "north_america",
          "tier_required": "free",
          "load_percent": 0,
          "status": "online",
          "available": true,
          "lon": -74.0,
          "lat": 40.7
        }
      ]
    }
  ]
}
```

`status` es `online`, `locked` o `coming_soon` — mapean a Conectado/Disponible,
Premium y Próximamente. Si se manda el token, los países de pago llegan como
`online` en vez de `locked`, así que la misma llamada sirve antes y después del
login.

### Detalles que ahorran depuración

- **Registro devuelve 201**, no 200. Tratar sólo el 200 como éxito rompe el alta.
- **Hay límite de peticiones**: 5 por minuto en `/api/v1/register`,
  `/api/v1/login` y `/api/v1/subscribe`; 100 por minuto en el resto. Al pasarse
  llega un **429** con `{"error": "..."}`. Conviene mostrar ese mensaje en vez de
  reintentar en bucle, o la app se autobloquea.
- **El correo se normaliza** a minúsculas y sin espacios en el servidor.
- **Los límites de dispositivos** son 1 en el plan gratuito y 5 en los de pago.
  Pasarse devuelve 403.
- **Emoji de bandera**: viene ya resuelto en el campo `flag`; no hace falta
  convertir el código ISO.

---

## Cosas que conviene no copiar del diseño

- **El spinner de Locations.** En el bundle hay un hueco grande sobre el buscador
  con un spinner azul que nunca resuelve — se comprobó a 2, 8 y 20 segundos.
  Parece un mapa que quedó a medias. O se pone un mapa de verdad, o se quita ese
  espacio.
- **Las métricas de velocidad** (42.3 / 18.7 Mbps) y el cronómetro son estáticos
  en la maqueta. En la app real salen del túnel activo.
- **`juan@correo.com`** es texto de relleno; va el correo de `/api/v1/me`.

---

## Nota sobre lo que Lovable no puede hacer

Lovable genera una app web. Un cliente VPN de verdad necesita permisos de sistema
para levantar el túnel WireGuard — en iOS es la Network Extension, en Android el
`VpnService`. Eso no existe en un navegador.

Así que hay dos caminos honestos:

1. **App de gestión** (lo que Lovable sí puede dar hoy): cuenta, plan, servidores,
   alta de dispositivos y descarga del `.conf` o del QR, que el usuario importa en
   la app oficial de WireGuard. El backend ya lo soporta entero.
2. **Cliente VPN completo**: hace falta React Native o nativo, envolviendo la
   Network Extension o el `VpnService`. Lovable serviría para prototipar la
   interfaz, pero el túnel hay que hacerlo fuera.

La opción 1 es un producto entregable de inmediato y usa el backend tal cual
está. Conviene decidirlo antes de empezar, porque cambia bastante lo que se
construye.
