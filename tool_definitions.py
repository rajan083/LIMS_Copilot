tools = [
    {
        "type": "function",
        "function": {
            "name": "list_collections",
            "description": "Lists available collections with top-level field names. Call first if unsure which collection holds the data.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_field",
            "description": "Expands a nested field (e.g. 'customer') into its full sub-field paths and types. Use field_path='all' to get every flattened leaf field for a collection in one call. Call before querying a nested field you haven't seen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "field_path": {"type": "string"},
                },
                "required": ["collection_name", "field_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "distinct_values",
            "description": "Returns the real, distinct stored values for a field (e.g. exact analyst name spelling/casing). Call before filtering on a name/text field if unsure of the exact stored value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "field_path": {"type": "string"},
                },
                "required": ["collection_name", "field_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_collection",
            "description": (
                "Query a collection with a MongoDB-style filter. Use dot-notation for "
                "nested fields (e.g. 'customer.name'). For date filtering pass date_field "
                "plus date_from/date_to as exact 'YYYY-MM-DD' strings you computed yourself "
                "— never a relative phrase. Documents are large/nested: always pass "
                "'fields' with only what's needed, unless the user wants the full record. "
                "Check list_collections/describe_field/distinct_values first if unsure of "
                "field names or values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "filters": {"type": "object", "description": "Non-date filter fields, e.g. {\"order_status\": \"PENDING\"}"},
                    "date_field": {"type": "string", "description": "Dot-path of the date field, e.g. 'order_date'."},
                    "date_from": {"type": "string", "description": "Inclusive start, 'YYYY-MM-DD'."},
                    "date_to": {"type": "string", "description": "Exclusive end, 'YYYY-MM-DD'."},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific field paths to return (e.g. ['order_status', 'customer.name']). Omit only if the user needs the complete record.",
                    },
                    "limit": {"type": "integer", "description": "Max rows to return, default 5."},
                },
                "required": ["collection_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_records_to_excel",
            "description": (
                "Export a query_collection-style filter to a downloadable .xlsx file. "
                "⚠️ Do NOT call this unless the user's own message explicitly asked for a "
                "file/export/download/spreadsheet. If they just asked to see, compare, or "
                "list data, answer with a markdown table in chat instead — see the "
                "CHAT vs EXCEL rule in your instructions. "
                "Export a query_collection-style filter to a downloadable .xlsx file. "
                "For a distinct/unique list (e.g. 'all the analysts'), set 'columns' to "
                "that one field and 'dedupe' to true — don't use distinct_values for this. "
                "For fields from TWO collections in one sheet, use "
                "export_joined_records_to_excel instead. With 'sheets', set "
                "'columns'/'dedupe' per sheet — top-level values are ignored then. "
                "When 'columns' is omitted for a genuine full-record export (top-level "
                "or per-sheet inside 'sheets'), nested array fields (multiple sub-records "
                "per row, e.g. sample-level or parameter-level detail lists) are "
                "automatically split into their own linked sheet(s) instead of being "
                "dumped as JSON text — for 'sheets', these child sheets are named after "
                "the field and shared/merged across all groups, linked back via "
                "'record_id'. The tool result tells you which sheets were created and "
                "why; relay that to the user in 1-2 sentences, don't just say 'exported'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "filters": {"type": "object"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific fields to include. Omit for all columns. Ignored when 'sheets' is used.",
                    },
                    "dedupe": {
                        "type": "boolean",
                        "description": "Remove duplicate rows (e.g. for a unique-list export). Ignored when 'sheets' is used.",
                    },
                    "limit": {"type": "integer"},
                    "label": {"type": "string", "description": "Short label for the filename, e.g. 'completed'."},
                    "sheets": {
                        "type": "array",
                        "description": "Use only if the user explicitly wants one file with multiple sheets.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "sheet_name": {"type": "string"},
                                "collection_name": {"type": "string"},
                                "filters": {"type": "object"},
                                "date_field": {"type": "string"},
                                "date_from": {"type": "string"},
                                "date_to": {"type": "string"},
                                "columns": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Flattened leaf field paths for this sheet. Omit for all columns.",
                                },
                                "dedupe": {"type": "boolean"},
                            },
                            "required": ["sheet_name", "collection_name"],
                        },
                    },
                },
                "required": ["collection_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_joined_records_to_excel",
            "description": (
                "Joins two collections on a shared key and exports the combined result as "
                "ONE sheet. Use whenever fields from two collections need to land in a "
                "single sheet (e.g. 'orders with account and analyst') — this performs the "
                "join; don't claim a combined sheet isn't possible. "
                "⚠️ Do NOT call this unless the user's own message explicitly asked for a "
                "file/export/download/spreadsheet. If they just asked to see, compare, or "
                "list data, answer with a markdown table in chat instead — see the "
                "EXCEL IS NEVER AUTOMATIC rule in your instructions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "left": {
                        "type": "object",
                        "description": "The base collection.",
                        "properties": {
                            "collection_name": {"type": "string"},
                            "filters": {"type": "object"},
                            "fields": {"type": "array", "items": {"type": "string"}},
                            "join_key": {"type": "string", "description": "Field path to join on, e.g. '_id'."},
                        },
                        "required": ["collection_name", "join_key"],
                    },
                    "right": {
                        "type": "object",
                        "description": "The collection to join in.",
                        "properties": {
                            "collection_name": {"type": "string"},
                            "filters": {"type": "object"},
                            "fields": {"type": "array", "items": {"type": "string"}},
                            "join_key": {"type": "string", "description": "Field path matching left's join_key, e.g. 'order._id'."},
                        },
                        "required": ["collection_name", "join_key"],
                    },
                    "dedupe": {"type": "boolean"},
                    "label": {"type": "string"},
                },
                "required": ["left", "right"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_parameter_durations",
            "description": (
                "Computes accurate test-result distribution AND duration min/max/avg per "
                "group from real timestamps (not eyeballed). ALWAYS use this instead of "
                "reading raw records to compare results or analysis-time variation. Fetches "
                "matching records itself — no need to call query_collection first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "filters": {"type": "object", "description": "MongoDB filter, e.g. {\"order._id\": {\"$in\": [...]}}"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "group_by": {"type": "string", "description": "Dot-path field to group by. Defaults to 'parameter.name'."},
                    "result_field": {"type": "string", "description": "Dot-path of the test result field. Defaults to 'test_result'."},
                    "start_field": {"type": "string", "description": "Dot-path of the analysis start timestamp. Defaults to 'analysisStartDate'."},
                    "end_field": {"type": "string", "description": "Dot-path of the analysis end timestamp. Defaults to 'analysisEndDate'."},
                    "limit": {"type": "integer", "description": "Optional cap on records fetched. Omit for the full matching set."},
                },
                "required": ["collection_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_by_field",
            "description": (
                "Returns the exact record COUNT per distinct value of a field via MongoDB "
                "aggregation. ALWAYS use instead of eyeballing duplicates for any "
                "'<field>-wise'/'count of X by Y' request. Datetime group_by fields "
                "auto-bucket by day (date_bucket='auto'). Feed 'counts' into generate_chart "
                "as labels/values for a chart."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "group_by": {"type": "string", "description": "Dot-path field to group/count by, e.g. 'order_date', 'order_status'."},
                    "filters": {"type": "object"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "date_bucket": {
                        "type": "string",
                        "enum": ["auto", "day", "week", "month", "raw"],
                        "description": "Only relevant for a datetime group_by. 'auto' buckets by day; use 'week'/'month' for long ranges; 'raw' for exact timestamps (rarely useful).",
                    },
                },
                "required": ["collection_name", "group_by"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sum_by_field",
            "description": (
                "Returns the exact SUM (and average) of one or more numeric fields per "
                "distinct group via MongoDB $group/$sum. ALWAYS use instead of adding up raw "
                "records yourself for 'total X by Y'/'which Y brings the most revenue' "
                "requests. Distinct from count_by_field: this answers a TOTAL/VALUE "
                "question, not a record count. Datetime group_by fields auto-bucket by day "
                "(date_bucket='auto'). Feed 'groups' into generate_chart when a chart is "
                "wanted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "group_by": {"type": "string", "description": "Dot-path field to group/sum by, e.g. 'customer.name', 'order_date'."},
                    "sum_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more numeric dot-path fields to sum per group, e.g. ['final_amount', 'net_amount']. Each group gets a sum and average per field.",
                    },
                    "filters": {"type": "object"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "date_bucket": {
                        "type": "string",
                        "enum": ["auto", "day", "week", "month", "raw"],
                        "description": "Only relevant for a datetime group_by. 'auto' buckets by day; use 'week'/'month' for long ranges; 'raw' for exact timestamps (rarely useful).",
                    },
                },
                "required": ["collection_name", "group_by", "sum_fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_chart",
            "description": (
                "Renders a chart, saved in charts/ (separate from exports/). File format "
                "depends on 'mode': 'offline' saves a PNG needing no internet; "
                "'online' saves an interactive HTML that loads Plotly from the internet — "
                "better for sharing/emailing. Returns the actual saved file path. Use "
                "whenever the user asks to show/chart/plot/visualize/graph something, or "
                "when a comparison/count/sum/variance result would be clearer as a chart. "
                "Rendering is handled for you: offline PNGs print each value directly on "
                "the chart (since they can't be hovered), pie/donut legends carry each "
                "slice's value and percentage, zero-value slices are dropped, colours "
                "scale to any number of categories, and size/font auto-adjust to label "
                "count and length — pass width/height/font_size only to override a "
                "specific need, and don't warn the user about crowding or suggest fewer "
                "categories. Build 'labels'/'series' yourself from data already retrieved "
                "— this tool only draws, it doesn't query. You must actually call this "
                "tool to produce a chart — never fabricate a filename or markdown image "
                "link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Category names for the x-axis (or pie/donut wedges). Not used for chart_type='histogram'.",
                    },
                    "series": {
                        "type": "object",
                        "description": (
                            "Map of series name -> list of numbers, same length/order as "
                            "'labels' (except histogram). One key for a simple chart, "
                            "multiple to compare series. 'pie'/'donut' accept exactly ONE "
                            "series — call once per field for several pie charts. For "
                            "'histogram', each list is raw ungrouped values to bin — don't "
                            "pre-aggregate."
                        ),
                        "additionalProperties": {"type": "array", "items": {"type": "number"}},
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": [
                            "bar", "horizontal_bar", "stacked_bar",
                            "line", "area",
                            "pie", "donut",
                            "scatter",
                            "histogram",
                        ],
                        "description": (
                            "Defaults to 'bar'. 'bar' — compare values across categories; "
                            "'horizontal_bar' — same but for long/many category names; "
                            "'stacked_bar' — series stacking to a total per category; "
                            "'line' — trend across an ordered axis like dates; 'area' — "
                            "trend where filled magnitude matters; 'pie'/'donut' — one "
                            "metric's share of a whole, single series only ('donut' only if "
                            "a ring look is requested); 'scatter' — discrete points, no "
                            "implied trend; 'histogram' — distribution of raw ungrouped "
                            "values."
                        ),
                    },
                    "title": {"type": "string"},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                    "label": {"type": "string", "description": "Short label for the saved filename, e.g. 'dispatching_duration_variance'."},
                    "width": {"type": "integer", "minimum": 600, "description": "Optional width in px. Leave unset to auto-size."},
                    "height": {"type": "integer", "minimum": 450, "description": "Optional height in px. Leave unset to auto-size."},
                    "font_size": {"type": "integer", "minimum": 8, "maximum": 32, "description": "Optional base font size in px. Leave unset for automatic sizing."},
                    "mode": {
                        "type": "string",
                        "enum": ["offline", "online"],
                        "description": (
                            "'offline' = PNG (no internet needed), 'online' = interactive "
                            "HTML (loads Plotly from the internet — better for sharing/"
                            "email). If the user's message doesn't explicitly state or "
                            "unmistakably imply which they want, ASK before calling this "
                            "tool — do not pick 'offline' as a silent default."
                        ),
                    },
                },
                "required": ["labels", "series"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "first_or_last_by_field",
            "description": (
                "Returns the earliest ('first') or latest ('last') value of a "
                "date/numeric field per distinct group, via exact MongoDB "
                "aggregation. ALWAYS use this for 'first order per client', "
                "'when did X first order', 'how long has X been a customer', "
                "'most recent test per analyst', etc. — never manually sort or "
                "eyeball raw records, and never export data just so the user "
                "can find this themselves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "group_by": {"type": "string", "description": "Dot-path field to group by, e.g. 'customer.name'."},
                    "target_field": {"type": "string", "description": "Dot-path field to find the min/max of, e.g. 'order_date'."},
                    "mode": {"type": "string", "enum": ["first", "last"], "description": "'first' = earliest (min), 'last' = latest (max)."},
                    "filters": {"type": "object"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                },
                "required": ["collection_name", "group_by", "target_field"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Search unstructured reference documents (SOPs, procedures, incident "
                "reports, compliance docs) for content relevant to a natural-language "
                "question. Use this for policy/procedure questions, NOT for structured "
                "data queries — use query_collection, count_by_field, etc. for those. "
                "Results are cached per (query, top_k, min_relevance) combination, so "
                "repeating the same question with the same settings returns instantly "
                "without re-searching the vector store."
                " The index also contains the E-labs user manual (page-by-page, including"
                " written descriptions of its screenshots), so use it for how-to and "
                "'what does this screen/field mean' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The natural-language question or topic to search for.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of relevant chunks to return. Default 3.",
                    },
                    "min_relevance": {
                        "type": "number",
                        "description": (
                            "Minimum cosine-similarity score (0-1) a chunk must meet to be "
                            "returned. Default 0.3. Raise it (e.g. 0.5-0.6) to get only "
                            "tightly-matching chunks; lower it if a reasonable query is "
                            "returning nothing."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cache_stats",
            "description": (
                "Returns how many search_documents results are currently cached "
                "(entry count and oldest/newest access times). Diagnostic tool — call "
                "only if the user explicitly asks about cache size/state, not as part "
                "of answering a normal question."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_hop_search",
            "description": (
                "Search the manuals/SOPs for questions that need information from "
                "more than one place: comparisons, multi-step procedures, or "
                "questions where one document refers to another. Use "
                "search_documents instead for simple single-fact lookups."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The full user question, with any context from earlier in the chat resolved."}
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_report_email",
            "description": (
                "Prepare an email with a previously generated report attached. Use ONLY when the "
                "user explicitly asks to email or send a report. This does not send anything: the "
                "user confirms with a Send button. If the user typed an email address, or you "
                "already showed it to them and they agreed, pass exactly that address as recipient. "
                "If they gave only a name, pass the name. If several contacts match, ask the user "
                "which one and call again with that contact_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "Email address typed by the user (or shown to them and agreed), or a person/company name."},                    "contact_id": {"type": "string", "description": "Only when the user picked from a list of matches."},
                    "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional. File names to attach, only files generated in this chat. Pass several when the user asks for several. Defaults to the latest report.",
                        },
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Short plain-text message."},
                },
                "required": ["subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_summary_to_excel",
            "description": (
                "Export a grouped total (the same data as sum_by_field) to a one-sheet Excel report "
                "with a TOTAL row. Use this when the user asks for a 'report' or 'export' of a total, "
                "revenue, sum or breakdown you just calculated. Pass the same arguments you used for sum_by_field."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "group_by": {"type": "string"},
                    "sum_fields": {"type": "array", "items": {"type": "string"}},
                    "filters": {"type": "object"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "date_bucket": {
                        "type": "string",
                        "enum": ["auto", "day", "week", "month"],
                        "description": "Only applies when group_by is a date field. Prefer month for reports.",
                    },
                    "label": {"type": "string"},
                },
                "required": ["collection_name", "group_by", "sum_fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_powerbi_file",
            "description": (
                "Create a Power BI file (.pbids) that opens in Power BI Desktop with live data. "
                "Use only when the user asks for Power BI or a dashboard. "
                "kind='records' for row-level data, 'count' for counts per group, 'sum' for totals per group."
                "For 'count' and 'sum', group_by is required; for totals over time pass the date field as group_by. "
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["records", "count", "sum"]},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "group_by": {
                        "type": "string",
                        "description": ("Required when kind is 'count' or 'sum': the field to group by. "
                                        "For totals over time (per month, per day), pass the date field here "
                                        "and set date_bucket."),
                    },
                    "sum_fields": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Required when kind is 'sum'.",
                    },                    
                    "filters": {"type": "object"},
                    "date_field": {"type": "string"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_bucket": {"type": "string", "enum": ["auto", "day", "week", "month"]},
                    "limit": {"type": "integer"},
                    "label": {"type": "string"},
                },
                "required": ["collection_name", "kind"],
            },
        },
    },
]