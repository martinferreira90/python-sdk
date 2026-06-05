# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**Short-lived fork** of [`Universal-Commerce-Protocol/python-sdk`](https://github.com/Universal-Commerce-Protocol/python-sdk) (origin: `martinferreira90/python-sdk`). It exists for one reason: the upstream PyPI release of `ucp-sdk` lags behind the UCP spec, and a downstream project needs models from a newer spec version. The fork regenerates the models against a more recent UCP release and is consumed as an external lib (likely via git dep) by that downstream project. **Once the official PyPI package catches up, this fork is throwaway** — don't invest in long-term maintenance, novel features, or architectural changes.

Current target version is encoded in the branch name (e.g. `feat/v2026-04-08`); previous bump on `main` was `2026-01-23` (commit `cbf9c9a`). The job on any working branch is almost always: pick a UCP version, regenerate, fix anything the preprocessor + codegen don't handle for that version, ship.

The package itself is a **thin, code-generated Pydantic v2 model library** with no runtime business logic. The entire surface area under `src/ucp_sdk/models/schemas/` is regenerated from upstream UCP JSON Schemas; **do not hand-edit generated files** — changes will be overwritten the next time `generate_models.sh` runs. Real changes belong in `preprocess_schemas.py`, `templates/`, `generate_models.sh`, or upstream in the UCP spec repo.

