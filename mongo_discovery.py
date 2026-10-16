from db import db


def flatten_schema(doc, prefix=""):
    fields = {}
    for key, value in doc.items():
        if key == "_id" and prefix == "":
            continue

        path = f"{prefix}.{key}" if prefix else key

        if isinstance(value, dict):
            fields.update(flatten_schema(value, path))
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                fields[path] = "array_of_objects"
                fields.update(flatten_schema(value[0], f"{path}[]"))
            else:
                fields[path] = "array"
        else:
            fields[path] = type(value).__name__

    return fields


def get_relevant_collections():
    return db.list_collection_names()


def build_schema_catalog(collection_names=None, sample_size=5):
    if collection_names is None:
        collection_names = get_relevant_collections()

    catalog = {}
    for name in collection_names:
        sample_docs = list(db[name].aggregate([{"$sample": {"size": 50}}]))
        if not sample_docs:
            continue
        fields = {}
        for doc in sample_docs:
            fields.update(flatten_schema(doc))
        catalog[name] = fields

    return catalog


def get_shallow_summary(catalog, max_fields=None):
    summary = {}
    for name, fields in catalog.items():
        top_level = sorted({p.split(".")[0].split("[")[0] for p in fields})
        summary[name] = top_level[:max_fields] if max_fields else top_level
    return summary


def describe_field(catalog, collection_name, field_path):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a known collection"}

    if field_path in (".", "", "*", "all"):
        return {
            "note": (
                "Complete flattened leaf-level fields for this collection. "
                "Use these dotted paths directly as 'fields'/'columns' — no "
                "further describe_field calls are needed to discover nested "
                "structure."
            ),
            "fields": dict(sorted(catalog[collection_name].items())),
        }

    matches = {
        path: type_name
        for path, type_name in catalog[collection_name].items()
        if path == field_path or path.startswith(field_path + ".") or path.startswith(field_path + "[]")
    }
    if not matches:
        return {"error": f"No fields found under '{field_path}' in '{collection_name}'"}
    return matches