# emBEADings consumer contract

emBEADify's input is an [emBEADings](https://github.com/CantrellJax/embeadings) schema-v1 JSON report
(see its [consumer contract](https://github.com/CantrellJax/embeadings/blob/main/docs/consumer-contract.md)
and [`schemas/v1`](https://github.com/CantrellJax/embeadings/tree/main/schemas/v1)). `plan` follows that
contract:

- `schema_version` must be `1`; anything else is rejected, never guessed.
- `report_type` selects the mapping. Only `orphans`, `mentions`, and `triage` are supported; other types
  are rejected.
- Unknown fields are ignored. Reports are advisory evidence, so every line is emitted commented out.

| Report | Fields read | Template lines |
| --- | --- | --- |
| `orphans` | `dangling_parent[].issue_id`, `parent_id`, `parent_status`, `status`, `title` | `# parent ID <fill-in>`. A parent is never guessed. |
| `mentions` | `claims[].issue_id`, `related_issue_id`, `kind`, `typed_link`, `source_fields`, titles | for `absorbed-by`, `duplicate-of`, `superseded-by`: alternatives `# dup ID CANONICAL` and `# parent ID ABSORBER`. Other kinds get a review-by-hand note. Claims with a typed link are left out. |
| `triage` | `candidates[]` of kind `completed-work-echo`: `issue_id`, `related_issue_id`, `similarity`, `what_to_verify` | `# close ID <reason: ...>` with a reason placeholder. |

Report titles may be private tracker text; they appear only in `#` comments of the template. Do not
publish templates, undo files, or reports from a private tracker.
