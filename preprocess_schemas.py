# Copyright 2026 UCP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import copy
from pathlib import Path
import sys

# --- I/O Helpers ---


def load_json(path):
    """Loads JSON data from a file."""
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)


def save_json(data, path):
    """Saves data to a JSON file with standard indentation."""
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# --- Traversal Helper ---


def iter_nodes(root):
    """
    Iteratively yields all dictionary or list nodes in a JSON tree.
    This replaces multiple manual stack-walking implementations.
    """
    stack = [root]
    visited = {id(root)}
    while stack:
        curr = stack.pop()
        yield curr

        # Identify children for the next iteration
        children = []
        if isinstance(curr, dict):
            children = curr.values()
        elif isinstance(curr, list):
            children = curr

        for child in children:
            if isinstance(child, (dict, list)) and id(child) not in visited:
                visited.add(id(child))
                stack.append(child)


# --- Reference Resolution ---


def resolve_local_ref(ref, root):
    """
    Resolves a local JSON pointer (e.g., #/$defs/name) within the same document.
    Returns the resolved schema fragment or None if invalid.
    """
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None

    parts = ref.split("/")
    current = root
    for part in parts[1:]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            if 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        else:
            return None
    return current


# --- Schema Normalization and Flattening ---


def _process_all_of_item(item, node, root, state):
    """Processes a single allOf item and updates the shared merge state."""
    # Resolve local references (internal inheritance) before merging
    if isinstance(item, dict) and "$ref" in item:
        resolved = resolve_local_ref(item["$ref"], root)
        if resolved:
            item = copy.deepcopy(resolved)
        else:
            # Keep external refs; we'll handle them in variant generation if needed
            state["remaining_refs"].append(item)
            return

    if not isinstance(item, dict):
        return

    # Extract polymorphic branches (anyOf, oneOf) to keep the node flat
    for poly_key in ["anyOf", "oneOf"]:
        if poly_key in item:
            state["poly_branches"].setdefault(poly_key, []).extend(
                item.pop(poly_key)
            )

    # Merge core property definitions and requirements
    if "properties" in item:
        state["merged_properties"].update(item["properties"])
    if "required" in item:
        for req in item["required"]:
            if req not in state["merged_required"]:
                state["merged_required"].append(req)

    # Carry over generic metadata (title, description, etc.) if not defined in the base
    reserved_keys = {
        "properties",
        "required",
        "allOf",
        "$ref",
        "anyOf",
        "oneOf",
    }
    for k, v in item.items():
        if k not in reserved_keys and k not in node:
            node[k] = v


def _apply_merged_state(node, state):
    """Applies accumulated properties and branches to the node."""
    if state["merged_properties"]:
        node.setdefault("properties", {}).update(state["merged_properties"])

    if state["merged_required"]:
        existing = node.setdefault("required", [])
        for r in state["merged_required"]:
            if r not in existing:
                existing.append(r)

    # Re-insert any combined polymorphic branches
    for k, branches in state["poly_branches"].items():
        node.setdefault(k, []).extend(branches)

    # If some refs couldn't be resolved locally, put them back into a slim allOf
    if state["remaining_refs"]:
        node["allOf"] = state["remaining_refs"]


def merge_all_of_to_node(node, root):
    """
    Merges 'allOf' components into the node itself.

    RATIONALE: Code generators (like datamodel-codegen) often create cleaner Pydantic
    models if inheritance is flattened at the schema level rather than relying on
    complex 'allOf' chains which can lead to redundant intermediate classes.
    """
    if "allOf" not in node:
        return

    all_of_sources = node.pop("allOf")
    state = {
        "merged_properties": {},
        "merged_required": [],
        "poly_branches": {},
        "remaining_refs": [],
    }

    for item in all_of_sources:
        _process_all_of_item(item, node, root, state)

    _apply_merged_state(node, state)


