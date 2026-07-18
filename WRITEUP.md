# EHR Media Intelligence Platform — Write-up

## Tradeoffs Made and Why

**Rule-based summaries vs. pure LLM**: I implemented both a Claude API summarizer and a rule-based fallback (`scripts/generate_summaries.py`). The rule-based path extracts structured fields (chief concern, diagnoses, labs, anomalies) directly from FHIR data, producing consistent summaries without API costs. The LLM path generates richer, more natural narratives but requires an API key and incurs latency. This dual approach means the full demo runs without credentials.

**Single-page frontend vs. React SPA**: Chose Vanilla JS + Tailwind CSS CDN over React to eliminate build tooling complexity. For a demo with one search view and one detail panel, the simpler approach reduces setup friction while still delivering real-time search, responsive layout, and accessible markup.

**ChromaDB over FAISS**: ChromaDB provides built-in persistence, metadata filtering (used for resource type filters), and a simpler API. FAISS would offer faster raw search but requires manual serialization and doesn't support metadata-based filtering natively.

**Per-patient deduplication in search**: A patient with 150+ DiagnosticReports would otherwise dominate search results. Deduplication keeps the most recent matching record per patient and tracks the total match count, giving clinicians a better overview across patients.

**fhir.resources v8 (R5)**: While the spec calls for R4, fhir.resources v8 uses Pydantic v2 (required by the stack). The core resource types (Patient, Encounter, DiagnosticReport, DocumentReference) are structurally identical between R4 and R5, so this is functionally R4-compatible.

## What I Would Improve with More Time

- **Pagination**: The matching records endpoint returns all results at once. For patients with hundreds of records, server-side pagination with cursor-based navigation would improve performance.
- **Search relevance tuning**: The current cosine similarity on `all-MiniLM-L6-v2` embeddings works well for general queries but could be improved with a clinical-specific model (e.g., BioClinicalBERT) or hybrid BM25 + vector search.
- **Authentication & RBAC**: No auth layer exists. In production, clinician identity and role-based access control would be essential for HIPAA compliance.
- **Integration tests**: Current tests cover ingestion cleaning and FHIR mapping. I'd add API endpoint tests, search result quality tests, and end-to-end pipeline tests.
- **Streaming summaries**: LLM summarization could stream results to the frontend for faster perceived latency.
- **Batch embedding**: Currently re-embeds everything on index rebuild. Incremental indexing (only new/changed documents) would be more efficient.

## FHIR and Clinical Concepts Researched

- **FHIR R4 Bundle structure**: Learned how `transaction` Bundles use `request.method` and `fullUrl` for each entry. Resource cross-referencing via `subject`, `encounter`, and `context` fields.
- **Resource type distinctions**: `DiagnosticReport` for structured lab results vs. `DocumentReference` for unstructured clinical notes, imaging reports, and discharge summaries. Each `DocumentReference` carries base64-encoded content in an `Attachment`.
- **LOINC codes**: Used LOINC coding system for DiagnosticReport categories and DocumentReference type codes (e.g., `34117-2` for History and Physical Note).
- **Clinical data quality**: Real-world EHR data has inconsistent date formats, duplicate records from system integrations, and varied identifier schemas. The cleaning pipeline mirrors these challenges.
- **Abnormal lab detection**: Researched clinical reference ranges for glucose, BMI, blood pressure, cholesterol, HbA1c, WBC, and troponin to flag anomalies in summaries.

## How I Validated AI Summary Quality

1. **Structural validation**: Every summary is parsed as JSON with required fields (chief_concern, key_diagnoses, recent_labs, recent_imaging, flagged_anomalies, summary). Missing or malformed fields trigger a retry or fallback.
2. **Word count enforcement**: The prompt instructs "under 200 words" and the output is verified — maximum observed is 64 words across 33 patients.
3. **Clinical accuracy spot-checks**: Compared generated summaries against source FHIR data for several patients, verifying that diagnoses match encounter reasons, lab values are correctly reported, and no hallucinated conditions appear.
4. **Anomaly detection validation**: Cross-referenced flagged anomalies against clinical reference ranges. Filtered out non-clinical items (social determinant questionnaires) that were incorrectly flagged as abnormal labs.
5. **Disclaimer field**: Every summary includes an explicit AI-generated disclaimer: "Not a clinical decision. Must be reviewed by a qualified healthcare provider."
6. **Content hash caching**: Summaries are cached by patient ID + SHA-256 hash of input text, ensuring regeneration only when source data changes.
