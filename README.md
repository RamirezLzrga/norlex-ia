# Norlex IA

Chatbot de Norlex reescrito desde cero en Python. Sustituye al `ChatbotService`
de C# (1.442 lineas de reglas `if`/`Contains`) por un agente con **function
calling** sobre Groq: el modelo decide qué consultar, y cada dato sale de la
base de datos mediante una herramienta protegida por rol.

## Por que esto y no el chatbot actual

El chatbot de C# es un arbol de reglas. Groq solo se usaba como ultimo recurso
(`ChatbotService.cs:306`), sin acceso a la base de datos, y el prompt le pedia
expresamente "NO inventes datos" (`ChatbotService.cs:413`). Resultado: el bot
sabia *que* leyes existen pero no *que dicen*.

Aqui el modelo no puede responder un dato sin llamar antes a una herramienta:

```
usuario -> rol (leido de BD_Fed) -> modelo ve SOLO las herramientas de su rol
        -> elige herramienta -> el servidor valida el rol otra vez
        -> consulta SQL parametrizada -> el resultado vuelve al modelo
        -> texto final
```

Si una herramienta no devuelve el dato, el modelo no lo tiene y no lo inventa.

## Arranque

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # y pon tu GROQ_API_KEY
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8099
```

- Swagger: http://127.0.0.1:8099/docs
- Estado: `GET /health`

```powershell
.\.venv\Scripts\python.exe test_agente.py   # 6 pruebas, no necesita API key
.\.venv\Scripts\python.exe pruebas.py       # chat real, necesita API key
```

## Los 4 roles

El rol se resuelve **siempre contra la base de datos**, nunca desde una
cabecera ni desde el claim `UserLevel` de la cookie (ese vale 1 o 2 y no
coincide con `Employees.UserLevel`, que vale -1 / 3 / 4).

| Rol | Se detecta con | Ve |
|---|---|---|
| `visitante` | sin sesion | 3 herramientas publicas |
| `usuario_empresa` | `Employees.UserLevel = 4` | + legislacion y sus datos |
| `titular` | `Clientes.EmployeeId = su EmployeeID` | + administracion de su empresa |
| `admin` | `UserLevel = -1` o `Email = admin@norlexinternacional.com` | las 16 herramientas |

Reglas replicadas de `PermisoMateriaService.cs` y `Login.cshtml.cs`:

- empresa = `Clientes.EmployeeId == id` o `Employees.ClienteId != null`
- colecciones = siglas de los planes vigentes + `PermisosMateria` del usuario
- **lista de siglas vacia = SIN RESTRICCION** (admin, o plan con `TODAS`)
- `EsCliente && !TieneSuscripcionActiva` = acceso bloqueado

Cada herramienta se filtra **dos veces**: una al construir el catalogo que ve el
modelo, otra al ejecutarla. Si el modelo alucina una llamada prohibida, el
servidor la rechaza (`tools.ejecutar`).

## Herramientas (16)

**Publicas** — `catalogo_planes`, `que_ofrece_la_plataforma`, `como_contratar`

**Autenticado** — `buscar_legislacion`, `detalle_ordenamiento`,
`mi_suscripcion`, `mis_materias`, `mis_alertas`, `mi_empresa`, `mi_perfil`

**Solo admin** — `metricas_negocio`, `solicitudes_pendientes`,
`listar_clientes`, `usuarios_registrados`, `empresas_sin_uso`,
`actividad_sospechosa`

Para agregar una: defines la funcion con el decorador `@herramienta(...)` en
`app/tools.py` y ya aparece en el catalogo del rol que le asignes.

## API

### `POST /api/chat/enviar`

```json
{
  "mensaje": "cuando vence mi plan?",
  "historial": [{ "rol": "user", "contenido": "hola" }],
  "ordenamiento_id": null,
  "pagina": null
}
```

```json
{
  "mensaje": "Tu plan Comercio vence el 2027-01-15, en 142 dias.",
  "sugerencias": [{ "etiqueta": "Ver mis materias", "pregunta": "..." }],
  "rol": "titular",
  "herramientas_usadas": ["mi_suscripcion"],
  "iteraciones": 2
}
```

Compatibilidad con el widget actual (`wwwroot/js/chatbot.js`): el campo
`mensaje` acepta `**markdown**` y `\n`, igual que `ChatbotRespuesta` en C#.

### Otros

- `GET /api/chat/bienvenida` — mensaje y sugerencias de arranque por rol
- `GET /api/chat/herramientas` — que herramientas ve el rol que pregunta (diagnostico)
- `GET /health` — estado y connectivity

## Vista de pruebas

`http://127.0.0.1:8099/demo` — una pagina para ver el chatbot funcionando sin
tocar la app .NET. Cambias de rol en la izquierda y ves como cambia todo:

