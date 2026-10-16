from datetime import datetime
from db import db
import json
import logging

MAX_RESULTS = 25     # Upper limit if the user asks for all the data, max of 25 data will be provided at a time.
DEFAULT_LIMIT = 10    # If no lower limit is given it will give me minimum of one data.

BULKY_FIELD_PATTERNS = ("signature", "s3path", "base64", "qrcode", "barcode")


logger = logging.getLogger("lims.mongo")

def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _bulky_field_projection(catalog, collection_name):
    if collection_name not in catalog:
        return None
    projection = {}
    for path in catalog[collection_name]:
        leaf = path.split(".")[-1].split("[]")[0].lower()
        if any(pattern in leaf for pattern in BULKY_FIELD_PATTERNS):
            projection[path.replace("[]", "")] = 0
    return projection or None


def build_date_query(date_field, date_from=None, date_to=None):
    if not date_from and not date_to:
        return {}
    condition = {}
    if date_from:
        condition["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
    if date_to:
        condition["$lt"] = datetime.strptime(date_to, "%Y-%m-%d")
    return {date_field: condition}


def query_collection(catalog, collection_name, filters=None, date_field=None,
                      date_from=None, date_to=None, fields=None, limit=None,
                      bypass_cap=False):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a valid collection"}

    if bypass_cap:
        limit = limit or 0
    else:
        limit = min(limit or DEFAULT_LIMIT, MAX_RESULTS)

    query = dict(filters or {})

    if date_field:
        query.update(build_date_query(date_field, date_from, date_to))

    if fields:
        projection = {f: 1 for f in fields}
        projection["_id"] = 1
    else:
        projection = _bulky_field_projection(catalog, collection_name)

    total_count = db[collection_name].count_documents(query)
    
    logger.info(
        f"[mongo_query] {collection_name}.find(query={json.dumps(query, indent=2, default=str)}, "
        f"projection={json.dumps(projection, default=str)}, limit={limit})"
    )

    cursor = db[collection_name].find(query, projection)
    if limit:
        cursor = cursor.limit(limit)

    rows = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        rows.append(_json_safe(doc))

    return {
        "returned_count": len(rows),
        "total_matching_count": total_count,
        "collection": collection_name,
        "records": rows,
    }


def distinct_values(catalog, collection_name, field_path, limit=50):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a valid collection"}
    
    logger.info(f"[mongo_query] {collection_name}.distinct({field_path!r})")

    values = db[collection_name].distinct(field_path)
    values = [_json_safe(v) for v in values]
    result= {"field": field_path, "count": len(values), "values": values[:limit]}
    if len(values)>limit:
        result["note"]= f"Showing {limit} of {len(values)} distinct values; tell the user the list is partial."
    return result


def _get_nested(rec, dotted_key):
    value = rec
    for part in dotted_key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _parse_dt(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def analyze_parameter_durations(catalog, collection_name, filters=None, date_field=None, date_from=None, date_to=None,
                                 group_by="parameter.name", result_field="test_result",
                                 start_field="analysisStartDate", end_field="analysisEndDate",
                                 limit=None):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a valid collection"}

    fetch_limit = limit or 0 
    query = dict(filters or {})
    if date_field:
        query.update(build_date_query(date_field, date_from, date_to))

    projection = {group_by: 1, result_field: 1, start_field: 1, end_field: 1, "_id": 1}

    total_count = db[collection_name].count_documents(query)
    
    logger.info(
        f"[mongo_query] {collection_name}.find(query={json.dumps(query, indent=2, default=str)}, "
        f"projection={json.dumps(projection, default=str)}, limit={fetch_limit})"
    )
    
    cursor = db[collection_name].find(query, projection)
    if fetch_limit:
        cursor = cursor.limit(fetch_limit)

    groups = {}
    skipped_missing_dates = 0
    negative_or_zero_duration_count = 0

    for doc in cursor:
        key = _get_nested(doc, group_by)
        key = key if key not in (None, "") else "(unspecified)"

        result_value = _get_nested(doc, result_field)
        result_value = result_value if result_value not in (None, "") else "(no result recorded)"

        start_raw = _get_nested(doc, start_field)
        end_raw = _get_nested(doc, end_field)
        start_dt = _parse_dt(start_raw)
        end_dt = _parse_dt(end_raw)

        g = groups.setdefault(key, {
            "occurrences": 0,
            "result_values": {},
            "durations_seconds": [],
            "missing_dates": 0,
            "sample_record_ids": [],
        })
        g["occurrences"] += 1
        g["result_values"][result_value] = g["result_values"].get(result_value, 0) + 1
        if len(g["sample_record_ids"]) < 3:
            g["sample_record_ids"].append(str(doc.get("_id")))

        if start_dt is None or end_dt is None:
            g["missing_dates"] += 1
            skipped_missing_dates += 1
            continue

        duration = (end_dt - start_dt).total_seconds()
        g["durations_seconds"].append(duration)
        if duration <= 0:
            negative_or_zero_duration_count += 1

    summary = []
    for key, g in groups.items():
        durations = g["durations_seconds"]
        entry = {
            "group": key,
            "occurrences": g["occurrences"],
            "distinct_result_values": g["result_values"],
            "records_missing_start_or_end_date": g["missing_dates"],
            "sample_record_ids": g["sample_record_ids"],
        }
        if durations:
            entry["duration_seconds"] = {
                "count_with_valid_duration": len(durations),
                "min": min(durations),
                "max": max(durations),
                "avg": sum(durations) / len(durations),
                "all_values": durations,
            }
        else:
            entry["duration_seconds"] = None
        summary.append(entry)

    summary.sort(key=lambda e: e["occurrences"], reverse=True)

    return {
        "collection": collection_name,
        "total_matching_count": total_count,
        "records_analyzed": sum(g["occurrences"] for g in groups.values()),
        "records_with_missing_start_or_end_date": skipped_missing_dates,
        "records_with_zero_or_negative_duration": negative_or_zero_duration_count,
        "groups": summary,
    }


def count_by_field(catalog, collection_name, group_by, filters=None,date_field=None, date_from=None, date_to=None, date_bucket="auto"):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a valid collection"}

    query = dict(filters or {})
    if date_field:
        query.update(build_date_query(date_field, date_from, date_to))

    effective_bucket, group_id_expr = _resolve_group_bucket(
        catalog, collection_name, group_by, date_bucket
    )

    pipeline = [
        {"$match": query},
        {"$group": {"_id": group_id_expr, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    logger.info(f"[mongo_query] {collection_name}.aggregate({json.dumps(pipeline, indent=2, default=str)})")
    results = list(db[collection_name].aggregate(pipeline))

    counts = []
    for r in results:
        key = r["_id"]
        key = _json_safe(key) if key is not None else "(unspecified)"
        counts.append({"group": key, "count": r["count"]})

    total = sum(c["count"] for c in counts)

    return {
        "collection": collection_name,
        "group_by": group_by,
        "date_bucket_used": effective_bucket,
        "total_matching_count": total,
        "distinct_groups": len(counts),
        "counts": counts,
    }


def first_or_last_by_field(catalog, collection_name, group_by, target_field, mode="first",
                            filters=None, date_field=None, date_from=None, date_to=None):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a valid collection"}
    if mode not in ("first", "last"):
        return {"error": "mode must be 'first' or 'last'"}

    query = dict(filters or {})
    if date_field:
        query.update(build_date_query(date_field, date_from, date_to))

    op = "$min" if mode == "first" else "$max"

    pipeline = [
        {"$match": query},
        {"$group": {
            "_id": f"${group_by}",
            "value": {op: f"${target_field}"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"value": 1}},
    ]
    logger.info(f"[mongo_query] {collection_name}.aggregate({json.dumps(pipeline, indent=2, default=str)})")
    results = list(db[collection_name].aggregate(pipeline))

    rows = []
    for r in results:
        key = r["_id"]
        key = _json_safe(key) if key not in (None, "") else "(unspecified)"
        rows.append({
            "group": key,
            "value": _json_safe(r["value"]),
            "record_count": r["count"],
        })

    return {
        "collection": collection_name,
        "group_by": group_by,
        "target_field": target_field,
        "mode": mode,
        "distinct_groups": len(rows),
        "results": rows,
    }
    

def _resolve_group_bucket(catalog, collection_name, group_by, date_bucket):
    field_type = catalog.get(collection_name, {}).get(group_by)
    if date_bucket == "auto":
        effective_bucket = "day" if field_type == "datetime" else None
    elif date_bucket in ("day", "week", "month"):
        effective_bucket = date_bucket if field_type == "datetime" else None
    else:
        effective_bucket = None

    if effective_bucket:
        bucket_format = {
            "day": "%Y-%m-%d",
            "week": "%G-W%V",
            "month": "%Y-%m",
        }[effective_bucket]
        group_id_expr = {
            "$dateToString": {
                "format": bucket_format,
                "date": f"${group_by}",
                "onNull": "(unspecified)",
            }
        }
    else:
        group_id_expr = f"${group_by}"

    return effective_bucket, group_id_expr


def sum_by_field(catalog, collection_name, group_by, sum_fields, filters=None,
                  date_field=None, date_from=None, date_to=None, date_bucket="auto"):
    if collection_name not in catalog:
        return {"error": f"'{collection_name}' is not a valid collection"}
    if not sum_fields:
        return {"error": "sum_by_field requires at least one field in 'sum_fields'"}

    query = dict(filters or {})
    if date_field:
        query.update(build_date_query(date_field, date_from, date_to))

    effective_bucket, group_id_expr = _resolve_group_bucket(
        catalog, collection_name, group_by, date_bucket
    )

    safe_keys = {f: f"sum__{f.replace('.', '__')}" for f in sum_fields}

    group_stage = {"_id": group_id_expr, "count": {"$sum": 1}}
    for f in sum_fields:
        group_stage[safe_keys[f]] = {"$sum": f"${f}"}

    pipeline = [
        {"$match": query},
        {"$group": group_stage},
        {"$sort": {"_id": 1}},
    ]
    logger.info(f"[mongo_query] {collection_name}.aggregate({json.dumps(pipeline, indent=2, default=str)})")
    results = list(db[collection_name].aggregate(pipeline))

    groups = []
    overall_totals = {f: 0 for f in sum_fields}
    overall_count = 0

    for r in results:
        key = r["_id"]
        key = _json_safe(key) if key is not None else "(unspecified)"
        count = r["count"]
        overall_count += count

        sums = {}
        for f in sum_fields:
            value = r.get(safe_keys[f]) or 0
            sums[f] = value
            sums[f"{f}_avg"] = (value / count) if count else 0
            overall_totals[f] += value

        groups.append({"group": key, "count": count, "sums": sums})

    return {
        "collection": collection_name,
        "group_by": group_by,
        "sum_fields": sum_fields,
        "date_bucket_used": effective_bucket,
        "total_matching_count": overall_count,
        "distinct_groups": len(groups),
        "overall_totals": overall_totals,
        "groups": groups,
    }