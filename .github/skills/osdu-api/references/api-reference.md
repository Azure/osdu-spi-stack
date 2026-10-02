# OSDU API Reference

Endpoints per service. All paths are relative to the environment's base URL;
`call` resolves them against the `base_url` that `connect` reports. `osdu` is the shell function
`SKILL.md` defines. `<partition>`, `<domain>`, and `<legal-tag>` stand for the `partition`,
`entitlements_domain`, and `partitions.<name>.legal_tag` values `connect` prints.

## Storage Service (`/api/storage/v2`)

### Get Record
```
GET /api/storage/v2/records/{id}
```
Returns the latest version of a record by its ID.

### Get Record Version
```
GET /api/storage/v2/records/{id}/{version}
```
Returns a specific version of a record.

### List Record Versions
```
GET /api/storage/v2/records/versions/{id}
```
Returns all version numbers for a record.

### Create or Update Records
```
PUT /api/storage/v2/records
Content-Type: application/json

[
  {
    "kind": "osdu:wks:reference-data--UnitOfMeasure:1.0.0",
    "acl": {
      "viewers": ["data.default.viewers@<partition>.<domain>"],
      "owners": ["data.default.owners@<partition>.<domain>"]
    },
    "legal": {
      "legaltags": ["<legal-tag>"],
      "otherRelevantDataCountries": ["US"]
    },
    "data": {
      "Name": "Example",
      "ID": "example-001"
    }
  }
]
```
Response: `{"recordIds": [...], "recordIdVersions": [...], "skippedRecordIds": [...]}`

### Query Records by Kind
```
POST /api/storage/v2/query/records?kind={kind}&limit={limit}&cursor={cursor}
```

### Batch Fetch Records
```
POST /api/storage/v2/query/records:batch
Content-Type: application/json

{"records": ["id1", "id2", "id3"]}
```

### Delete Record (soft)
```
DELETE /api/storage/v2/records/{id}
```

### Purge Record (permanent)
```
DELETE /api/storage/v2/records/{id}:purge
```

### Service Info
```
GET /api/storage/v2/info
```

---

## Search Service (`/api/search/v2`)

### Query
```
POST /api/search/v2/query
Content-Type: application/json

{
  "kind": "osdu:wks:master-data--Well:*",
  "query": "*",
  "limit": 10,
  "offset": 0,
  "returnedFields": ["id", "kind", "data.Name"],
  "sort": {
    "field": ["data.Name"],
    "order": ["ASC"]
  }
}
```

Response:
```json
{
  "results": [{"id": "...", "kind": "...", "data": {...}}],
  "totalCount": 100
}
```

**Query parameters:**
- `kind` (required): Schema kind pattern. Supports wildcards: `osdu:wks:*:*`, `*:*:master-data--Well:*`
- `query`: Elasticsearch query string (default `"*"`)
- `limit`: Max results (default 10, max 1000)
- `offset`: Skip N results
- `returnedFields`: Array of fields to include in response
- `sort`: Sort configuration with field names and ASC/DESC order

### Service Info
```
GET /api/search/v2/info
```

---

## Legal Service (`/api/legal/v1`)

### List Legal Tags
```
GET /api/legal/v1/legaltags
GET /api/legal/v1/legaltags?valid=true
```

### Get Legal Tag
```
GET /api/legal/v1/legaltags/{name}
```

### Get Allowed Properties
```
GET /api/legal/v1/legaltags:properties
```
Returns valid values for country codes, security classifications, etc.

### Search Legal Tags
```
POST /api/legal/v1/legaltags:query
Content-Type: application/json

{"names": ["tag-name-1", "tag-name-2"]}
```

### Create Legal Tag
```
POST /api/legal/v1/legaltags
Content-Type: application/json

{
  "name": "my-legal-tag",
  "description": "Description of the legal tag",
  "properties": {
    "countryOfOrigin": ["US"],
    "contractId": "CONTRACT-001",
    "originator": "MyOrg",
    "securityClassification": "Public",
    "personalData": "No Personal Data",
    "exportClassification": "EAR99",
    "dataType": "Public Domain Data",
    "expirationDate": "2027-12-31"
  }
}
```

