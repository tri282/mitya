# Project Context
<!-- Global philosophy (~/.claude/CLAUDE.md) and modular defaults (~/.claude/rules/*.md — code style, git, tooling)
     already apply everywhere. This file is only for project-specific deltas: fill brackets below, or override a
     rules/ default (e.g. a different Code Style block) if this project needs something else. -->

- **Stack:** Python 3.14, FastAPI.
- **What this service does:** "Mitya" — Dostoevsky-styled local LLM. Two parts: (1) Mistral-7B QLoRA fine-tune, merged + GGUF-quantized via llama.cpp, done; (2) metadata-tagged RAG over Dostoevsky's texts (Qdrant, hybrid dense/sparse retrieval, reranking, FastAPI serving), not built yet.
- **Constraint:** target hardware is a 6GB GPU — every design/dependency choice must fit that budget (quantization, small rerankers, local-mode Qdrant, no heavyweight sparse encoders like SPLADE).
- **Architecture & design docs:** `docs/DESIGN.md` (full architecture, was README.md), `docs/RAG.md` (RAG design detail), `docs/FINE_TUNE.md` (training pipeline), `docs/TODO.md` (status). Read before non-trivial changes.
- **My role:** act as AI Engineer / MLOps engineer — favor production-shaped code (logging, caching, typed configs, API boundaries) over notebook-style scripts, but don't over-engineer past what a solo local deployment needs.
- **Monorepo**

# Approval Model
- **No prompt:** read-only exploration, linters, type-checkers, formatters.
- **Run to completion:** code edits within an approved plan.
- **Needs approval:** running code/builds/tests, migrations, anything with security impact.
- **Hard-blocked (I run these manually):** [`git push`, `kubectl apply`, `terraform apply`, `helm upgrade`, `aws/gcloud` mutating calls, `psql` against non-local DBs — trim to this project].

# Code Style & Conventions
<!-- Overrides ~/.claude/rules/code-style.md for this project, if different. -->
- Comment logical steps in large blocks; inline-comment only quirks. No over-commenting.
- **Logging, not `print`/`System.out`.**
- Match existing code's naming and idioms.

# Dependencies & Environment
- **Package manager:** `pip`. Don't generate other lock/req files.
- **Runtime/env:** activate `.venv`

# Testing & Verification
- Do **not** read large data files (`.csv`, `.parquet`, `.pt`, images, model weights) — context bloat.
