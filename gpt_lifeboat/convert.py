"""Convert a Custom GPT definition into a Claude skill, an MCP server, a system prompt and a report."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import __version__
from .openapi import SchemaError, extract_operations, load_schema, security_from_schema, server_url

TEMPLATES = Path(__file__).resolve().parent / "templates"
DESCRIPTION_MAX = 1024  # Agent Skills specification
NAME_MAX = 64
SKILL_BODY_SOFT_LIMIT = 500  # lines; the specification recommends staying under this

# ChatGPT built-in tools that have no automatic equivalent after conversion.
CAPABILITY_NOTES = {
    "web_search": "Web search (browsing) is a ChatGPT built-in. The skill only has it if the host you load it into provides a search tool.",
    "browsing": "Web search (browsing) is a ChatGPT built-in. The skill only has it if the host you load it into provides a search tool.",
    "code_interpreter": "Code Interpreter is a ChatGPT built-in. Claude Code and claude.ai can run code, but files and libraries differ; test any step that relies on it.",
    "image_generation": "Image generation (DALL-E / GPT Image) is a ChatGPT built-in and is not converted. Remove those steps or connect an image tool yourself.",
    "dalle": "Image generation (DALL-E / GPT Image) is a ChatGPT built-in and is not converted. Remove those steps or connect an image tool yourself.",
    "canvas": "Canvas is a ChatGPT interface feature and is not converted.",
}
INSTRUCTION_HINTS = [
    (r"dall[\s·.\-]?e|image generation|generate an image", "mentions image generation"),
    (r"code interpreter|python tool|data analysis tool", "mentions Code Interpreter"),
    (r"\bbrows(e|ing)\b|web search|search the web", "mentions web browsing"),
    (r"\bcanvas\b", "mentions Canvas"),
    (r"knowledge (file|base)|uploaded file|myfiles_browser", "mentions knowledge files"),
    (r"\bGPT\b|ChatGPT", "refers to itself as a GPT or to ChatGPT"),
]
SECRET_KEYS = {"api_key", "key", "client_secret", "secret", "token", "access_token", "password"}


class DefinitionError(ValueError):
    """The GPT definition cannot be converted as given."""


def slugify(text, fallback="converted-gpt"):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:NAME_MAX].strip("-")
    return slug or fallback


def yaml_string(text):
    """A double-quoted YAML scalar. JSON string syntax is valid YAML for single-line text."""
    return json.dumps(" ".join(str(text).split()), ensure_ascii=False)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalise(definition):
    """Validate the input and return it in one predictable shape."""
    if not isinstance(definition, dict):
        raise DefinitionError("the GPT definition must be a JSON object")
    name = str(definition.get("name") or "").strip()
    instructions = str(definition.get("instructions") or "").strip()
    if not name:
        raise DefinitionError('the GPT definition needs a "name"')
    if not instructions:
        raise DefinitionError('the GPT definition needs "instructions" (paste them from the GPT builder\'s Configure tab)')
    knowledge = []
    for item in _as_list(definition.get("knowledge_files")):
        if isinstance(item, str):
            knowledge.append({"name": item, "description": ""})
        elif isinstance(item, dict) and item.get("name"):
            knowledge.append({"name": str(item["name"]), "description": str(item.get("description") or "")})
    capabilities = definition.get("capabilities") or []
    if isinstance(capabilities, dict):
        capabilities = [key for key, on in capabilities.items() if on]
    actions = _as_list(definition.get("actions"))
    if definition.get("action_schema") is not None:  # shorthand for one action
        actions.append({"schema": definition["action_schema"], "auth": definition.get("auth")})
    return {
        "name": name,
        "description": str(definition.get("description") or "").strip(),
        "instructions": instructions,
        "conversation_starters": [str(s).strip() for s in _as_list(definition.get("conversation_starters")) if str(s).strip()],
        "knowledge_files": knowledge,
        "capabilities": [str(c).lower() for c in capabilities],
        "actions": [a for a in actions if isinstance(a, dict) and a.get("schema") is not None],
    }


def _clean_auth(raw, schema, notes, label):
    """Normalise the auth block, drop any secret values, and explain what the user has to supply."""
    auth = dict(raw) if isinstance(raw, dict) else None
    if isinstance(raw, str):
        auth = {"type": raw}
    if auth is None:
        auth = security_from_schema(schema)
        if auth:
            notes.append("%s: no auth type was given, so it was read from the schema's securitySchemes (%s). Check it." % (label, auth["type"]))
        else:
            auth = {"type": "none"}
    leaked = sorted(key for key in auth if key.lower() in SECRET_KEYS and auth[key])
    for key in leaked:
        auth.pop(key)
    if leaked:
        notes.append(
            "%s: the input contained a secret value (%s). It was NOT copied into any output file. "
            "Put it in the environment variable instead, and consider rotating it since it has been in a plain file." % (label, ", ".join(leaked))
        )
    kind = str(auth.get("type") or "none").lower().replace("-", "_").replace("apikey", "api_key")
    if kind in ("bearer", "basic", "custom"):
        auth["auth_type"], kind = kind, "api_key"
    if kind not in ("none", "api_key", "oauth"):
        notes.append("%s: unknown auth type %r, treated as none." % (label, auth.get("type")))
        kind = "none"
    clean = {"type": kind}
    if kind == "api_key":
        clean["auth_type"] = str(auth.get("auth_type") or "bearer").lower()
        if clean["auth_type"] == "custom":
            clean["custom_header_name"] = auth.get("custom_header_name") or "X-Api-Key"
        if clean["auth_type"] == "query":
            clean["query_param"] = auth.get("query_param") or "api_key"
    if kind == "oauth":
        for key in ("authorization_url", "token_url", "scope", "token_exchange_method"):
            if auth.get(key):
                clean[key] = auth[key]
    return clean


def build_actions(gpt, notes):
    """Parse every action schema. Returns the operations.json content, or None when there are no actions."""
    if not gpt["actions"]:
        return None
    config = {"server_name": slugify(gpt["name"]), "generated_by": "gpt-lifeboat %s" % __version__, "actions": [], "operations": []}
    taken = set()
    for index, action in enumerate(gpt["actions"], start=1):
        label = "Action %d" % index
        try:
            schema = load_schema(action["schema"])
            issues = []
            operations = extract_operations(schema, issues)
        except SchemaError as exc:
            notes.append("%s was NOT converted: %s" % (label, exc))
            continue
        prefix = "ACTION%d" % index
        url = server_url(schema, issues)
        auth = _clean_auth(action.get("auth"), schema, notes, label)
        title = ((schema.get("info") or {}).get("title")) or url or label
        config["actions"].append(
            {
                "title": title,
                "base_url": url,
                "auth": auth,
                "env": {"base_url": prefix + "_BASE_URL", "api_key": prefix + "_API_KEY", "access_token": prefix + "_ACCESS_TOKEN"},
            }
        )
        for op in operations:
            original = op["tool"]
            name, n = original, 2
            while name in taken:
                name = "%s_%d" % (original[:60], n)
                n += 1
            if name != original:
                issues.append("Tool %s exists in more than one action; this one is named %s." % (original, name))
            taken.add(name)
            op["tool"] = name
            op["action"] = len(config["actions"]) - 1
            if op.get("consequential") is True:
                op["description"] += " (Marked consequential in the original GPT: confirm with the user before calling.)"
            config["operations"].append(op)
        notes.extend("%s: %s" % (label, issue) for issue in dict.fromkeys(issues))
        if auth["type"] == "oauth":
            notes.append(
                "%s uses OAuth. The sign-in flow is NOT converted: ChatGPT ran it for you and held the tokens. "
                "The generated server sends whatever access token is in %s_ACCESS_TOKEN and does not refresh it. "
                "For more than personal use you need a real OAuth client; your client ID and secret are not in any output."
                % (label, prefix)
            )
        if action.get("privacy_policy_url"):
            notes.append("%s: privacy policy URL %s is not used by the outputs; keep it if you publish the replacement." % (label, action["privacy_policy_url"]))
    if not config["operations"]:
        return None
    return config


def skill_description(gpt):
    base = gpt["description"] or "Converted from the Custom GPT %s." % gpt["name"]
    if not base.endswith((".", "!", "?")):
        base += "."
    when = " Use when the user asks for what the %s GPT did" % gpt["name"]
    if gpt["conversation_starters"]:
        when += ', for example: "%s"' % gpt["conversation_starters"][0].rstrip(".")
    text = " ".join((base + when + ".").split())
    return text if len(text) <= DESCRIPTION_MAX else text[: DESCRIPTION_MAX - 3].rstrip() + "..."


def _tools_table(config):
    lines = ["| Tool | Request | What it does |", "| --- | --- | --- |"]
    for op in config["operations"]:
        what = " ".join(op["description"].split()).replace("|", "\\|")
        lines.append("| `%s` | `%s %s` | %s |" % (op["tool"], op["method"], op["path"], what))
    return "\n".join(lines)


def render_skill(gpt, slug, config, copied):
    out = ["---", "name: %s" % slug, "description: %s" % yaml_string(skill_description(gpt)), "metadata:",
           "  converted-from: %s" % yaml_string("Custom GPT: " + gpt["name"]),
           "  converter: %s" % yaml_string("gpt-lifeboat " + __version__), "---", "",
           "# %s" % gpt["name"], ""]
    if gpt["description"]:
        out += [gpt["description"], ""]
    out += ["## Instructions", "", gpt["instructions"], ""]
    if gpt["conversation_starters"]:
        out += ["## Example requests", "", "Requests this skill was built to answer:", ""]
        out += ["- %s" % s for s in gpt["conversation_starters"]] + [""]
    if gpt["knowledge_files"]:
        out += ["## Reference material", "",
                "The original GPT answered from uploaded knowledge files. They are listed in "
                "[references/knowledge-files.md](references/knowledge-files.md). Read the relevant file before answering "
                "a question it covers. If a listed file is not present, say so instead of answering from memory.", ""]
    if config:
        out += ["## Tools", "",
                "The original GPT called an external API through actions. Those calls are now MCP tools with the names below "
                "(details in [references/actions.md](references/actions.md)). If the tools are not available in this "
                "session, tell the user the connection is not set up; do not invent results.", "",
                _tools_table(config), ""]
    return "\n".join(out)


def render_knowledge(gpt, copied):
    out = ["# Knowledge files", "", "Files the original GPT had uploaded as knowledge.", ""]
    for item in gpt["knowledge_files"]:
        where = "[knowledge/%s](knowledge/%s)" % (item["name"], item["name"].replace(" ", "%20")) if item["name"] in copied else "NOT INCLUDED: add it to `references/knowledge/`"
        note = " - %s" % item["description"] if item["description"] else ""
        out.append("- `%s`%s (%s)" % (item["name"], note, where))
    return "\n".join(out) + "\n"


def render_actions_reference(config):
    out = ["# Actions (now MCP tools)", "", "Generated from the GPT's OpenAPI action schema. One tool per operation.", ""]
    for index, action in enumerate(config["actions"]):
        out += ["## %s" % action["title"], "", "- Base URL: `%s`" % (action["base_url"] or "set " + action["env"]["base_url"]),
                "- Authentication: %s" % action["auth"]["type"], ""]
        for op in (o for o in config["operations"] if o["action"] == index):
            out += ["### `%s`" % op["tool"], "", "`%s %s`" % (op["method"], op["path"]), "", op["description"], "",
                    "Arguments:", "", "```json", json.dumps(op["input_schema"], indent=2), "```", ""]
    return "\n".join(out)


def render_system_prompt(gpt, config):
    out = [gpt["instructions"], ""]
    if gpt["knowledge_files"]:
        out += ["---", "Reference files", "",
                "You were originally given these files. Use them when they are attached or pasted into the conversation; "
                "if one is needed and missing, ask for it.", ""]
        out += ["- %s%s" % (k["name"], ": " + k["description"] if k["description"] else "") for k in gpt["knowledge_files"]] + [""]
    if config:
        out += ["---", "Tools", "",
                "You may have these tools. Call them by these names. If a tool is not available, say so; never make up its result.", ""]
        out += ["- %s: %s" % (op["tool"], " ".join(op["description"].split())) for op in config["operations"]] + [""]
    return "\n".join(out)


def render_env_example(config):
    out = ["# Copy to .env (never commit the copy) or set these in your MCP client's configuration.", ""]
    for action in config["actions"]:
        env, auth = action["env"], action["auth"]
        out.append("# %s" % action["title"])
        out.append("%s%s=%s" % ("" if not action["base_url"] else "# ", env["base_url"], action["base_url"] or "https://api.example.com"))
        if auth["type"] == "api_key":
            how = {"bearer": "sent as Authorization: Bearer <key>", "basic": "sent as Authorization: Basic <key>; give user:password or the base64 value",
                   "query": "sent as query parameter %s" % auth.get("query_param"), "custom": "sent in header %s" % auth.get("custom_header_name")}[auth["auth_type"]]
            out += ["# API key, %s" % how, "%s=" % env["api_key"]]
        elif auth["type"] == "oauth":
            out += ["# OAuth access token, sent as Authorization: Bearer <token>. Not refreshed automatically.", "%s=" % env["access_token"]]
        out.append("")
    return "\n".join(out)


def render_server_readme(gpt, config):
    env_lines = []
    for action in config["actions"]:
        if not action["base_url"]:
            env_lines.append("- `%s`: the API's base URL (the schema did not give one)" % action["env"]["base_url"])
        if action["auth"]["type"] == "api_key":
            env_lines.append("- `%s`: API key for %s" % (action["env"]["api_key"], action["title"]))
        if action["auth"]["type"] == "oauth":
            env_lines.append("- `%s`: an OAuth access token for %s (you obtain and refresh it yourself)" % (action["env"]["access_token"], action["title"]))
    return "\n".join([
        "# MCP server for %s" % gpt["name"], "",
        "Generated by gpt-lifeboat from the GPT's action schema. %d tool(s), listed in `operations.json`." % len(config["operations"]), "",
        "## Run", "", "```", 'pip install "mcp>=2,<3"', "cp .env.example .env   # then fill it in", "python server.py --check",
        "python server.py          # stdio", "python server.py --http   # Streamable HTTP on http://127.0.0.1:8000/mcp", "```", "",
        "## Credentials", "", "\n".join(env_lines) or "None needed: the action had no authentication.", "",
        "Each `ACTIONn_BASE_URL` can also be set to override the address from the schema.", "",
        "## Connect a client", "",
        "`mcp.json.example` is the `mcpServers` entry used by Claude Desktop (`claude_desktop_config.json`) and "
        "Claude Code (`.mcp.json`). Replace the path and add your variables.", "",
        "## Limits", "",
        "- The HTTP mode has no authentication. Do not expose it to the internet as it is.",
        "- OAuth sign-in is not implemented; see MIGRATION_REPORT.md.",
        "- Responses are returned as text and cut at 100,000 characters.", ""])


def render_report(gpt, slug, config, notes, copied):
    out = ["# Migration report: %s" % gpt["name"], "", "Produced by gpt-lifeboat %s. Nothing here has been tested against your real API." % __version__, "",
           "## What was produced", "",
           "- `skill/%s/` - Claude skill (SKILL.md plus references)" % slug,
           "- `system-prompt.txt` - the same behaviour as a plain prompt for any model"]
    out.append("- `mcp-server/` - MCP server with %d tool(s)" % len(config["operations"]) if config else "- No MCP server: the GPT had no convertible actions")
    out += ["", "## Not converted, or needs your attention", ""]
    items = list(notes)
    for cap in dict.fromkeys(gpt["capabilities"]):
        if cap in CAPABILITY_NOTES and CAPABILITY_NOTES[cap] not in items:
            items.append(CAPABILITY_NOTES[cap])
    missing = [k["name"] for k in gpt["knowledge_files"] if k["name"] not in copied]
    if missing:
        items.append("Knowledge files are listed but their contents were not available to the converter: %s. "
                     "Copy them into `skill/%s/references/knowledge/` (re-run with --knowledge-dir to do it automatically)." % (", ".join(missing), slug))
    binary = [n for n in copied if not n.lower().endswith((".md", ".txt", ".csv", ".json", ".html", ".yaml", ".yml"))]
    if binary:
        items.append("Copied as they are, not converted to text: %s. A skill reads text best; convert PDFs and Office files if answers look thin." % ", ".join(binary))
    lines = gpt["instructions"].splitlines()
    for pattern, meaning in INSTRUCTION_HINTS:
        hits = [str(i) for i, line in enumerate(lines, start=1) if re.search(pattern, line, re.I)]
        if hits:
            items.append("Instructions line %s: %s. Reword for the new host if needed." % (", ".join(hits[:8]), meaning))
    if len(lines) > SKILL_BODY_SOFT_LIMIT:
        items.append("The instructions are %d lines; the skill specification recommends under %d in SKILL.md. Consider moving detail into reference files." % (len(lines), SKILL_BODY_SOFT_LIMIT))
    if config:
        items.append("The MCP server's HTTP mode has no authentication of its own. Keep it local or put it behind a gateway.")
    items += [
        "The skill description decides when the skill is picked. It was assembled from the GPT's description and first conversation starter: read it and rewrite it in your own words.",
        "Conversation starters became an 'Example requests' list; skills and prompts have no clickable starters.",
        "The GPT's model choice, sharing settings, profile picture, usage counts and existing conversations are not part of this conversion.",
    ]
    out += ["- %s" % item for item in items]
    out += ["", "## Before you rely on it", "",
            "1. Run three prompts you know well through the original GPT and the replacement, and compare.",
            "2. If there is an MCP server: `python server.py --check`, then call one read-only tool with real credentials.", ""]
    return "\n".join(out)


def convert(definition, out_dir, knowledge_dir=None):
    """Write all outputs for one GPT definition into out_dir. Returns a summary dict."""
    gpt = normalise(definition)
    slug = slugify(gpt["name"])
    out_dir = Path(out_dir)
    notes = []
    config = build_actions(gpt, notes)

    skill_dir = out_dir / "skill" / slug
    refs = skill_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    copied = []
    if knowledge_dir and gpt["knowledge_files"]:
        for item in gpt["knowledge_files"]:
            source = Path(knowledge_dir) / Path(item["name"]).name
            if source.is_file():
                (refs / "knowledge").mkdir(exist_ok=True)
                shutil.copyfile(source, refs / "knowledge" / source.name)
                copied.append(item["name"])
    (skill_dir / "SKILL.md").write_text(render_skill(gpt, slug, config, copied), encoding="utf-8")
    if gpt["knowledge_files"]:
        (refs / "knowledge-files.md").write_text(render_knowledge(gpt, copied), encoding="utf-8")
    if config:
        (refs / "actions.md").write_text(render_actions_reference(config), encoding="utf-8")
    if not any(refs.iterdir()):
        refs.rmdir()

    (out_dir / "system-prompt.txt").write_text(render_system_prompt(gpt, config), encoding="utf-8")

    if config:
        server_dir = out_dir / "mcp-server"
        server_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / "server.py", server_dir / "server.py")
        (server_dir / "operations.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        (server_dir / "requirements.txt").write_text("mcp>=2,<3\n", encoding="utf-8")
        (server_dir / ".env.example").write_text(render_env_example(config), encoding="utf-8")
        (server_dir / "README.md").write_text(render_server_readme(gpt, config), encoding="utf-8")
        env = {}
        for action in config["actions"]:
            if action["auth"]["type"] != "none":
                env[action["env"]["access_token" if action["auth"]["type"] == "oauth" else "api_key"]] = "PUT-YOUR-VALUE-HERE"
        example = {"mcpServers": {slug: {"command": "python3", "args": ["/absolute/path/to/mcp-server/server.py"], "env": env}}}
        (server_dir / "mcp.json.example").write_text(json.dumps(example, indent=2) + "\n", encoding="utf-8")

    (out_dir / "MIGRATION_REPORT.md").write_text(render_report(gpt, slug, config, notes, copied), encoding="utf-8")
    return {"slug": slug, "out_dir": str(out_dir), "tools": [op["tool"] for op in config["operations"]] if config else [], "notes": notes}