def distribute_properties_to_branches(node):
    """
    Inherits base properties/requirements into anyOf/oneOf branches.

    RATIONALE: This ensures that each branch of a union is a self-contained, valid
    model in Pydantic. Without this, a generated union model might miss required
    common fields if it's treated as a pure 'oneOf' alternative.
    """
    if "properties" not in node:
        return

    base_props = node["properties"]
    base_req = node.get("required", [])
    base_type = node.get("type")

    for poly_key in ["anyOf", "oneOf"]:
        if poly_key not in node:
            continue

        updated_branches = []
        for branch in node[poly_key]:
            if not isinstance(branch, dict):
                updated_branches.append(branch)
                continue

            # Branch properties override common base properties
            new_branch = copy.deepcopy(branch)
            branch_props = new_branch.setdefault("properties", {})
            combined_props = copy.deepcopy(base_props)
            combined_props.update(branch_props)
            new_branch["properties"] = combined_props

            # Combine union and base required field lists
            new_branch["required"] = list(
                set(base_req + new_branch.get("required", []))
            )

            # Ensure the branch knows its JSON type (usually 'object') if inheriting from common base
            if "type" not in new_branch and base_type:
                new_branch["type"] = base_type

            updated_branches.append(new_branch)
        node[poly_key] = updated_branches


def flatten_entity_reference(node, entity_definition):
    """
    Replaces $ref to 'ucp.json#/$defs/entity' with actual logic.
    This effectively converts 'Entity' inheritance into direct 'BaseModel' fields.
    """
    if "allOf" not in node:
        return

    filtered_all_of = []
    for item in node["allOf"]:
        is_entity_ref = isinstance(item, dict) and item.get(
            "$ref", ""
        ).endswith("ucp.json#/$defs/entity")
        if is_entity_ref:
            if not entity_definition:
                raise ValueError(
                    "Node requires 'ucp.json#/$defs/entity' but no entity definition was provided."
                )
            # Inline a copy; strip name to prevent unwanted class generation for the base
            e_copy = copy.deepcopy(entity_definition)
            e_copy.pop("title", None)
            e_copy.pop("description", None)
            filtered_all_of.append(e_copy)
        else:
            filtered_all_of.append(item)
    node["allOf"] = filtered_all_of


def preprocess_full_schema(schema, entity_def=None):
    """
    Main entry point for normalizing a single schema file.
    Uses bottom-up iteration to ensure nested structures are flat before parents process them.
    """
    # 1. Discovery: find all dictionaries in the tree
    nodes = [n for n in iter_nodes(schema) if isinstance(n, dict)]

    # 2. Execution: process in reverse (approximate bottom-to-top)
    for node in reversed(nodes):
        if entity_def:
            flatten_entity_reference(node, entity_def)
        merge_all_of_to_node(node, schema)
        distribute_properties_to_branches(node)


# --- Variant Generation (Create/Update/Complete) ---


def get_required_ops(schema):
    """
    Scans a schema for the custom 'ucp_request' metadata.
    Returns a set of operation keys (e.g. {'create', 'update'}) that need distinct models.
    """
    ops = set()
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return ops

    for data in properties.values():
        if isinstance(data, dict):
            marker = data.get("ucp_request")
            if isinstance(marker, str):
                ops.update(["create", "update"])  # Standard shortcut
            elif isinstance(marker, dict):
                ops.update(marker.keys())
    return ops


def eval_prop_inclusion(name, data, op, base_required):
    """
    Decides if a property should be included or required for a specific operation.
    Follows UCP 'ucp_request' metadata rules.
    """
    if not isinstance(data, dict):
        return True, name in base_required

    marker = data.get("ucp_request")
    include = True
    is_required = name in base_required

    if marker == "omit":
        include = False
    elif marker == "required":
        is_required = True
    elif isinstance(marker, dict):
        val = marker.get(op)
        if val == "omit" or val is None:
            include = False
        elif val == "required":
            is_required = True

    return include, is_required


def update_variant_identity(variant_schema, op, stem):
    """Updates title and $id so that generated code doesn't have naming collisions."""
    base_title = variant_schema.get("title", stem)
    variant_schema["title"] = f"{base_title} {op.capitalize()} Request"

    if "$id" in variant_schema:
        old_id = variant_schema["$id"]
        if "/" in old_id:
            parts = old_id.split("/")
            # Support both .json and extension-less IDs
            name_ext = parts[-1].split(".", 1)
            name = name_ext[0]
            ext = name_ext[1] if len(name_ext) > 1 else "json"

            parts[-1] = f"{name}_{op}_request.{ext}"
            variant_schema["$id"] = "/".join(parts)


