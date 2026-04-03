# Skill: python-async

## Purpose
Async Python patterns, common pitfalls, and COSMOS-specific async conventions for FastAPI + SQLAlchemy + aiokafka + asyncio.

## Loaded By
`engineer`

---

## Core Patterns

### Session management (SQLAlchemy async)
```python
# Always use async context manager — never hold session across awaits
async def get_data(query: str) -> list:
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(text(sql), {"param": value})
            await session.commit()
            return result.fetchall()
        except Exception as e:
            await session.rollback()
            logger.error("service.failed", error=str(e), error_code="ERR-COSMOS-NNN")
            raise
```

**Never do:**
```python
# ✗ WRONG — session leaks if exception occurs
session = AsyncSessionLocal()
result = await session.execute(...)
await session.close()  # never reached on exception
```

### External HTTP calls (httpx)
```python
# Always use async client with timeout
async with httpx.AsyncClient(timeout=settings.MCAPI_TIMEOUT) as client:
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()
```

### Parallel execution (asyncio.gather)
```python
# Run independent coroutines in parallel
results = await asyncio.gather(
    leg1_exact_lookup(query),
    leg2_ppr_search(query),
    leg3_bfs_search(query),
    leg4_vector_search(query),
    leg5_lexical_search(query),
    return_exceptions=True,  # don't let one failure cancel others
)

# Filter out exceptions
valid = [r for r in results if not isinstance(r, Exception)]
```

### Timeout wrapping
```python
# Always wrap external calls with timeout
try:
    result = await asyncio.wait_for(
        qdrant_client.search(collection_name, query_vector),
        timeout=settings.LLM_TIMEOUT,
    )
except asyncio.TimeoutError:
    logger.warning("qdrant.timeout", timeout=settings.LLM_TIMEOUT)
    return []
```

---

## Common Pitfalls

### Missing await (silent bug — most dangerous)
```python
# ✗ WRONG — returns coroutine object, not result
result = session.execute(text(sql))  # forgot await

# ✓ CORRECT
result = await session.execute(text(sql))
```

Symptom: `result` is a coroutine object, not data. Often causes `AttributeError` later.

### Blocking I/O in async context (blocks event loop)
```python
# ✗ WRONG — blocks entire server
import requests
response = requests.get(url)  # synchronous!

# ✓ CORRECT
async with httpx.AsyncClient() as client:
    response = await client.get(url)
```

### Shared mutable state across coroutines
```python
# ✗ WRONG — results list shared across parallel coroutines
results = []
async def fetch_and_append():
    data = await fetch()
    results.append(data)  # race condition

# ✓ CORRECT — return values, don't mutate shared state
async def fetch():
    return await fetch_data()

results = await asyncio.gather(fetch(), fetch(), fetch())
```

### Session not rolled back on error
```python
# ✗ WRONG — session stays in bad state
async with AsyncSessionLocal() as session:
    await session.execute(...)
    # exception raised here
    await session.commit()  # never reached

# ✓ CORRECT — always rollback in except
async with AsyncSessionLocal() as session:
    try:
        await session.execute(...)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
```

---

## FastAPI Async Patterns

### Dependency injection for DB session
```python
# app/api/deps.py
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

# In endpoint
@router.get("/items")
async def get_items(session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT * FROM items"))
    return result.fetchall()
```

### Background tasks
```python
# For fire-and-forget (logging, metrics, non-critical)
@router.post("/query")
async def query(request: QueryRequest, background_tasks: BackgroundTasks):
    result = await process_query(request)
    background_tasks.add_task(log_query_trace, request, result)
    return result
```

### SSE (Server-Sent Events) for streaming
```python
from fastapi.responses import StreamingResponse

async def event_generator(query: str):
    async for chunk in stream_response(query):
        yield f"data: {json.dumps(chunk)}\n\n"

@router.get("/stream")
async def stream(query: str):
    return StreamingResponse(event_generator(query), media_type="text/event-stream")
```

---

## Kafka (aiokafka) Patterns

### Producer
```python
from aiokafka import AIOKafkaProducer

producer = AIOKafkaProducer(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)
await producer.start()
try:
    await producer.send_and_wait(topic, json.dumps(payload).encode())
finally:
    await producer.stop()
```

### Consumer
```python
from aiokafka import AIOKafkaConsumer

consumer = AIOKafkaConsumer(
    topic,
    bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
    group_id=settings.KAFKA_CONSUMER_GROUP,
)
await consumer.start()
try:
    async for message in consumer:
        await handle_message(json.loads(message.value))
finally:
    await consumer.stop()
```

---

## COSMOS Async Conventions

1. **All service methods are `async def`** — no sync service methods
2. **Config is read from `settings.*`** — never `os.environ.get()` in service code
3. **Errors logged with `error_code`** — `logger.error("service.failed", error_code="ERR-COSMOS-NNN")`
4. **Timeouts on all external calls** — use `asyncio.wait_for()` or client-level timeout
5. **Circuit breaker** for upstream failures — see `app/engine/circuit_breaker.py`
