# AgentForge DeepSeek Agent v0.1 — Windows Quick Start

> This is a **RELEASE_CANDIDATE** source bundle, not a standalone installer or executable. It intentionally excludes Python virtual environments, database data, model weights, and local model runtimes. A targeted synthetic DeepSeek multi-turn host scenario passed; the full 15-scenario matrix and excluded features are not claimed as passed. See `RELEASE_V0.1_REPORT.md` and `RELEASE_V0.1_LIMITATIONS.md`.

## Requirements

- Windows 10/11 and Python 3.10 or newer. The project was last verified with its project virtual environment on Python 3.12.7.
- PostgreSQL already installed and running at `127.0.0.1:5432`, with database `agentforge` and least-privilege role `agentforge_app`.
- Network access to the official DeepSeek API and a DeepSeek API credential.
- PowerShell 5.1 or newer for the included `.cmd` launchers.
- Local Qwen and Embedding services are optional. No model weights or llama-server runtime are included.

## Prepare Python environment

Open PowerShell in the extracted `AgentForge-v0.1` directory, the same directory that contains `pyproject.toml`. Check that `python --version` reports Python 3.10 or newer, then create the package-local environment:

```powershell
python -m venv .agentforge\af012-venv
& .\.agentforge\af012-venv\Scripts\python.exe -m pip install -e ".[persistence]"
```

The persistence extra installs SQLAlchemy, psycopg, Alembic, and Chroma. Python package installation may need access to your configured package index. No model is downloaded by these commands. If you already use the development checkout at `D:\agent\AgentForge` and its `.agentforge\af012-venv` exists, you do not need a second environment just to use that checkout; run its root `Start-AgentForge.cmd`.

## Prepare PostgreSQL

Start PostgreSQL yourself. Create the `agentforge` database and `agentforge_app` role with the permissions needed for AgentForge migrations and application access. Keep the role password private. `Start-AgentForge.cmd` asks for the application-role password through a hidden prompt on first use and stores it encrypted for the current Windows account. The launcher applies forward migrations; it does not start/stop PostgreSQL or create a disposable database.

## Start AgentForge

**DeepSeek-only release:** double-click `Start-AgentForge.cmd`. Do not use `Start-Qwen.cmd` or `Start-AgentForge-All.cmd` unless you separately installed the local llama-server runtime and Qwen model assets. Those assets are not included in this package. An error naming `models\qwen3.5-4b\.runtime\llama-b11424\llama-server.exe` means the optional local Qwen launcher was selected; it does not indicate a DeepSeek connection failure.

1. Double-click `Start-AgentForge.cmd`.
2. At the hidden prompt, enter the `agentforge_app` database password.
3. In the console, enter `/model auth set deepseek`. Enter the API credential only in its hidden local prompt. The key is stored with Windows DPAPI for the current account; do not put it in source or send it in chat.
4. Create a conversation with `/new My project`. The console explains that the question and relevant evidence/context may be sent to DeepSeek. Type `YES` only if you agree. Capability discovery happens only after this consent.
5. Ask a general question, or register and index a repository:

   ```text
   /repo register D:\work\my-project
   /repo index D:\work\my-project
   ```

   If indexing completed before conversation creation, it is available to the current Auto Planner. Ask a code question normally; the Planner chooses General or Repository retrieval.

6. Use `/list`, `/switch 1`, `/history`, `/rename New title`, `/model`, `/modellist`, `/help`, and `/exit` as needed.

If a model capability message says the DeepSeek cache is stale, run `/model refresh` after reviewing the cloud-data notice. This refresh requests model metadata; it does not send your repository content. A new conversation still requires its own cloud-data consent.

## Optional local Qwen and vectors

Use `/model qwen-local` only when local Qwen is configured and its service is running. The included local-model launchers expect operator-supplied pinned runtime/model assets in the layout described in `README.md`; the release bundle contains no model weights or binaries.

Code symbol and keyword retrieval does not require Qwen Embedding. Chroma semantic retrieval is optional and requires a configured local embedding service. Configure and start that service, then run `/chroma index` and `/chroma on` in the console.

## Common issues

- **PostgreSQL login or migration error:** check that PostgreSQL is running and that the hidden password belongs to `agentforge_app` for database `agentforge`. Do not paste the password into chat or command-line arguments.
- **DeepSeek credential error:** run `/model auth status`; it reports only whether a credential is configured and its source. Use `/model auth set deepseek` to replace it.
- **Stale or missing model capability metadata:** after cloud consent, run `/model refresh`.
- **Repository answer has insufficient evidence:** confirm the intended repository is registered and indexed. AgentForge should state uncertainty rather than invent a source claim.
- **Planner or tool failure:** the application reports failure and fails closed. This v0.1 preparation has an offline-tested one-shot correction for invalid planner JSON, but the corrected real-model path is not yet verified.

## Data and cloud boundary

Session and memory data are stored in PostgreSQL. Repository indexes are local SQLite files. Chroma vectors are local and rebuildable. DeepSeek receives conversation content, relevant repository evidence, and approved context only after per-session consent. The launcher does not start or stop services and does not perform silent provider fallback.