def rewrite_refs_to_variants(root, op, file_path, variant_needs):
    """
    Walks a schema tree and updates external links to point to variant files.
    Example: product.json -> product_create_request.json
    """
    for node in iter_nodes(root):
        if isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if "#" not in ref:  # External file reference
                abs_target = (file_path.parent / ref).resolve()
                if (
                    str(abs_target) in variant_needs
                    and op in variant_needs[str(abs_target)]
                ):
                    ref_path = Path(ref)
                    node["$ref"] = str(
                        ref_path.parent / f"{ref_path.stem}_{op}_request.json"
                    )


def _strip_extension_defs(variant_schema):
    """
    Remove $defs entries that compose with an external base via allOf — these
    are extension schemas (and their variants) that belong to the source file,
    not to its operation-specific variants. Carrying them over makes codegen
    emit duplicate sibling classes (e.g. a leaked PlatformSchema in
    order_create_request.py). Plain type aliases (no external base) are kept
    because variants legitimately reference them via local $refs.
    """
    defs = variant_schema.get("$defs")
    if not isinstance(defs, dict):
        return
    for name in list(defs.keys()):
        d = defs[name]
        if not isinstance(d, dict):
            continue
        if any(
            isinstance(item, dict)
            and isinstance(item.get("$ref"), str)
            and "#" not in item["$ref"]
            for item in (d.get("allOf") or [])
        ):
            del defs[name]
    if not defs:
        variant_schema.pop("$defs", None)


def _create_single_variant(
    schema, op, stem, file_path, global_variant_requirements
):
    """Creates a modified copy of the schema tailored for a specific operation."""
    variant = copy.deepcopy(schema)
    update_variant_identity(variant, op, stem)
    _strip_extension_defs(variant)

    new_props = {}
    new_required = []
    base_req = schema.get("required", [])

    for name, data in schema.get("properties", {}).items():
        include, required = eval_prop_inclusion(name, data, op, base_req)
        if include:
            prop_data = copy.deepcopy(data)
            if isinstance(prop_data, dict):
                prop_data.pop("ucp_request", None)
                rewrite_refs_to_variants(
                    prop_data, op, file_path, global_variant_requirements
                )

            new_props[name] = prop_data
            if required:
                new_required.append(name)

    variant["properties"] = new_props
    variant["required"] = new_required
    return variant


def generate_variants(path, schema, ops, global_variant_requirements):
    """Creates specific JSON files (create/update/complete) based on ucp_request markers."""
    file_path = Path(path)
    for op in ops:
        variant = _create_single_variant(
            schema, op, file_path.stem, file_path, global_variant_requirements
        )
        out = file_path.parent / f"{file_path.stem}_{op}_request.json"
        save_json(variant, out)
        sys.stdout.write(f"Generated variant: {out}\n")


# --- Extension Variant Generation ($defs that compose with an external base) ---


def find_extension_defs(schema):
    """
    Locate $defs entries that extend an external base schema (via allOf with
    an external $ref) and declare ucp_request markers on their own properties.

    These are 'extension schemas' like the one in shopping/fulfillment.json
    that adds a `fulfillment` field to Checkout. The original variant generator
    only scans top-level properties and never sees them.

    Returns: list of (def_name, def_schema, ops_set).
    """
    defs = schema.get("$defs") or {}
    if not isinstance(defs, dict):
        return []

    results = []
    for name, d in defs.items():
        if not isinstance(d, dict):
            continue
        has_external_base = any(
            isinstance(item, dict)
            and isinstance(item.get("$ref"), str)
            and "#" not in item["$ref"]
            for item in (d.get("allOf") or [])
        )
        if not has_external_base:
            continue

        ops = set()
        for prop_data in (d.get("properties") or {}).values():
            if not isinstance(prop_data, dict):
                continue
            marker = prop_data.get("ucp_request")
            if isinstance(marker, str):
                ops.update(["create", "update"])
            elif isinstance(marker, dict):
                for op, val in marker.items():
                    if val != "omit":
                        ops.add(op)
        if ops:
            results.append((name, d, ops))
    return results


