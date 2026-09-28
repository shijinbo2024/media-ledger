# MediaLedger — Token-Saving Multimodal Generation MCP

English | [中文](README.md)

> Chinese generation request → template-skeleton matching (zero LLM) → ready-to-call
> payload for a generation API. Instant hits, deterministic, assertable scoring;
> out-of-ledger requests fall back honestly — never guesses (FAR=0).

## Features

- **Zero dependencies**: pure Python standard library (3.8+), clone and run
- **Skeleton matching**: verbatim wording, slot-filled parameters; bigram fuzzy recall
  on miss; ambiguous ties are never guessed
- **Two execution modes**: default `dry` returns the payload only (free); `--call`
  actually invokes the Ark generation API (key read only from the `ARK_API_KEY`
  environment variable, never persisted); identical payloads reuse cached results
- **teach channel**: injection gate + sandboxed self-check + atomic ledger write,
  effective in seconds
- **Self-test page**: `index.html` is fully static — just open it (embedded JS engine)

## Quick Start

```bash
python runtime_media.py --ask "生成一张1080x1920的赛博朋克风格城市夜景封面图"  # dry payload
python runtime_media.py --ask "画一个卡通的橘猫方形头像" --call              # live call (needs ARK_API_KEY)
python runtime_media.py --teach-file my_recipe.json                         # teach a new recipe
python runtime_media.py --list | --stats | --selftest                       # list / stats / acceptance self-test
start index.html   # Windows; macOS: open index.html — static self-test panel
```

## MCP Deployment

Zero-dependency stdio server, works with any MCP client. Add to your client config
(e.g. Claude Desktop's `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "media_ledger": {
      "command": "python",
      "args": ["/path/to/media-ledger/mcp_server_media.py"]
    }
  }
}
```

Other clients (Cline / Cursor etc.): add a stdio server manually with
`command = python`, `args = ["/path/to/media-ledger/mcp_server_media.py"]`.

After connecting, `tools/list` exposes three tools:

| Tool | Input | Behavior |
|---|---|---|
| `ask_media` | `question` | dry payload (deterministic, auditable, free) |
| `teach_media` | `question, code, tests, patterns?` | teach: written to ledger only after injection gate + scored self-check |
| `stats_media` | – | ledger statistics |

The MCP channel returns payloads only; use CLI `--call` for live generation.
UTF-8 reconfiguration for Windows is built in.

### Making the agent actually call it

MCP tools are model-invoked, not auto-invoked — some harnesses won't trigger them
by default. This server ships protocol-level `instructions` plus trigger wording in
the tool description; to enforce the workflow, paste the following into your rules
file (`CLAUDE.md` / `AGENTS.md` / `.cursorrules`):

```text
Whenever a task involves constructing an image/video generation API request: call
the MCP tool media_ledger's ask_media first (input = the user's request verbatim)
and use the returned payload on a hit — never hand-write the request body. On a
miss (near_miss/fallback) say so honestly and proceed the usual way. ask_media
returns a payload only; for live generation use CLI: python runtime_media.py --ask "..." --call.
```

## Self-Test Web Page (fully static)

Double-click `index.html` — no server, no network needed:

- **Acceptance self-test**: computed in-browser by the embedded JS engine
  (seed hits + scoring / out-of-ledger fallback / parameter generalization);
  the teach-gate and MCP-smoke checks are CLI-only and shown as SKIP
- **Live API test**: fill in one generic API Key to call the generation API directly
  from the browser (if blocked by CORS, use CLI `--call` instead); the key stays
  in page memory only and is never persisted

## The ledger: generative and growable

The ledger (`state/media_ledger.json`) is not a hardcoded config — it grows with use:

- **Generative**: recipe templates are not hand-written — teach one real request
  sentence (with literal parameter values) + a one-line function + ≥2 assertion cases,
  and the template skeleton is auto-derived and committed in seconds after the scored
  self-check and injection gate. It bootstraps from an empty ledger
  (teach one, hit one).
- **Growable**: add new recipes anytime; keep appending wording variants to existing
  recipes so different phrasings all hit. Pure-JSON storage with atomic writes —
  versionable, portable, rollback-friendly.

Entry points: CLI `python runtime_media.py --teach-file my_recipe.json` /
`--add-pattern-file`; the MCP `teach_media` tool works the same way.

## Honest Boundaries

- Saves orchestration tokens and wasted generation retries; **not a replacement for
  the generation model itself**. Scoring guarantees payload correctness only
- Matching is wording-sensitive; generalization comes from wording families plus
  fuzzy recall (thresholded) — beyond that it falls back honestly
- `ARK_API_KEY` is read from the environment only; never written to disk or ledgers

## Layout

```
media-ledger/
  runtime_media.py     runtime (skeleton matching / sandbox / teach gate / selftest CLI)
  mcp_server_media.py  MCP stdio server (ask_media / teach_media / stats_media)
  adapters.py          Ark adapter + payload-hash cache (urllib; extensible to other multimodal generation models)
  index.html           self-test page (fully static)
  state/media_ledger.json  recipe ledger (13 seeds + teach increments)
  tests/test_selftest.py   acceptance criteria (--selftest green)
  benchmark/           100x-scale benchmark (generator / runner / live sampling + report)
```

## License

MIT
