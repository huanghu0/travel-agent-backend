# DashScope Embedding Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task in the current `rag` checkout. The user explicitly requires no test execution, no commit, and no push. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the RAG Embedding provider with Alibaba Cloud Bailian `qwen3.7-text-embedding` through its OpenAI-compatible endpoint while preserving the existing 768-dimensional Qdrant and sharing workflows.

**Architecture:** `DashScopeEmbeddingClient` adapts the existing OpenAI SDK to the provider-neutral `EmbeddingClient` protocol. `RagRuntime` remains the composition root and injects DashScope configuration into the adapter; sharing, indexing, retrieval, MySQL, and Qdrant retain their existing boundaries.

**Tech Stack:** Python 3.11+, OpenAI Python SDK 1.109.1, FastAPI, Qdrant Client 1.19.0, unittest-compatible test modules.

## Global Constraints

- Use only `qwen3.7-text-embedding` with `EMBEDDING_DIMENSION=768`.
- Read secrets only from `DASHSCOPE_API_KEY`; never write a real key to the repository.
- Read the workspace-compatible endpoint only from `DASHSCOPE_BASE_URL`.
- Remove the old provider client, environment variable, model default, SDK dependency, and live-evaluation option without a compatibility alias.
- Preserve the current Qdrant Collection schema and business workflows.
- Update existing tests so they can be run later, but do not execute any test command in this task.
- Do not call an external Embedding API, do not commit, and do not push.

---

### Task 1: Replace the provider adapter boundary

**Files:**
- Modify: `app/rag/embedding.py`
- Create: `tests/test_dashscope_embedding.py`
- Delete: `tests/test_gemini_embedding.py`

**Interfaces:**
- Consumes: `openai.OpenAI` and an injected compatible client exposing `embeddings.create(...)`.
- Produces: `DashScopeEmbeddingClient(api_key, base_url, model, dimension, timeout_seconds, max_attempts, client=None)` with `embed(text) -> list[float]`.

- [ ] **Step 1: Define the DashScope behavior in the renamed test module without running it**

Use a fake compatible client whose request boundary is:

```python
class FakeEmbeddings:
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def response_for(values):
    return SimpleNamespace(data=[SimpleNamespace(embedding=values)])
```

Assert that `embed("safe input")` sends exactly:

```python
{
    "model": "qwen3.7-text-embedding",
    "input": "safe input",
    "dimensions": 768,
}
```

Retain coverage definitions for numeric conversion, empty/missing data, wrong dimensions, non-finite values, 429/5xx/timeout mapping, 4xx mapping, sanitized logging, and constructor arguments. Patch `app.rag.embedding.OpenAI` and assert `base_url`, `timeout`, and `max_retries=3` when `max_attempts=4`.

- [ ] **Step 2: Implement `DashScopeEmbeddingClient`**

Replace provider imports and construction with:

```python
from openai import OpenAI


class DashScopeEmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int,
        timeout_seconds: float,
        max_attempts: int,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=max(0, max_attempts - 1),
        )
```

Call the provider with:

```python
response = self._client.embeddings.create(
    model=self.model,
    input=text,
    dimensions=self.dimension,
)
```

Read `response.data[0].embedding`, retain the existing finite 768-dimensional validation and stable error classes, and change the sanitized event prefix to `dashscope_embedding`.

- [ ] **Step 3: Remove the superseded test module**

Delete `tests/test_gemini_embedding.py` only after `tests/test_dashscope_embedding.py` contains the complete replacement definitions. Do not execute either test module.

---

### Task 2: Migrate application configuration and runtime composition

**Files:**
- Modify: `.env.example`
- Modify: `app/core/config.py`
- Modify: `app/rag/interfaces.py`
- Modify: `app/rag/runtime.py`
- Modify: `app/sharing/service.py`
- Modify: `main.py`
- Modify: `tests/test_rag_config.py`
- Modify: `tests/test_rag_runtime.py`