def _inline_local_ref_alias(node, root_schema):
    """
    If `node` carries a local $ref whose target in $defs is itself just a thin
    `{"$ref": "<external>.json"}` alias, replace the local ref with that
    external ref so `rewrite_refs_to_variants` can swap in the variant target.
    """
    if not isinstance(node, dict):
        return
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return
    resolved = resolve_local_ref(ref, root_schema)
    if isinstance(resolved, dict) and len(resolved) == 1 and "$ref" in resolved:
        node["$ref"] = resolved["$ref"]


def _rewrite_extension_base_ref(variant_schema, op, file_path, variant_needs):
    """Repoint the external $ref in allOf at the base schema's variant file."""
    for item in variant_schema.get("allOf") or []:
        if not isinstance(item, dict):
            continue
        ref = item.get("$ref")
        if not isinstance(ref, str) or "#" in ref:
            continue
        abs_target = str((file_path.parent / ref).resolve())
        if abs_target in variant_needs and op in variant_needs[abs_target]:
            p = Path(ref)
            item["$ref"] = str(p.parent / f"{p.stem}_{op}_request.json")


def _create_extension_variant(
    def_schema, op, def_name, file_path, root_schema, variant_needs
):
    """Build a per-op variant copy of an extension $defs entry."""
    variant = copy.deepcopy(def_schema)

    base_title = variant.get("title", def_name)
    variant["title"] = f"{base_title} {op.capitalize()} Request"

    new_props = {}
    new_required = []
    base_req = def_schema.get("required", [])
    for name, data in (def_schema.get("properties") or {}).items():
        include, required = eval_prop_inclusion(name, data, op, base_req)
        if not include:
            continue
        prop_data = copy.deepcopy(data)
        if isinstance(prop_data, dict):
            prop_data.pop("ucp_request", None)
            _inline_local_ref_alias(prop_data, root_schema)
            rewrite_refs_to_variants(prop_data, op, file_path, variant_needs)
        new_props[name] = prop_data
        if required:
            new_required.append(name)
    variant["properties"] = new_props
    variant["required"] = new_required

    _rewrite_extension_base_ref(variant, op, file_path, variant_needs)
    return variant


def generate_extension_variants(schemas, variant_needs):
    """
    For each schema whose $defs declares extension entries with ucp_request
    markers, add per-op variant $defs entries alongside them so codegen emits
    matching extended request models (e.g. CheckoutCreateRequest with the
    fulfillment field).
    """
    for p_abs, schema in schemas.items():
        if "ucp.json" in p_abs:
            continue
        extensions = find_extension_defs(schema)
        if not extensions:
            continue
        defs = schema["$defs"]
        file_path = Path(p_abs)
        for def_name, def_schema, ops in extensions:
            for op in ops:
                variant_name = f"{def_name}_{op}_request"
                if variant_name in defs:
                    continue
                defs[variant_name] = _create_extension_variant(
                    def_schema,
                    op,
                    def_name,
                    file_path,
                    schema,
                    variant_needs,
                )
                sys.stdout.write(
                    f"Generated extension variant: "
                    f"{file_path.name}#/$defs/{variant_name}\n"
                )
        save_json(schema, file_path)


# --- Global Normalization ---


def normalize_metadata_schemas(schemas, target_dir):
    """
    Ensures ucp.json has a root union and other files point to it generically.
    This enables a unified 'ucp' metadata property across the entire SDK.
    Returns structurally corrected schemas.
    """
    ucp_path = str((target_dir / "ucp.json").resolve())
    if ucp_path in schemas:
        ucp = schemas[ucp_path]
        ucp["oneOf"] = [
            {"$ref": f"#/$defs/{d}"}
            for d in [
                "platform_schema",
                "business_schema",
                "response_checkout_schema",
                "response_order_schema",
                "response_cart_schema",
            ]
        ]

    for p_abs, s in schemas.items():
        if "ucp.json" in p_abs or "_request.json" in p_abs:
            continue
        # Find the 'ucp' property and point it to the ucp.json root
        ucp_prop = s.get("properties", {}).get("ucp", {})
        if (
            isinstance(ucp_prop, dict)
            and "$ref" in ucp_prop
            and "ucp.json" in ucp_prop["$ref"]
        ):
            ucp_prop["$ref"] = ucp_prop["$ref"].split("#")[0]
    return schemas


