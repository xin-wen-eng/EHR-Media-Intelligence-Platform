---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section {
    background: linear-gradient(135deg, #f8fafc 0%, #eef2ff 100%);
    font-family: -apple-system, 'Helvetica Neue', 'Segoe UI', sans-serif;
    color: #0f172a;
    padding: 60px 70px;
  }
  section::after {
    color: #64748b;
    font-size: 0.7em;
  }
  h1 {
    color: #1e3a8a;
    border-bottom: 3px solid #3b82f6;
    padding-bottom: 12px;
    font-weight: 700;
  }
  h2 {
    color: #1e3a8a;
    font-weight: 600;
    border-bottom: 2px solid #93c5fd;
    padding-bottom: 8px;
  }
  h3 {
    color: #475569;
    font-weight: 500;
  }
  strong {
    color: #1e40af;
  }
  em {
    color: #64748b;
  }
  code {
    background: #e0e7ff;
    color: #1e3a8a;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
  }
  pre {
    background: #0f172a;
    color: #e0e7ff;
    padding: 20px;
    border-radius: 8px;
    font-size: 0.72em;
    line-height: 1.4;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
  }
  pre code {
    background: transparent;
    color: inherit;
    padding: 0;
  }
  table {
    border-collapse: collapse;
    margin: 20px 0;
    width: 100%;
    background: white;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 2px 4px rgba(0,0,0,0.05);
  }
  th {
    background: #1e3a8a;
    color: white;
    padding: 12px 16px;
    text-align: left;
    font-weight: 600;
  }
  td {
    padding: 10px 16px;
    border-bottom: 1px solid #e2e8f0;
  }
  tr:last-child td {
    border-bottom: none;
  }
  ul {
    line-height: 1.7;
  }
  li {
    margin-bottom: 6px;
  }
  a {
    color: #2563eb;
    text-decoration: none;
    border-bottom: 1px dotted #93c5fd;
  }
  /* Title slide: deeper gradient + centered feel */
  section.lead {
    background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 50%, #06b6d4 100%);
    color: white;
    text-align: center;
    justify-content: center;
  }
  section.lead h1 {
    color: white;
    border: none;
    font-size: 2.4em;
  }
  section.lead h3 {
    color: #dbeafe;
    font-weight: 400;
  }
  section.lead strong {
    color: white;
  }
  section.lead a {
    color: #dbeafe;
    border-bottom-color: #dbeafe;
  }
---

<!-- _class: lead -->


# Clinical Data Intelligence Platform
### Human-in-the-Loop AI for Trustworthy Clinical Summaries

**Track 3** · Secure AI Hackathon 2026 · Team 4 · Xin Wen

github.com/xin-wen-eng/Clinical-Data-Intelligence

---

## Background & Motivation — why I picked this problem

- **US primary-care doctors work 61.8 hours a week**: most of it on charting, insurance, coding *(source: sv101 Ep. 227)*
- Healthcare data is **30% of all human-generated data, but utilized less than 5%**
- Giants and unicorns are racing to close that gap:
  OpenAI **ChatGPT for Health** · Anthropic **Claude for Healthcare** · Lilly × NVIDIA **$1 B** drug-discovery deal · **OpenEvidence**: 3-year-old unicorn, **$12 B valuation, 40% of US doctors use daily**
- I'm a student, so I just picked one specific corner to learn:
  **How do we make an AI-generated clinical summary safe enough for a clinician to trust?**

---

## Architecture — 4-layer trust, wrapped in auth

```
Session (login · PBKDF2 · 15-min idle · role-gated)
        │
Synthea EHR → Ingestion → FHIR R4 → Claude Opus summarize
                                        │
                            ┌───────────┴───────────┐
                            ↓                       ↓
                   Fact grounding             Cross-vendor
                   (source ↔ claim)           verify (GPT-5.6-terra)
                            └───────────┬───────────┘
                                        ↓
                              Human review queue
                            (approve · edit · reject)
                                        ↓
                                Audit log (every event)
```

Pre-existing: FHIR pipeline, Claude summarizer, ChromaDB search. **48h build: everything else.**

---

## Trust Layer — 4 stacked mechanisms

| # | Mechanism | What it catches |
|---|---|---|
| 1 | **Fact grounding** (internal, deterministic) | Claims not found in source FHIR |
| 2 | **Cross-vendor verify** (OpenAI GPT-5.6-terra) | Semantic hallucinations Claude wouldn't self-catch |
| 3 | **Human review** (approve / edit / reject) | Anything the two verifiers disagree on or miss |
| 4 | **Audit log** (actor + role + content hash) | Retroactive accountability |

Plus: **PII masking by default**, **role-gated actions** (clinician vs reviewer, enforced backend), **HIPAA-aligned 15-min session timeout**.

---

## Result — 33 patients, independently verified

| Verdict | Count | Recommendation |
|---|---|---|
| Clean, all claims grounded | **4** | approve |
| Partially hallucinated | **28** | edit |
| Insufficient source | **1** | reject |

- **104 hallucinated claims across 33 patients**，that is average 3.2 per patient
- **Systematic pattern**: Claude fabricates BMI numbers and blood-pressure values that don't exist in source
- **0/33 safe to auto-approve** without human review

---

## Limitations & Next Steps

**48-hour scope limits:**
- Fact grounding uses token overlap: misses paraphrase (e.g. "MI" ↔ "myocardial infarction")
- 3 seeded accounts, no SSO / MFA · 33 synthetic patients only

**Ship next:**
1.  Clinical-specific embedder (BioClinicalBERT) for stronger grounding
2. SSO + MFA · differential privacy at model boundary
3. Streaming summaries with progressive verification

---

<!-- _class: lead -->

# Thank you

Healthcare AI is a **highly specialized market** with **huge growth potential**, and **safety is non-negotiable**. This project is my small step into that space; I'd love to keep building here.

Excited to keep learning from everyone working in this field. Thanks to the organizers, mentors, and judges for making this hackathon happen.

**Clinical Data Intelligence Platform** · github.com/xin-wen-eng/Clinical-Data-Intelligence
Ready for questions: happy to open the repo, walk any layer, or run one more patient live.
