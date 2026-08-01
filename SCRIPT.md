# Full Pitch Script — 10 minutes

Total time budget: **10:00**. Slide numbers refer to `SLIDES.md`.

| Segment | Slide | Time | Running |
|---|---|---|---|
| Welcome | 1 | 0:20 | 0:20 |
| Problem | 2 | 1:00 | 1:20 |
| Live demo | — | 4:00 | 5:20 |
| Architecture | 3 | 1:00 | 6:20 |
| Trust layer | 4 | 1:30 | 7:50 |
| Result | 5 | 1:00 | 8:50 |
| Limitations | 6 | 0:45 | 9:35 |
| Thanks | 7 | 0:15 | 9:50 |

---

## 1 · Welcome (0:20)

> "Hi, I'm [YOUR NAME], and this is **Clinical Data Intelligence Platform** — my submission for Track 3, Human-in-the-Loop AI. In the next ten minutes I'll show you an AI summarization system for electronic health records, and more importantly, the four-layer trust stack i built around it in 48 hours."

**Advance to slide 2.**

---

## 2 · Background & Motivation (1:00)

> "Some context first. The Chinese tech podcast *Silicon Valley 101* did a recent episode on the US healthcare-AI arms race. One number from it stuck with me: **US primary-care doctors work 61.8 hours a week**, and most of that time goes to charting, insurance, and coding — not to patients.
>
> Healthcare data is roughly **30% of all human-generated data, and less than 5% of it is actually utilized**. That gap is why every major AI player is moving in.
>
> I'm a student, so I'm not competing with any of them. I picked one specific corner to learn from: **how do you make an AI-generated clinical summary safe enough for a clinician to actually trust?** The generation side is well-funded. The verification side, much less.
>
> To motivate the problem, I ran a quick audit. Took Claude Opus — same class of model the giants ship — pointed it at 33 patient FHIR bundles. **Every single summary contained fabricated content.** Claude invents BMI numbers when the source only has an 'obesity' tag with no number. It invents blood pressure readings when the source has zero BP records anywhere. One case is worse — the AI wrote a full heart-failure and hospice-care narrative for a patient whose entire bundle was imaging studies. Pure fabrication.
>
> If a student running 33 patients can find this pattern, deployment at scale needs a real trust layer. So I built one — a four-layer stack that sits between the AI and the clinician."

**Advance to browser tab with app.**

---

## 3 · Live Demo (4:00) — script for the screen recording

Open **http://localhost:8000** already logged out.

### 3a · Login (0:30)

> "First, HIPAA-aligned session. Everything in the app is behind a login. Sign in as `dr.wen`, password `reviewer2026`. See the top-right: acting as dr.wen, role reviewer, 15-minute session timer counting down. If we go idle 15 minutes, we're logged out automatically, logged in the audit trail."

Sign in as `dr.wen` / `reviewer2026`.

### 3b · Search + badges (0:45)

> "Search 'diabetes.' Ten results. Look at each card: patient name is 'Patient B077' — masked. MRN ends in the last four. Progress note text shows 'Patient: [PATIENT]' instead of the real name. That's the PII layer, on by default.
>
> Every card also has a status badge — PENDING or APPROVED — from the human review queue. And the amber `GPT: edit (6)` badge means our cross-vendor verifier, OpenAI GPT-5.6-terra, flagged 6 hallucinated claims in this claude opus generated summary."

Search "diabetes" in the search box.

### 3c · PII toggle (0:30)

> "PII masking is a toggle, not a permanent block. Uncheck 'PII masked' — real names appear: Janiece Bogan, real MRN. The toggle event is logged in the audit trail — we know who unmasked, when, and for how long."

### 3d · Patient detail — grounding + cross-vendor verify (1:30)

> "Click into the first patient. Top of the summary: three badges. `6/10 grounded` — that's our internal fact-grounding layer. Of ten factual claims in this summary, 6 are traceable back to a source FHIR resource. 
>
> Expand 'Diagnoses.' Hyperlipidemia — click it — grounded, three source encounters as evidence, dates and resource IDs shown. This is not summary text, this is a **link back to the primary record.**
>
> Now expand 'Flagged Anomalies' — 'obese BMI (30.1 kg/m²).' Click it — red warning, 'no matching evidence found in source FHIR bundle. This claim may be hallucinated.' Our grounding didn't find a BMI number anywhere. That matches what GPT independently concluded.
>
> Scroll down to 'Cross-vendor verification (OpenAI GPT-5.6-terra…).' Expand it. Full hallucination list from GPT — BMI, systolic BP, diastolic BP — all flagged as not in source. Two independent verifiers agree."

Open first patient. Expand Diagnoses > Hyperlipidemia. Then Flagged Anomalies > obese BMI. Then Cross-vendor block.

### 3e · Review workflow (0:15)

> "As a reviewer, I now decide. Approve, Edit, or Reject. I'll click Approve — status flips to green APPROVED. That decision, my name, timestamp, and content hash are now in the audit log."

Click Approve.

### 3f · Role-based access (0:15)

> "Log out. Sign in as `nurse.li`, a clinician account. Open the same patient. The Reviewer Decision panel now shows an amber warning: 'read-only.' Approve, Edit, Reject buttons are all disabled. it's enforced server-side."

Logout → login as `nurse.li` / `clinician2026` → open a patient.

### 3g · Audit log (0:15)

> "Finally, the Audit Log tab. Every login, view, approval, PII toggle, role attempt — all persisted with actor, role, action, patient MRN, and content hash. Local time on the left, raw UTC on hover. This is the record i'd hand to a compliance officer."

Click Audit Log tab.