Python >= 3.10. Dependency management via [`uv`](https://docs.astral.sh/uv/).

## Typical workflow on this fork

1. `./generate_models.sh <version>` against the new UCP release (branch on the upstream UCP repo: `release/<version>`).
2. If codegen fails or produces nonsense for a new schema shape, fix it in `preprocess_schemas.py` (see pipeline below) — not by editing the generated `.py` files.
3. `uv run ruff format && uv run ruff check --fix src/ucp_sdk` to clean up.
4. Commit the regenerated `src/ucp_sdk/models/schemas/` tree with a conventional-commit message (e.g. `fix: update Python SDK schemas for UCP release <version>`).
5. Bump `version` in `pyproject.toml` if the downstream consumer pins by version.

## Common commands

```bash
uv sync                                     # install deps (incl. dev group for code generation)
./generate_models.sh                        # regenerate from UCP spec main branch
./generate_models.sh 2026-01-23             # regenerate from a specific release branch (release/<version>)
uv run ruff format                          # format
uv run ruff check --fix src/ucp_sdk         # lint
uv run pre-commit run --all-files           # full lint/format/checks (ruff + prettier + shellcheck + codespell)
uv build                                    # build sdist + wheel (release path, also runs in CI on GitHub release)
```

There is no test suite — the SDK is generated artifacts. "Verifying" a change means regenerating, then importing/validating with Pydantic in a REPL or sample.

## Model generation pipeline (the only thing that matters here)

`generate_models.sh` is a three-stage pipeline. Understanding stage 2 (`preprocess_schemas.py`) is the key to making non-trivial changes — it does work that `datamodel-codegen` cannot do on its own.

```
1. git clone the UCP spec repo into ./ucp/   (release/<ver> branch, or main)
2. uv run python preprocess_schemas.py        # rewrites JSON schemas in-place + emits variant files
3. uv run datamodel-codegen ...                # JSON Schema → Pydantic v2 → src/ucp_sdk/models/schemas/
4. uv run ruff format && ruff check --fix     # post-process generated code
```

The `./ucp/` directory is a transient clone — it's wiped and re-cloned each run. Treat anything inside it as read-only scratch.

### `preprocess_schemas.py` — what it does and why

`datamodel-codegen` produces awkward Python for schemas that use heavy `allOf` inheritance, unions, or operation-specific shapes. The preprocessor normalizes the schemas first:

- **`allOf` flattening** (`merge_all_of_to_node`): merges all `allOf` branches into a single flat node so the codegen emits one clean class instead of a chain of intermediate base classes. Local `$ref`s are resolved inline; external `$ref`s that can't be resolved are kept in a slim `allOf`.
- **Branch distribution** (`distribute_properties_to_branches`): copies the base object's `properties` / `required` into each `anyOf` / `oneOf` branch so every union variant is self-contained (otherwise generated union members miss inherited fields).
- **Entity inlining** (`flatten_entity_reference`): replaces `$ref: ucp.json#/$defs/entity` with the inlined entity definition so generated models inherit from `BaseModel` directly, not from a generated `Entity` class.
- **Variant generation** (`ucp_request` marker → `*_create_request.json`, `*_update_request.json`, `*_complete_request.json`): JSON schemas can annotate a property with `"ucp_request": "omit" | "required" | { "create": ..., "update": ... }`. The preprocessor scans for these markers and emits a separate JSON file (and therefore a separate generated Python class) per operation, with properties included/required per the marker rules. The "needs a variant" signal is then **propagated transitively** down the `$ref` graph — if `Foo` needs a `create` variant and references `Bar`, `Bar` also gets a `create` variant so refs match. External `$ref`s inside variants are rewritten to point to the matching variant file (e.g. `product.json` → `product_create_request.json`).
- **Metadata normalization** (`normalize_metadata_schemas`): rewrites the `ucp` property on every non-request schema to point at the root of `ucp.json`, and ensures `ucp.json` itself exposes a `oneOf` of the platform/business/response schemas — giving a unified `ucp` metadata field across the SDK.

If you're adding a new schema-level transform, it goes here. The pipeline is in three explicit passes (see `main()`): local flattening + variant discovery → transitive propagation → variant file emission. Order matters.

### Custom Jinja template

`templates/pydantic_v2/RootModel.jinja2` overrides the default `RootModel` template to force `model_config = ConfigDict(frozen=True, ...)` on all generated root models. Codegen is told to use this via `--custom-template-dir templates` in `generate_models.sh`. Other model kinds use the default templates shipped with `datamodel-code-generator`.

### Codegen flags worth knowing

The flags in `generate_models.sh` are load-bearing — changing them changes the public API:

- `--enum-field-as-literal all` — enums become `Literal[...]` types (no enum classes generated).
- `--allow-extra-fields` — generated models accept (and round-trip) unknown fields. UCP is designed for forward-compatibility.
- `--field-constraints` — regex / min / max from the JSON schemas are applied as Pydantic `Field(...)` constraints rather than as `Annotated[..., StringConstraints]`.
- `--use-schema-description` + `--use-field-description` — JSON `description` becomes Python docstrings.
- `--no-use-annotated` — avoid `Annotated[...]` in generated signatures.
- `--additional-imports pydantic.ConfigDict` — needed because the custom RootModel template emits `ConfigDict(...)`.

## Package layout

Public import surface (the only stable thing for consumers):

| Package                                 | Contents                                            |
| --------------------------------------- | --------------------------------------------------- |
| `ucp_sdk.models.schemas`                | Top-level: service, capability, payment_handler, ucp |
| `ucp_sdk.models.schemas.shopping`       | checkout, cart, order, payment + `*_request` variants |
| `ucp_sdk.models.schemas.shopping.types` | Reusable parts: items, totals, buyer, fulfillment, etc. |
| `ucp_sdk.models.schemas.transports`     | REST / MCP / embedded protocol bindings             |
| `ucp_sdk.models.schemas.common`         | Cross-cutting (e.g. `identity_linking`)             |

Files ending in `_create_request.py`, `_update_request.py`, `_complete_request.py` are **operation-specific variants** generated by the `ucp_request` marker mechanism described above — not hand-written.

## Lint, format, commits

- Ruff is the single linter + formatter. Config lives in `pyproject.toml` (line length 80, double quotes, Google docstring convention, isort settings).
- Pre-commit also runs prettier on `*.md`/`*.yaml`/`*.json`, shellcheck on shell scripts, and codespell (ignore list at `.codespellignore`).
- Conventional Commits are enforced on PRs (`.github/workflows/conventional-commits.yml`). PR titles + commit messages must follow `<type>: <subject>` (e.g. `feat:`, `fix:`, `chore:`, `docs:`).
- `.github/workflows/release.yml` is inherited from upstream and points at the official `ucp-sdk` PyPI project (trusted publishing). **It will not work from this fork** unless reconfigured — assume distribution to the downstream consumer happens via git dep, not PyPI, until told otherwise.