### Update Legal Tag
```
PUT /api/legal/v1/legaltags
Content-Type: application/json

{
  "name": "existing-tag-name",
  "description": "Updated description",
  "properties": { ... }
}
```

### Delete Legal Tag
```
DELETE /api/legal/v1/legaltags/{name}
```

### Service Info
```
GET /api/legal/v1/info
```

---

## Schema Service (`/api/schema-service/v1`)

### List Schemas
```
GET /api/schema-service/v1/schema?authority=osdu&source=wks&limit=20&offset=0
```

Query parameters:
- `authority`: Filter by authority (e.g., `osdu`)
- `source`: Filter by source (e.g., `wks`)
- `entityType`: Filter by entity type (e.g., `master-data--Well`)
- `status`: Filter by status (`PUBLISHED`, `DEVELOPMENT`)
- `scope`: Filter by scope (`INTERNAL`, `SHARED`)
- `limit`: Max results (default 100)
- `offset`: Skip N results
- `latestVersion`: Only latest versions (`true`/`false`)

### Get Schema by ID
```
GET /api/schema-service/v1/schema/{schemaId}
```
The schema ID is URL-encoded, e.g.: `osdu:wks:master-data--Well:1.0.0`

### Create Schema
```
POST /api/schema-service/v1/schema
Content-Type: application/json

{
  "schemaInfo": {
    "schemaIdentity": {
      "authority": "osdu",
      "source": "wks",
      "entityType": "my-data--MyType",
      "schemaVersionMajor": 1,
      "schemaVersionMinor": 0,
      "schemaVersionPatch": 0
    },
    "status": "DEVELOPMENT",
    "scope": "INTERNAL"
  },
  "schema": { ... JSON Schema ... }
}
```

### Update Schema
```
PUT /api/schema-service/v1/schema
Content-Type: application/json
```
Same body format as create.

### Service Info
```
GET /api/schema-service/v1/info
```

---

## Entitlements Service (`/api/entitlements/v2`)

### Get My Groups
```
GET /api/entitlements/v2/groups
```
Returns groups the authenticated user belongs to.

### List Group Members
```
GET /api/entitlements/v2/groups/{group_email}/members
```

### Add Member to Group
```
POST /api/entitlements/v2/groups/{group_email}/members
Content-Type: application/json

{
  "email": "user@example.com",
  "role": "MEMBER"
}
```
Roles: `MEMBER`, `OWNER`

### Service Info
```
GET /api/entitlements/v2/info
```

---

## Partition Service (`/api/partition/v1`)

### List Partitions
```
GET /api/partition/v1/partitions
```
Returns an array of partition IDs, for example `["opendes"]`. The partition
API refuses a person's token with 403; call it as `deploy` or `member`.

### Get Partition
```
GET /api/partition/v1/partitions/{partitionId}
```

### Create Partition
```
POST /api/partition/v1/partitions/{partitionId}
Content-Type: application/json

{
  "properties": {
    "compliance-ruleset": {"value": "shared"},
    "elastic-endpoint": {"value": "http://elasticsearch-es-http.platform:9200"},
    ...
  }
}
```

### Service Info
```
GET /api/partition/v1/info
```

---

## File Service (`/api/file/v2`)

### Get Upload URL
```
POST /api/file/v2/files/uploadURL
```

### Get File Metadata
```
GET /api/file/v2/files/{id}/metadata
```

### Get Download URL
```
GET /api/file/v2/files/{id}/downloadURL
```

### Service Info
```
GET /api/file/v2/info
```

---

## Workflow Service (`/api/workflow/v1`)

### List Workflows
```
GET /api/workflow/v1/workflow
```

### Get Workflow
```
GET /api/workflow/v1/workflow/{workflowName}
```

### Service Info
```
GET /api/workflow/v1/info
```
