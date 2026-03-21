# quorbit-sdk

Python client for the [QUORBIT Protocol](https://quorbit.network) — trust layer for AI agents.

```python
from quorbit import QuorbitClient

client = QuorbitClient("http://localhost:8000")                    # auto-generates Ed25519 keypair
record = client.register("my-agent", {"nlp": 0.9, "code": 0.7})  # POST /api/v1/agents
client.heartbeat()                                                 # POST /api/v1/agents/{id}/heartbeat
agents = client.discover("summarise a legal document")            # POST /api/v1/discover
print(agents[0]["name"], agents[0]["discovery_score"])
```

Install: `pip install quorbit-sdk`
