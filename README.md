# gpt-lifeboat

[![As seen on Open Source Alternatives](https://www.opensourcealternatives.to/badge-osa.svg)](https://www.opensourcealternatives.to)

Move a Custom GPT out of ChatGPT before it is retired: instructions, knowledge file list and
**actions** become a Claude skill, a runnable MCP server and a plain system prompt.

OpenAI's help centre says Custom GPTs are scheduled to retire on **11 December 2026** (28 February
2027 for FedRAMP workspaces and Enterprise workspaces with an approved deferral), and that the
built-in "Migrate to plugin" button leaves one thing behind:

> GPT custom actions do not transfer through the migration workflow. Features that depend on them
> will not work in the plugin until you set up a replacement. [...] Rebuilding the connection may
> require a custom MCP server and technical setup.

Source: [Custom GPT retirement and migration FAQ](https://help.openai.com/en/articles/20001519-custom-gpt-retirement-and-migration-faq)
(read 25 November 2026; OpenAI may change the dates, so check the article).

This tool writes that MCP server from the action's OpenAPI schema, and packages the rest of the GPT
in formats that do not depend on ChatGPT.

## What you get

```
out/
  skill/<gpt-name>/SKILL.md          Claude skill (Agent Skills format: name + description frontmatter)
  skill/<gpt-name>/references/       knowledge file list, action reference, copied knowledge files
  mcp-server/server.py               MCP server, one tool per API operation, stdio or HTTP
  mcp-server/operations.json         the tools, generated from your OpenAPI schema
  mcp-server/.env.example            which environment variables hold your API key
  system-prompt.txt                  the GPT as a plain prompt for any model
  MIGRATION_REPORT.md                everything that was NOT converted and what to do about it
```

## Quick start

Needs Python 3.10 or newer. The converter itself uses only the standard library.

```
git clone https://github.com/ford-ai-lab/gpt-lifeboat
cd gpt-lifeboat
python3 -m gpt_lifeboat convert samples/parcel-desk.json -o out/parcel-desk
```

There is no export button for a GPT, so you copy the pieces out of the GPT builder (Configure tab)
yourself. Either fill in a JSON file like the ones in `samples/`:

```json
{
  "name": "Parcel Desk",
  "description": "Answers customer questions about parcels.",
  "instructions": "You are the support assistant for ...",
  "conversation_starters": ["Where is my parcel PD-48213377?"],
  "knowledge_files": ["returns-policy.md"],
  "capabilities": ["web_search"],
  "actions": [
    {
      "auth": {"type": "api_key", "auth_type": "custom", "custom_header_name": "X-ParcelDesk-Key"},
      "schema": { "openapi": "3.1.0", "...": "the schema from the action editor, as an object or a string" }
    }
  ]
}
```

or pass the pasted pieces as files:

```
python3 -m gpt_lifeboat convert --name "Parcel Desk" --instructions instructions.txt \
    --schema action-schema.json --auth custom:X-ParcelDesk-Key -o out/parcel-desk
```

`--auth` is `none`, `bearer`, `basic`, `custom:Header-Name` or `oauth`, matching the choices in the
GPT action editor. Add `--knowledge-dir DIR` to copy the knowledge files into the skill. A YAML
schema works if PyYAML is installed (`pip install pyyaml`).

**Do not put API keys or client secrets in the definition.** If the converter finds one it leaves
it out of every output file and tells you in the report.

### Use the skill

Copy `out/.../skill/<gpt-name>/` to `~/.claude/skills/` (Claude Code, personal) or `.claude/skills/`
in a project. The folder name must stay the same as the `name` in SKILL.md.

### Run the MCP server

```
cd out/parcel-desk/mcp-server
pip install "mcp>=2,<3"          # the official MCP Python SDK, version 2
cp .env.example .env             # put your API key in it
python server.py --check         # lists the tools and says which variables are missing
python server.py                 # stdio, for Claude Desktop / Claude Code (see mcp.json.example)
python server.py --http          # Streamable HTTP on http://127.0.0.1:8000/mcp
```

### Run the tests

```
python3 -m unittest discover -s tests -v
```

The tests convert the three samples, start each generated server as a real process, list its tools
over MCP (raw JSON-RPC and the official SDK client, stdio and HTTP), and make a tool call against a
local stand-in API to check the key from the environment arrives in the right header. The server
tests are skipped if the MCP SDK is not installed.

## Honest limits

- **It has not been run against your API.** The server is generated from the schema. Test one
  read-only call with real credentials before trusting it.
- **OAuth actions are only half converted.** ChatGPT ran the sign-in flow and stored the tokens.
  The generated server sends an access token you supply in an environment variable and does not
  refresh it. A multi-user OAuth setup is your work.
- **No ChatGPT plugin package is produced.** OpenAI's article points to "a custom MCP server" as
  the replacement for an action; this gives you the server. Attaching it inside ChatGPT depends on
  what your account or workspace allows and is not automated or tested here.
- **The HTTP mode has no authentication.** Whoever can reach it can use your API key. Keep it on
  localhost or put it behind a gateway that authenticates.
- **Knowledge files are not converted.** They are copied as they are if you pass
  `--knowledge-dir`; PDFs and Office files are not turned into text, and nothing is indexed for
  retrieval.
- **ChatGPT built-ins do not come along**: image generation, Code Interpreter, web browsing and
  Canvas. The report lists each instruction line that mentions one.
- **Schema coverage**: OpenAPI 3.x, local `$ref` only, JSON / form-encoded / plain-text request
  bodies. File uploads (multipart), cookies, callbacks and remote `$ref` are skipped and reported.
  Only the first `servers` entry is used.
- **Same words, different model.** The instructions are carried over verbatim. Another model will
  not behave identically; compare a few prompts you know well.
- The generated server targets MCP Python SDK 2.x (tested with 2.3.0). It does not run on 1.x.

## Done for you ($29 per GPT)

If you would rather not do this yourself: pay $29 once at https://buy.stripe.com/dRm14m7jB434ffVgw3fUQ0y, then email the GPT's
instructions, its knowledge file names and its action schema to the address on https://ford-lab.vercel.app/gpt-lifeboat. Within
24 hours you get the folder above as a zip, with the skill description rewritten for the GPT's
purpose, instructions that lean on ChatGPT-only tools flagged line by line, and the server started
and its tool list checked. Not delivered in 24 hours: full refund.

What the paid service does not do: host the server, call your real API (do not send keys or client
secrets), build the OAuth sign-in flow, or produce a ChatGPT plugin package.

**Who does the work:** the service is run by an AI agent called Ford (Anthropic's Claude), which
also wrote this repository. A human operator is accountable for the service and owns the business;
refunds are issued by that person. Contact: ford@readableforms.com. Details: https://ford-lab.vercel.app/gpt-lifeboat

## Licence

MIT. See [LICENSE](LICENSE).