- el contexto resuelto (empresa, suscripcion, siglas permitidas)
- **exactamente que herramientas ve el modelo** para ese rol
- el chat, y debajo el **JSON crudo** que salio de cada herramienta

Cualquier herramienta se puede ejecutar suelta desde el panel: consultas la BD
real y te muestra el dato. Es la forma mas rapida de verificar que el chatbot
dice la verdad.

La pagina vive en `/api/demo/*`, que **elige el rol desde un parametro de query
y NO exige firma HMAC**. Por eso esta desactivado salvo que pongas:

```env
NORLEX_IA_DEMO=true
```

En cuanto lo dejas en `false`, esos endpoints responden 403 y `/` redirige a
`/docs`. No lo actives en un entorno con datos reales: cualquiera en internet
podria pedir el rol `admin`.

## Desplegar en Render

Si, se puede subir. El repo ya trae `render.yaml`, `Procfile` y `Dockerfile`.
En el panel: New > Blueprint, apuntas al repo y Render lee el `render.yaml`.

**El bloqueo real no es Render, es la base de datos.** Ahora mismo el servicio
usa `Trusted_Connection` (Windows Auth) contra `.\SQLEXPRESS`, y Render no puede
alcanzar eso. Necesitas una de estas:

| Opcion | Como |
|---|---|
| Azure SQL | la mas comoda para .NET; migras `BD_Fed` y pones los env vars |
| Tu SQL Server con IP publica | abrir el 1433 y crear un login SQL, con firewall por IP |
| Tunel (ngrok / tailscale) | solo para desarrollo, no para produccion |

Con cualquiera de las dos primeras defines en Render:

```
NORLEX_DB_SERVER   = tcp:tu-servidor,1433   (o el host de Azure)
NORLEX_DB_NAME     = BD_Fed
NORLEX_DB_USER     = sa  (o un login de solo lectura)
NORLEX_DB_PASSWORD = ...
NORLEX_IA_SHARED_SECRET = el mismo que pongas en appsettings.Local.json
GROQ_API_KEY       = una key valida de console.groq.com
NORLEX_IA_DEMO     = false
NORLEX_IA_ALLOW_ANONYMOUS = false
```

Dos avisos sobre el plan:

- **El plan free suspende el contenedor a los 15 minutos.** La primera peticion
  tardara ~50 s en despertar y mientras tanto la base puede seguir cerrada, por
  eso `db._obtener_conexion` reintenta 3 veces. Para produccion, plan *starter*.
- **Crea un login de solo lectura** para el servicio. Hoy escribe en `Auditoria`
  y `Consultas`, asi que necesita esos permisos, pero no `db_owner`.

Si prefieres Azure SQL, el connection string cambia un poco: hay que quitar
`Encrypt=no` y poner `Encrypt=yes;TrustServerCertificate=yes`. Si usas el
servidor por nombre en el connection string actual ya funciona.

## Seguridad

El navegador nunca habla con este servicio. La peticion llega a .NET, se
autentica con la cookie de sesion, y .NET reenvia firmado. Python no
reimplementa login ni 2FA.

```
X-Norlex-EmployeeId: 42          # 0 o ausente = visitante
X-Norlex-Timestamp: 1788000000   # epoch segundos
X-Norlex-Nonce: a3f9c2...
X-Norlex-Signature: <hex HMAC-SHA256 de "42.1788000000.a3f9c2...">
```

La firma prueba que la peticion sale de .NET. **El rol no viene en la
cabecera**: se relee de `Employees`, asi que aunque alguien falsifique el
EmployeeId con una firma valida, no puede convertirse en admin.

### Cliente C#

