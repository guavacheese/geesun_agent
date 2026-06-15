##

## Installation

### Init project

```
uv init geesun_agent
```

### Install dependencies

```
uv add langchain langchain-openai openai deepagents

## install sandbox--choose one

uv add langchain-modal
uv add langchain-runloop

## sandbox
uv add langchain-daytona
uv add "langsmith[sandbox]"

## mcp
uv add langchain-mcp-adapters

## long-term memory
uv add langgraph-checkpoint-sqlite aiosqlite
uv add langgraph-checkpoint-postgres
uv add "psycopg[binary]" 

uv sync
```


## Install CubeSandbox locally
```
cd /mnt/d/workspace/geesun_agent
source .venv/bin/activate
uv pip install -e ../langchain-cubesandbox
```

## Install cubesandbox certificat
```
export SSL_CERT_FILE=/home/dhp/projects/cube-cert/cube-ca.pem

```
