# OSDU Search Patterns

`osdu` is the shell function `SKILL.md` defines.

Common Elasticsearch query patterns for the OSDU Search API.

## Basic Query Structure

```bash
osdu call POST /api/search/v2/query -d '{
  "kind": "REQUIRED_KIND_PATTERN",
  "query": "OPTIONAL_QUERY_STRING",
  "limit": 10,
  "offset": 0
}'
```

## Kind Patterns

| Pattern | Matches |
|---------|---------|
| `*:*:*:*` | All records |
| `osdu:wks:*:*` | All OSDU well-known schemas |
| `osdu:wks:master-data--Well:*` | All versions of Well master data |
| `osdu:wks:master-data--Well:1.0.0` | Exact schema version |
| `*:*:master-data--*:*` | All master data types |
| `*:*:work-product-component--*:*` | All work product components |
| `*:*:reference-data--*:*` | All reference data types |

## Query String Syntax

OSDU Search uses Elasticsearch query string syntax.

### Match all
```json
{"kind": "*:*:*:*", "query": "*"}
```

### Search by record ID
```json
{"kind": "*:*:*:*", "query": "id:\"<partition>:master-data--Well:abc123\""}
```

### Search by data field (exact)
```json
{"kind": "osdu:wks:master-data--Well:*", "query": "data.Name:(\"My Well\")"}
```

### Search by data field (wildcard)
```json
{"kind": "osdu:wks:master-data--Well:*", "query": "data.Name:(*North Sea*)"}
```

### Boolean combinations
```json
{"kind": "osdu:wks:master-data--Well:*", "query": "data.Name:(\"Well A\") AND data.Country:(\"US\")"}
```

```json
{"kind": "*:*:*:*", "query": "data.Source:(\"my-source\") OR data.Source:(\"other-source\")"}
```

### NOT queries
```json
{"kind": "osdu:wks:master-data--Well:*", "query": "NOT data.Status:(\"Abandoned\")"}
```

### Range queries
```json
{"kind": "*:*:*:*", "query": "data.Depth:[1000 TO 5000]"}
```

### Exists check
```json
{"kind": "*:*:*:*", "query": "_exists_:data.WellName"}
```

## Returned Fields

Limit response to specific fields to reduce payload:
```json
{
  "kind": "osdu:wks:master-data--Well:*",
  "query": "*",
  "limit": 50,
  "returnedFields": ["id", "kind", "data.Name", "data.Country", "data.WellStatus"]
}
```

## Sorting

```json
{
  "kind": "osdu:wks:master-data--Well:*",
  "query": "*",
  "limit": 20,
  "sort": {
    "field": ["data.Name"],
    "order": ["ASC"]
  }
}
```

Multiple sort fields:
```json
{
  "sort": {
    "field": ["data.Country", "data.Name"],
    "order": ["ASC", "ASC"]
  }
}
```

## Pagination

```bash
# Page 1
osdu call POST /api/search/v2/query -d '{"kind":"*:*:*:*","query":"*","limit":50,"offset":0}'

# Page 2
osdu call POST /api/search/v2/query -d '{"kind":"*:*:*:*","query":"*","limit":50,"offset":50}'
```

## Timing Guidance

| Operation | Availability |
|-----------|-------------|
| `GET /api/storage/v2/records/{id}` | Immediate after creation |
| `POST /api/search/v2/query` by ID field | Immediate (search_by_id) |
| `POST /api/search/v2/query` by data fields | 30-60 seconds (indexing delay) |
| `POST /api/search/v2/query` by kind | 30-60 seconds (indexing delay) |

After creating a record, verify with storage GET first. Wait 30-60 seconds before
expecting it to appear in field-based search queries.

## Common Issues

| Problem | Solution |
|---------|----------|
| Record not in search results | Wait for indexing (30-60s), verify with storage GET |
| Query syntax error | Check Elasticsearch quoting: `data.Name:("exact value")` |
| No results for kind query | Verify the kind pattern with `*` wildcards |
| No match on a text value | Text fields are analyzed; quote the phrase or query the `.keyword` subfield for an exact match |
| Special characters in values | Wrap in double quotes: `data.ID:("value-with-dashes")` |
