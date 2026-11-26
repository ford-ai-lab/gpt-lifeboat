"""Turn an OpenAPI 3.x schema (as pasted into a Custom GPT action) into tool definitions.

Standard library only. YAML input needs PyYAML, which is optional.
"""

from __future__ import annotations

import copy
import json
import re

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
TOOL_NAME_MAX = 64
UNSUPPORTED = object()


class SchemaError(ValueError):
    """The action schema could not be read at all."""


def load_schema(raw):
    """Accept a dict, a JSON string or a YAML string and return a dict."""
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise SchemaError("action schema is empty")
    text = raw.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SchemaError(
                "action schema is not JSON. If it is YAML, install PyYAML "
                "(pip install pyyaml) or convert it to JSON first."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SchemaError("action schema is neither valid JSON nor valid YAML: %s" % exc) from exc
    if not isinstance(data, dict):
        raise SchemaError("action schema must be an object at the top level")
    return data


def _lookup(root, ref):
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve_refs(node, root, issues, _stack=()):
    """Inline local $ref pointers. Cycles and remote refs are cut and reported."""
    if isinstance(node, list):
        return [resolve_refs(item, root, issues, _stack) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if not ref.startswith("#/"):
            issues.append("Remote $ref not followed: %s (replaced with an untyped value)." % ref)
            return {"description": "Unresolved reference %s" % ref}
        if ref in _stack:
            return {"type": "object", "description": "Recursive reference to %s, not expanded." % ref}
        target = _lookup(root, ref)
        if target is None:
            issues.append("Broken $ref: %s (replaced with an untyped value)." % ref)
            return {"description": "Unresolved reference %s" % ref}
        return resolve_refs(target, root, issues, _stack + (ref,))
    return {key: resolve_refs(value, root, issues, _stack) for key, value in node.items()}


_DROP_KEYS = {"xml", "externalDocs", "discriminator", "example", "examples", "readOnly", "writeOnly", "deprecated"}


def to_json_schema(node):
    """Convert an OpenAPI 3.0 schema object into plain JSON Schema (nullable, vendor keys)."""
    if isinstance(node, list):
        return [to_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key in _DROP_KEYS or key == "nullable" or (isinstance(key, str) and key.startswith("x-")):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: to_json_schema(sub) for name, sub in value.items()}
        else:
            out[key] = to_json_schema(value)
    if node.get("nullable") is True and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def tool_name(candidate, taken):
    """Make a name every MCP client accepts: letters, digits, _ and -, at most 64 long, unique."""
    name = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", candidate or "")).strip("_") or "operation"
    name = name[:TOOL_NAME_MAX]
    base, n = name, 2
    while name in taken:
        suffix = "_%d" % n
        name = base[: TOOL_NAME_MAX - len(suffix)] + suffix
        n += 1
    taken.add(name)
    return name


def server_url(schema, issues):
    servers = schema.get("servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], dict):
        issues.append("The schema has no servers[0].url. Set the base URL environment variable before running the server.")
        return ""
    url = str(servers[0].get("url") or "")
    for var, spec in (servers[0].get("variables") or {}).items():
        default = str((spec or {}).get("default", ""))
        if "{%s}" % var in url:
            url = url.replace("{%s}" % var, default)
            issues.append("Server URL variable {%s} was filled with its default value %r." % (var, default))
    if len(servers) > 1:
        issues.append("The schema lists %d servers; only the first (%s) is used." % (len(servers), url))
    if not re.match(r"^https?://", url):
        issues.append("Server URL %r is not an absolute http(s) URL. Set the base URL environment variable." % url)
        return ""
    return url.rstrip("/")


def _pick_body(request_body, issues, label):
    """Return (content_type, schema, required) for the one body format the server can send."""
    if not isinstance(request_body, dict):
        return None
    content = request_body.get("content") or {}
    if not isinstance(content, dict) or not content:
        return None
    order = ["application/json", "application/x-www-form-urlencoded", "text/plain"]
    chosen = next((ct for ct in order if ct in content), None)
    if chosen is None:
        chosen = next((ct for ct in content if ct.endswith("+json")), None)
    if chosen is None:
        issues.append(
            "%s was NOT converted: request body type %s is not supported (JSON, form-encoded and plain text are)."
            % (label, ", ".join(sorted(content)))
        )
        return UNSUPPORTED
    schema = (content[chosen] or {}).get("schema") or {}
    return chosen, schema, bool(request_body.get("required"))


def extract_operations(schema, issues):
    """Return one tool definition per OpenAPI operation."""
    root = schema
    paths = schema.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SchemaError("action schema has no paths, so there is nothing to convert")
    if not str(schema.get("openapi", "")).startswith("3"):
        issues.append(
            "Schema version is %r; only OpenAPI 3.x is handled. Results may be incomplete."
            % (schema.get("openapi") or schema.get("swagger") or "unknown")
        )
    taken = set()
    operations = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        item = resolve_refs(item, root, issues) if "$ref" in item else item
        shared = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            label = "%s %s" % (method.upper(), path)
            picked = _pick_body(resolve_refs(op.get("requestBody"), root, issues), issues, label)
            if picked is UNSUPPORTED:
                continue
            op_id = op.get("operationId")
            fallback = "%s_%s" % (method, re.sub(r"[{}]", "", path))
            name = tool_name(op_id or fallback, taken)
            if not op_id:
                issues.append("%s has no operationId; the tool is named %s." % (label, name))
            elif name != op_id:
                issues.append(
                    "%s: operationId %r became tool name %r. Update any instruction that mentions the old name."
                    % (label, op_id, name)
                )

            properties, required, arg_map = {}, [], {}
            merged = {}
            for raw in list(shared) + list(op.get("parameters") or []):
                param = resolve_refs(raw, root, issues)
                if isinstance(param, dict) and param.get("name") and param.get("in"):
                    merged[(param["in"], param["name"])] = param
            for (location, pname), param in merged.items():
                if location == "cookie":
                    issues.append("%s: cookie parameter %r is not sent by the generated server." % (label, pname))
                    continue
                arg = pname if pname not in properties and pname != "body" else "%s_%s" % (location, pname)
                prop = to_json_schema(resolve_refs(param.get("schema") or {}, root, issues))
                if param.get("description") and "description" not in prop:
                    prop["description"] = param["description"]
                properties[arg] = prop
                arg_map[arg] = {"in": location, "name": pname}
                if param.get("required") or location == "path":
                    required.append(arg)

            body_type = None
            if picked:
                body_type, body_schema, body_required = picked
                properties["body"] = to_json_schema(resolve_refs(body_schema, root, issues)) or {}
                properties["body"].setdefault("description", "Request body (%s)." % body_type)
                if body_required:
                    required.append("body")

            if op.get("callbacks"):
                issues.append("%s: callbacks are not converted." % label)

            summary = (op.get("summary") or "").strip()
            description = (op.get("description") or "").strip()
            text = summary if not description else (description if not summary or summary in description else "%s. %s" % (summary.rstrip("."), description))
            input_schema = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = required
            operations.append(
                {
                    "tool": name,
                    "operation_id": op_id or "",
                    "method": method.upper(),
                    "path": path,
                    "description": text or "%s %s" % (method.upper(), path),
                    "input_schema": input_schema,
                    "arg_map": arg_map,
                    "body_type": body_type,
                    "read_only": method in ("get", "head", "options"),
                    "consequential": op.get("x-openai-isConsequential"),
                }
            )
    if not operations:
        raise SchemaError("no operation in the action schema could be converted")
    return operations


def security_from_schema(schema):
    """Best guess at the auth type from components.securitySchemes, in the converter's own vocabulary."""
    schemes = ((schema.get("components") or {}).get("securitySchemes")) or {}
    for spec in schemes.values():
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type")
        if kind == "oauth2" or kind == "openIdConnect":
            flows = spec.get("flows") or {}
            flow = flows.get("authorizationCode") or next(iter(flows.values()), {}) or {}
            return {
                "type": "oauth",
                "authorization_url": flow.get("authorizationUrl", ""),
                "token_url": flow.get("tokenUrl", ""),
                "scope": " ".join((flow.get("scopes") or {}).keys()),
            }
        if kind == "http":
            return {"type": "api_key", "auth_type": "basic" if spec.get("scheme") == "basic" else "bearer"}
        if kind == "apiKey":
            if spec.get("in") == "query":
                return {"type": "api_key", "auth_type": "query", "query_param": spec.get("name", "api_key")}
            return {"type": "api_key", "auth_type": "custom", "custom_header_name": spec.get("name", "X-Api-Key")}
    return None