**Interfaces:**
- Consumes: environment values from the existing `_env` helpers.
- Produces: `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, `EMBEDDING_MODEL=qwen3.7-text-embedding`, and a runtime-created `DashScopeEmbeddingClient`.

- [ ] **Step 1: Update test settings and factory expectations without running them**

Replace setting fixtures with:

```python
"DASHSCOPE_API_KEY": "dashscope-key",
"DASHSCOPE_BASE_URL": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
"EMBEDDING_MODEL": "qwen3.7-text-embedding",
"EMBEDDING_DIMENSION": 768,
```

Add or rename assertions so missing API Key maps to `invalid_dashscope_api_key`, missing Base URL maps to `invalid_dashscope_base_url`, and runtime factory kwargs include `base_url`.

- [ ] **Step 2: Replace settings and validation**

Define settings as:

```python
DASHSCOPE_API_KEY: Optional[str] = _env("DASHSCOPE_API_KEY")
DASHSCOPE_BASE_URL: str = _env("DASHSCOPE_BASE_URL", "") or ""
EMBEDDING_MODEL: str = (
    _env("EMBEDDING_MODEL", "qwen3.7-text-embedding")
    or "qwen3.7-text-embedding"
)
```

When either feature is enabled, append exact non-secret validation errors for a blank API Key or Base URL. Reject every `EMBEDDING_MODEL` value except `qwen3.7-text-embedding`. Keep the existing 768-dimension, timeout, retry, lease, Qdrant, and RAG validation.

- [ ] **Step 3: Replace runtime construction and health configuration**

Import and default to `DashScopeEmbeddingClient`, then construct it with:

```python
embedding = embedding_client_factory(
    api_key=settings.DASHSCOPE_API_KEY,
    base_url=settings.DASHSCOPE_BASE_URL,
    model=settings.EMBEDDING_MODEL,
    dimension=settings.EMBEDDING_DIMENSION,
    timeout_seconds=settings.EMBEDDING_TIMEOUT_SECONDS,
    max_attempts=settings.EMBEDDING_MAX_ATTEMPTS,
)
```

Require both DashScope values and the exact Qwen model in `_embedding_configured()` and `_runtime_configuration_errors()`. Extend `_configuration_reason()` with `DASHSCOPE_API_KEY` and `DASHSCOPE_BASE_URL`, and remove the old environment variable name.

- [ ] **Step 4: Restore the provider-neutral sharing type boundary**

Replace the concrete provider annotation in `app/sharing/service.py` with:

```python
from app.rag.interfaces import EmbeddingClient

# constructor annotation
embedding_client: EmbeddingClient,
```

Declare `model: str`, `dimension: int`, and `embed(text) -> list[float]` on the
`EmbeddingClient` Protocol. Keep `InvalidEmbeddingError` imported from
`app.rag.embedding` because it is part of the stable adapter error contract.
Update the optional-adapter composition comment in `main.py` to name
DashScope/Qdrant without changing runtime assembly.

- [ ] **Step 5: Update the environment sample without adding credentials**

Use exactly:

```dotenv
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=768
```

Do not add the user's workspace host or API Key to a tracked file.

---

### Task 3: Migrate maintenance and evaluation scripts

**Files:**
- Modify: `scripts/reindex_shared_guides.py`
- Modify: `scripts/reconcile_shared_guide_index.py`
- Modify: `scripts/run_rag_retrieval_evaluation.py`
- Modify: `tests/test_rag_operations.py`

**Interfaces:**
- Consumes: the same `Settings` object used by the application runtime.
- Produces: maintenance commands using `DashScopeEmbeddingClient` and a manual `--live-dashscope` calibration option.

- [ ] **Step 1: Update script-oriented test fixtures without running them**

Provide `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, and `qwen3.7-text-embedding` in fake settings. Update mocked factory expectations to include `base_url` and rename any live-provider option assertions to `--live-dashscope`.

- [ ] **Step 2: Update reindex and reconcile composition roots**

In both scripts import `DashScopeEmbeddingClient`, update concrete type annotations, and construct it with:

```python
embedding = DashScopeEmbeddingClient(
    api_key=settings.DASHSCOPE_API_KEY,
    base_url=settings.DASHSCOPE_BASE_URL,
    model=settings.EMBEDDING_MODEL,
    dimension=settings.EMBEDDING_DIMENSION,
    timeout_seconds=settings.EMBEDDING_TIMEOUT_SECONDS,
    max_attempts=settings.EMBEDDING_MAX_ATTEMPTS,
)
```

Before constructing the client in apply mode, reject a model other than
`qwen3.7-text-embedding` so incompatible vectors cannot enter the v1
Collection. The live evaluation uses the same fixed model.

- [ ] **Step 3: Rename live retrieval calibration**

Expose:

```python
parser.add_argument(
    "--live-dashscope",
    action="store_true",
    help="manual calibration only; fixtures are never modified",
)
```