# --- Dependency Management ---


def extract_external_refs(schema, path):
    """Finds all relative external file references in the schema properties."""
    refs = []
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return refs

    for name, data in props.items():
        for node in iter_nodes(data):
            if isinstance(node, dict) and "$ref" in node:
                ref = node["$ref"]
                if "#" not in ref:
                    abs_path = str((path.parent / ref).resolve())
                    refs.append((name, abs_path))
    return refs


def propagate_needs_transitive(variant_needs, schema_refs, schemas):
    """
    If a parent model needs a 'create' variant, and it has a child property 'X',
    then 'X' also needs a 'create' variant so that references match.
    """
    changed = True
    while changed:
        changed = False
        for path, refs in schema_refs.items():
            if path not in variant_needs:
                continue

            for op in list(variant_needs[path]):
                for prop_name, child_path in refs:
                    if child_path not in schemas:
                        continue

                    # Only propagate if the property isn't 'omit'ted for this op
                    data = (
                        schemas[path].get("properties", {}).get(prop_name, {})
                    )
                    include, _ = eval_prop_inclusion(
                        prop_name, data, op, schemas[path].get("required", [])
                    )

                    if include:
                        target_set = variant_needs.setdefault(child_path, set())
                        if op not in target_set:
                            target_set.add(op)
                            changed = True


# --- Main Flow ---


def main():
    """
    Orchestrates the schema preprocessing pipeline:
    1. Pass 1: Local flattening (allOf) and discovery of needed variants
    2. metadata normalization: unifies ucp properties
    3. Pass 2: Transitive propagation (ensuring matched variants for linked schemas)
    4. Pass 3: Extension variants ($defs entries that compose with an external
       base, e.g. Checkout-with-Fulfillment) injected into source schemas
    5. Pass 4: Variant file generation (*_request.json)
    """
    target_dir = Path(
        sys.argv[1] if len(sys.argv) > 1 else "ucp/source/schemas"
    )
    if not target_dir.exists():
        sys.stderr.write(f"Error: Directory {target_dir} not found.\n")
        return

    schemas, schema_refs, variant_needs = {}, {}, {}
    for f in target_dir.rglob("*.json"):
        if "_request.json" in f.name:
            continue
        try:
            s = load_json(f)
            p_abs = str(f.resolve())
            schemas[p_abs] = s
        except Exception as e:
            sys.stderr.write(f"Failed to load {f}: {e}\n")

    # Phase 0: Ensure the metadata 'ucp' property is consistent across all files
    normalize_metadata_schemas(schemas, target_dir)

    ucp_path = str((target_dir / "ucp.json").resolve())
    entity_def = {}
    if ucp_path in schemas:
        entity_def = schemas[ucp_path].get("$defs", {}).get("entity", {})
    if not entity_def:
        raise ValueError(
            "Entity definition not found! 'ucp.json' must define '$defs.entity'"
        )

    # Pass 1: Local flattening and find explicit variant markers
    for p_abs, s in schemas.items():
        if "ucp.json" in p_abs or "_request.json" in p_abs:
            continue
        preprocess_full_schema(s, entity_def)
        # Write back the flattened core schema
        save_json(s, Path(p_abs))

        schema_refs[p_abs] = extract_external_refs(s, Path(p_abs))

        # Check if this schema explicitly asks for variants via 'ucp_request' markers
        ops = get_required_ops(s)
        if ops:
            variant_needs[p_abs] = ops

    if ucp_path in schemas:
        save_json(schemas[ucp_path], Path(ucp_path))

    # Pass 2: Propagate the need for variants down the dependency tree
    propagate_needs_transitive(variant_needs, schema_refs, schemas)

    # Pass 3: Inject extension variants ($defs entries) into source schemas so
    # codegen emits matching extended *_create/update_request models alongside
    # the base extension classes.
    generate_extension_variants(schemas, variant_needs)

    # Pass 4: Finally write out the new variant files
    for path, ops in variant_needs.items():
        generate_variants(path, schemas[path], ops, variant_needs)


if __name__ == "__main__":
    main()