```csharp
public sealed class NorlexIaClient
{
    private readonly HttpClient _http;
    private readonly string _secreto;
    private readonly string _baseUrl;

    public NorlexIaClient(IHttpClientFactory factory, IConfiguration config)
    {
        _http = factory.CreateClient(nameof(NorlexIaClient));
        _secreto = config["Norlex:Ia:SharedSecret"] ?? "";
        _baseUrl = config["Norlex:Ia:BaseUrl"] ?? "http://127.0.0.1:8099";
    }

    public async Task<ChatbotRespuesta?> EnviarAsync(
        string mensaje, int? employeeId, string? userName, string ip, CancellationToken ct = default)
    {
        var id = employeeId ?? 0;
        var ts = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var nonce = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        var carga = $"{id}.{ts}.{nonce}";

        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(_secreto));
        var firma = Convert.ToHexString(hmac.ComputeHash(Encoding.UTF8.GetBytes(carga))).ToLowerInvariant();

        using var peticion = new HttpRequestMessage(HttpMethod.Post, $"{_baseUrl}/api/chat/enviar");
        peticion.Headers.Add("X-Norlex-EmployeeId", id.ToString());
        peticion.Headers.Add("X-Norlex-Timestamp", ts.ToString());
        peticion.Headers.Add("X-Norlex-Nonce", nonce);
        peticion.Headers.Add("X-Norlex-Signature", firma);
        peticion.Headers.Add("X-Norlex-Ip", ip);
        peticion.Content = JsonContent.Create(new { mensaje });

        using var respuesta = await _http.SendAsync(peticion, ct);
        if (!respuesta.IsSuccessStatusCode) return null;

        return await respuesta.Content.ReadFromJsonAsync<ChatbotRespuesta>(cancellationToken: ct);
    }
}
```

Configurar en `appsettings.Local.json` (ignorado por git):

```json
{
  "Norlex": {
    "Ia": {
      "BaseUrl": "http://127.0.0.1:8099",
      "SharedSecret": "el mismo NORLEX_IA_SHARED_SECRET del .env"
    }
  }
}
```

## Base de datos

Solo lee `BD_Fed` en `.\SQLEXPRESS`. Usa `Trusted_Connection` (Windows Auth), asi
que **el servicio debe correr en la misma maquina que SQL Server**. Para correr
en otro host, define `NORLEX_DB_USER` / `NORLEX_DB_PASSWORD` en el `.env`.

Escribe unicamente dos tablas, igual que el chatbot de C#:

- `Auditoria` (`Accion = 'ChatbotIA'`) en cada peticion
- `Consultas` cuando el modelo busca legislacion

Notas de esquema que se verificaron contra la base real:

- `Materia` y `Coleccion` guardan siglas separadas por comas, pero **con
  taxonomias distintas**: `Materia` usa ENGA/SGIN/TPTE/LBRL y **0 filas**
  contienen una sigla canonica. Todo el filtrado va por `Coleccion`.
- `LEGISXXI.Nombrecorto` es la clave (`04031608`), no el nombre. El nombre real
  esta en `Nombrecompleto`.
- `Modificaciones` se enlaza por `NombreCorto`, no por `OrdenamientoId`.
- `Plan` es palabra reservada en T-SQL: siempre `[Plan]`.
- `ENTIDADES` y `MUNICIPIOS` dan 0 coincidencias en `Coleccion`: se filtran por
  el estado y el municipio de la empresa, no por sigla.

## Estado actual

- 16 herramientas, 14 probadas contra la BD real sin errores
- 4 roles con filtros de herramientas y permisos de colecciones verificados
- Vista de pruebas en `/demo` funcionando
- Falta una `GROQ_API_KEY` valida para probar el texto final del modelo
- Falta una BD accesible desde internet para desplegar en Render

## Lo que falta

- **Texto de los artículos**: `buscar_legislacion` devuelve fichas (clave,
  estatus, fechas), no el articulado. Para responder "que dice el artículo 42"
  hace falta recuperar el texto de la tabla que lo contiene y pasarlo como
  contexto. Es el siguiente paso natural y el que mas valor aporta.
- Historial persistente entre recargas (hoy se manda en el body de la peticion)
- Streaming de la respuesta
- Caché y reintentos en las consultas
- Rate limiting propio (hoy depende del que impone .NET)