Rename `_live_scores` dependencies to `DashScopeEmbeddingClient`, read `DASHSCOPE_API_KEY` and `DASHSCOPE_BASE_URL`, default the model to `qwen3.7-text-embedding`, and return a sanitized JSON error when either required value is missing. Do not invoke this option during implementation.

---

### Task 4: Remove dependency and fixture-level provider coupling

**Files:**
- Modify: `requirements.txt`
- Modify: `tests/test_mysql_stores.py`
- Modify: `tests/test_rag_retrieval.py`
- Modify: `tests/test_share_index_worker.py`
- Modify: `tests/test_shared_guide_api.py`
- Modify: `tests/test_shared_guide_models.py`
- Modify: `tests/test_shared_guide_service.py`
- Modify: `tests/test_shared_guide_store.py`

**Interfaces:**
- Consumes: existing provider-neutral fake clients and persisted `embedding_model` strings.
- Produces: fixtures consistently identifying `qwen3.7-text-embedding`.

- [ ] **Step 1: Remove the unused SDK dependency**

Delete only this requirement:

```text
google-genai==2.16.0
```

Keep `openai==1.109.1`, which is shared by LLM and Embedding integrations.

- [ ] **Step 2: Update provider metadata in tests without executing them**

Replace persisted and fake model values with:

```python
model = "qwen3.7-text-embedding"
```

Change provider-outage sample messages to neutral text such as `"embedding provider offline"`; no assertion may depend on the old provider name.

---

### Task 5: Update active documentation and historical RAG design references

**Files:**
- Modify: `docs/shared-guide-rag-operations.md`
- Modify: `docs/superpowers/specs/2026-08-26-shared-guide-rag-design.md`
- Modify: `docs/superpowers/plans/2026-08-26-shared-guide-rag.md`

**Interfaces:**
- Consumes: the finalized environment names, client name, model, and manual calibration option from Tasks 1-3.
- Produces: deployment and maintenance instructions that match the executable code.

- [ ] **Step 1: Update active deployment instructions**

Document these secret-loading and calibration commands without real values:

```powershell
$env:DASHSCOPE_API_KEY = (Get-Secret -Name DashScopeEmbeddingApiKey -AsPlainText)
$env:DASHSCOPE_BASE_URL = (Get-Secret -Name DashScopeEmbeddingBaseUrl -AsPlainText)
python scripts/run_rag_retrieval_evaluation.py --live-dashscope
Remove-Item Env:DASHSCOPE_API_KEY
Remove-Item Env:DASHSCOPE_BASE_URL
```

Describe Qwen quota/403 handling, data minimization, key redaction, and full reindexing before cutover when incompatible provider vectors exist.

- [ ] **Step 2: Align the original design and implementation plan**

Mechanically update the old provider-specific architecture labels, client name, default model, configuration names, live-calibration option, outage descriptions, and privacy wording to the approved DashScope design. Preserve unrelated RAG decisions and task history.

---

### Task 6: Perform static-only completion audit

**Files:**
- Inspect: all files changed by Tasks 1-5
- Do not modify or inspect any real secret file outside the repository

**Interfaces:**
- Consumes: completed uncommitted working-tree changes.
- Produces: a handoff report explicitly marked as not runtime-tested.

- [ ] **Step 1: Search active runtime surfaces for superseded identifiers**

Run only static searches, excluding the migration design and migration plan that necessarily describe the transition:

```powershell
rg -n -i "gemini|google-genai" app scripts tests requirements.txt .env.example docs/shared-guide-rag-operations.md docs/superpowers/specs/2026-08-26-shared-guide-rag-design.md docs/superpowers/plans/2026-08-26-shared-guide-rag.md
```

Expected: no matches.

- [ ] **Step 2: Search for accidental API Key material**

Inspect only tracked and newly created project files for full secret-looking assignments. Report suspicious matches without printing their values. The expected repository state contains only blank `DASHSCOPE_API_KEY=` examples and fake test keys.

- [ ] **Step 3: Review the uncommitted diff without running code**

Use `git diff --check`, `git diff --stat`, and `git status --short` with the repository-specific safe-directory override. Do not invoke Python, unittest, pytest, quality-gate, curl, Docker, package installation, or any network command.

- [ ] **Step 4: Hand off server validation commands**

Report the exact modified-file count and provide server-side setup/test commands separately. State plainly that implementation was not executed or runtime-verified in this session.
