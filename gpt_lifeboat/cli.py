"""Command line: python3 -m gpt_lifeboat convert GPT.json -o OUT"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .convert import DefinitionError, convert


def _read(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def load_definition(args):
    """Build the definition from a JSON file, from separate pasted pieces, or both (flags win)."""
    definition = {}
    if args.definition:
        try:
            definition = json.loads(_read(args.definition))
        except json.JSONDecodeError as exc:
            raise DefinitionError("%s is not valid JSON: %s" % (args.definition, exc)) from exc
        if not isinstance(definition, dict):
            raise DefinitionError("%s must contain a JSON object" % args.definition)
    if args.name:
        definition["name"] = args.name
    if args.description:
        definition["description"] = args.description
    if args.instructions:
        definition["instructions"] = _read(args.instructions)
    if args.starter:
        definition["conversation_starters"] = args.starter
    if args.knowledge:
        definition["knowledge_files"] = args.knowledge
    if args.schema:
        auth = {"type": "none"}
        if args.auth:
            kind, _, header = args.auth.partition(":")
            auth = {"type": kind}
            if header:
                auth["custom_header_name"] = header
        definition.setdefault("actions", [])
        definition["actions"] = list(definition["actions"]) + [{"schema": _read(args.schema), "auth": auth if args.auth else None}]
    return definition


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gpt_lifeboat", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("convert", help="convert one GPT definition")
    c.add_argument("definition", nargs="?", help="GPT definition as JSON (see samples/), or - for stdin")
    c.add_argument("-o", "--out", required=True, help="output folder")
    c.add_argument("--name", help="GPT name")
    c.add_argument("--description", help="GPT description")
    c.add_argument("--instructions", metavar="FILE", help="text file with the GPT's instructions")
    c.add_argument("--starter", action="append", help="a conversation starter (repeatable)")
    c.add_argument("--knowledge", action="append", metavar="NAME", help="a knowledge file name (repeatable)")
    c.add_argument("--knowledge-dir", metavar="DIR", help="folder holding the knowledge files, to copy them into the skill")
    c.add_argument("--schema", metavar="FILE", help="OpenAPI action schema (JSON, or YAML if PyYAML is installed)")
    c.add_argument("--auth", help="auth type of the action: none, bearer, basic, custom:Header-Name or oauth")
    args = parser.parse_args(argv)

    try:
        if not args.definition and not (args.name and args.instructions):
            raise DefinitionError("give a definition file, or at least --name and --instructions")
        result = convert(load_definition(args), args.out, knowledge_dir=args.knowledge_dir)
    except (DefinitionError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print("Converted to %s" % result["out_dir"])
    print("  skill/%s/SKILL.md" % result["slug"])
    print("  system-prompt.txt")
    if result["tools"]:
        print("  mcp-server/ with %d tool(s): %s" % (len(result["tools"]), ", ".join(result["tools"])))
    print("  MIGRATION_REPORT.md  <- read this; it lists what was not converted")
    return 0