**Return to slides. Advance to slide 3.**

---

## 4 · Architecture (1:00)

> "Zoomed out, here's the stack. Synthea generates realistic synthetic patient data. I normalize it to FHIR — pronounced 'fire', the healthcare industry's standard data format — release 4. Then Claude Opus summarizes. Two independent verification layers — internal fact-grounding and OpenAI cross-vendor verify — feed into the human review queue. Everything is wrapped in a session layer with PBKDF2-hashed passwords and 15-minute idle timeout, and every event lands in the audit log.
>
> The data pipeline and the Claude summarizer existed before the hackathon and are disclosed in the README. **The trust stack — grounding, cross-vendor, review, audit, PII, auth, sessions — is everything i built in the 48 hours.**"

**Advance to slide 4.**

---

## 5 · Trust Layer (1:30)

> "The core insight is that no single layer is trustworthy. So i stacked four.
>
> **Layer one, fact grounding**, is deterministic. It checks whether the words and numbers in each claim actually appear in the source FHIR bundle. It catches obvious hallucinations like invented lab values, but it can miss paraphrase — that's a known false-negative.
>
> **Layer two, cross-vendor verify** — OpenAI GPT-5.6-terra reviews the Claude summary. Different vendor, different model architecture and safety tuning, different failure modes. It catches semantic hallucinations Claude wouldn't self-detect.
>
> **Layer three, human review.** No AI summary reaches a clinician as 'trusted' without a reviewer clicking Approve, Edit, or Reject. This is Track 3's core requirement, and i treat it as the primary safety net.
>
> **Layer four, audit log.** a durable record of every decision. If a clinician later says 'I never saw that,' the log settles it.
>

**Advance to slide 5.**

---

## 6 · Result (1:00)

> "Numbers. We ran cross-vendor verification on all 33 patient summaries.
>
> **Four came back clean and safe to approve. Twenty-eight need edits. One got a full reject.**
>
> The reject case is the imaging-only bundle I mentioned earlier — MRN ending in E28C. Zero grounded claims. GPT called it 'insufficient source' — the claude summary is a fiction.
>
> Across the 33 patients, 104 hallucinated claims total. The pattern is systematic: Claude invents numeric BMI values when the source only has a categorical 'obesity' label, and it invents blood pressure readings when the source has no BP records at all.
>
> **Zero of thirty-three are safe to auto-approve.** that's why the human review layer is mandatory. 

**Advance to slide 6.**

---

## 7 · Limitations (0:45)

> "Honest limits. Grounding is token-based — it misses paraphrase like 'MI' versus 'myocardial infarction.' and i only tested  33 synthetic patients this time— real EHR scale is untested.
>
> Next 90 days: swap the general-purpose text model for one trained on medical language, so paraphrase stops being a blind spot. Replace passwords with hospital-badge single sign-on and add two-factor auth. And stream summaries sentence by sentence, verifying each claim as it's generated instead of waiting for the whole batch."

**Advance to slide 7.**

---

## 8 · Thanks (0:15–0:25)

> "Zooming back out: healthcare AI is a highly specialized market with huge growth potential, and safety in this space is non-negotiable. This project is my small step into that space, and I'd love to keep building here. Thanks to the organizers, mentors, and judges, and I'm ready for questions."

---

# Attribution

Macro statistics in the Background slide come from **硅谷101 (Silicon Valley 101) Podcast, Ep. 227 · "美国医疗市场AI争夺战"** — 61.8 hr/week working hours, 30% / <5% data utilization, OpenEvidence $12 B valuation with 40% daily-use rate, Lilly × NVIDIA $1 B deal, ChatGPT for Health / Claude for Healthcare launches.

If a judge asks the source, cite the podcast episode. Don't paraphrase into a fake first-hand claim.

---

# Rehearsal tips

- **Practice the demo section 3× with a timer** — the 4-min budget is tight, easy to overrun
- **Have the app pre-loaded with `diabetes` search results ready** so the first click is instant
- **Approve a patient before the recording** so one of the visible cards already shows APPROVED
- **Screen recorder**: QuickTime (Mac) — Cmd+Shift+5 → Record Portion → include audio
- **Zoom in** if screen text is small: `Cmd + +` in the browser 1-2 clicks before recording
- **If you fumble a step during live judging**, skip it and keep the flow — evaluators care about the story, not perfect clicks

---

# Marp — how to use it

Marp turns your `SLIDES.md` (with the `marp: true` frontmatter) into slides.

**Fastest way — VS Code extension:**
1. Install [Marp for VS Code](https://marketplace.visualstudio.com/items?itemName=marp-team.marp-vscode)
2. Open `SLIDES.md`
3. Cmd+Shift+P → "Marp: Open Preview" (side-by-side live preview)
4. Cmd+Shift+P → "Marp: Export slide deck" → choose PDF, PPTX, or HTML

**CLI (also fast):**

```bash
npm install -g @marp-team/marp-cli
marp SLIDES.md --pdf                # generates SLIDES.pdf
marp SLIDES.md --pptx               # generates SLIDES.pptx (edit in Keynote/PowerPoint)
marp SLIDES.md --html --allow-local-files
```

**Present directly (no export needed):**

```bash
marp SLIDES.md --preview            # opens preview window; Cmd+F for fullscreen
```

**Syntax reminders:**
- `---` (three dashes on its own line) = new slide
- Frontmatter block at the top controls theme / size / pagination
- Themes: `default`, `gaia`, `uncover`. Change with `theme: gaia`
- For a title slide, use `<!-- _class: lead -->` before the H1

**If you want a specific look**, tell me — I can add custom CSS to the frontmatter.
