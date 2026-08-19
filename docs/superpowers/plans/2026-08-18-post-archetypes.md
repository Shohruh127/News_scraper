# Post Archetypes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each post a shape that matches what it is — a release, a protocol, a risk, a rule, a finding or a company — instead of one layout for everything.

**Architecture:** The English editorial stage gains an `archetype` field and one matching detail block, chosen with boundary definitions in the prompt. The translation stage derives its schema from whatever the English stage produced, so no archetype-specific translation code exists. Rendering picks a template from the archetype, falling back to today's template whenever anything is missing.

**Tech Stack:** Django 6.0 templates, Pydantic 2 (`create_model`), MiMo via `json_schema` + `strict: true`, Ollama `gemma4:latest`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-18-post-archetypes-design.md`

## Global Constraints

- Ruff `line-length = 100`, `target-version = "py313"`. `uv run ruff check .` must pass
- The suite must stay green: `uv run pytest -q` → **134 passed** before this plan
- Nothing inside a detail block is `required` in the JSON schema. A strict schema does not make a model know an answer, it makes it produce one — that is how a default sampling parameter acquired a `HIGH` severity during measurement
- The English stage emits `*_en` only. `*_uz` comes from the translation stage (ADR-005)
- `evidence_level` keeps its frozen enum `vendor_claim_only | multiple_evidence`. Do not redefine it
- Do not touch the "Keep in English" term list in `TRANSLATION_PROMPT` — that list belongs to P3
- Tests run offline against fixed payloads. Never assert a live model's output
- The archetype block is an enhancement: its absence simplifies the layout and never loses the post

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `docs/CONTENT_SCHEMA.md` | the six boundary definitions, as the contract | modify §5 |
| `apps/digest/llm.py` | archetype enum, detail schemas, Pydantic models, prompt, derived translation schema | modify |
| `apps/digest/ranking.py` | archetype → template selection, context fields | modify |
| `apps/digest/templates/digest/item_base.html` | the invariant frame both blocks hang from | create |
| `apps/digest/templates/digest/item_<archetype>.html` | six templates | create |
| `config/settings.py` | `TELEGRAM_LINK_PREVIEW` default | modify |
| `tests/test_editorial.py` | schema and flattening tests | modify |
| `tests/test_publish.py` | template selection and rendering tests | modify |

### Context an engineer new to this repo needs

The editorial pipeline has **two** LLM stages, deliberately. `editorial_en` runs on MiMo and
produces English; `editorial_uz` runs on local Ollama and translates it. They are separate so a
bad summary can be traced to comprehension or to translation. Never merge them.

`_editorial_call(prompt, schema, model_cls, num_predict, client, provider, ollama_model)` sends
the request and validates the reply against `model_cls`, a Pydantic model. Both the JSON schema
and the Pydantic model must therefore change together whenever fields change.

`_item_data(item)` in `ranking.py` builds one context dict shared by `render_item_post` and
`render_item_appendix`, so the two templates cannot drift apart. Archetype fields go into that
same dict.

`translation_gates.validate_translation(en_fields, uz_fields)` takes two flat dicts and joins
their values. Nested dicts would stringify badly, which is why archetype fields are flattened
before translation rather than passed as a nested block.

---

## Task 1: Archetype and its detail blocks in the English stage

**Files:**
- Modify: `docs/CONTENT_SCHEMA.md` — new subsection at the end of §5
- Modify: `apps/digest/llm.py` — models near line 121, schema near line 185, prompt near line 225
- Test: `tests/test_editorial.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `ARCHETYPES: tuple[str, ...]`, `ARCHETYPE_DEFINITIONS: str`, and an `EditorialEn`
  model carrying `archetype: str` plus six optional `<archetype>_details` fields. Task 2 reads
  `payload["archetype"]` and `payload[f"{archetype}_details"]`; Task 3 reads the same two.

- [ ] **Step 1: Add the definitions to the contract**

Append this to the end of §5 in `docs/CONTENT_SCHEMA.md`, before the `---` that starts §6:

```markdown
### `archetype` — the shape of the post

Measured 2026-08-18 on six real items: with no definitions the model scored 0/6 and filled six
irrelevant detail blocks, including `HIGH` severity for a change to a default sampling parameter.
With the definitions below it scored 5/6 and filled none. They are load-bearing.

| value | boundary |
|---|---|
| `release` | A named product or model shipped a new version. A changelog, a release note, a version number. This is the default for any version bump. |
| `agent_protocol` | A protocol or framework for connecting tools to models, where the news IS the connection mechanism. Not a runtime that happens to run agents. |
| `risk_hardening` | A risk, a weakness, or work done to reduce one. There must be something that can go wrong and someone acting on it. |
| `policy` | A rule issued by a government or standards body, with someone obliged to comply. Pricing is not policy. |
| `research` | A method or a finding with a claim and evidence, not a shipped artifact. |
| `company_product` | A company entering a market or making a commercial launch, where the company is the news rather than the version. |

Exactly one `<archetype>_details` block is filled. Every field inside it is optional in the
schema: 11 stored security articles contained one CVE identifier, no CVSS score and no
affected-version range, so a required field would be invented rather than found.
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_editorial.py`:

```python
def test_archetype_enum_matches_the_detail_blocks():
    """Every archetype must have a block, and every block an archetype."""
    from apps.digest.llm import ARCHETYPES, EDITORIAL_EN_SCHEMA

    props = EDITORIAL_EN_SCHEMA["properties"]
    assert set(props["archetype"]["enum"]) == set(ARCHETYPES)
    for name in ARCHETYPES:
        assert f"{name}_details" in props, f"{name} has no detail block"


def test_no_detail_field_is_required_in_the_schema():
    """A strict schema does not make the model know an answer, it makes it produce one.

    Measured 2026-08-18: a change to a default sampling parameter was given HIGH severity.
    """
    from apps.digest.llm import ARCHETYPES, EDITORIAL_EN_SCHEMA

    top_required = EDITORIAL_EN_SCHEMA["required"]
    for name in ARCHETYPES:
        block = EDITORIAL_EN_SCHEMA["properties"][f"{name}_details"]
        assert not block.get("required"), f"{name}_details marks fields required"
        assert f"{name}_details" not in top_required


def test_editorial_model_accepts_one_block_and_none():
    """The model validates a payload with a single block, and one with no block at all."""
    from apps.digest.llm import EditorialEn

    common = {
        "headline_en": "Ollama v0.32.10 changes the default repeat penalty",
        "summary_en": "The release changes a default and speeds up prefill.",
        "why_it_matters_en": "It standardises behaviour across engines.",
        "leadership_en": "A routine update with a measurable speedup.",
        "uzbekistan_application_en": "Local teams running Ollama benefit directly.",
        "technical": {
            "what_was_built": "Ollama v0.32.10",
            "limitations": "Applies to NVFP4 MLX models only",
            "local_deployable": True,
        },
        "evidence_level": "vendor_claim_only",
    }

    with_block = EditorialEn(
        archetype="release",
        release_details={"what_changed_en": "repeat_penalty now defaults to 1.0"},
        **common,
    )
    assert with_block.release_details.what_changed_en.startswith("repeat_penalty")
    assert with_block.risk_hardening_details is None

    without_block = EditorialEn(archetype="release", **common)
    assert without_block.release_details is None


def test_archetype_definitions_are_in_the_prompt():
    """The definitions moved accuracy from 0/6 to 5/6, so their absence is a defect."""
    from apps.digest.llm import ARCHETYPES, EDITORIAL_EN_PROMPT

    for name in ARCHETYPES:
        assert name in EDITORIAL_EN_PROMPT, f"{name} is not defined in the prompt"
    assert "Pricing is not policy" in EDITORIAL_EN_PROMPT
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_editorial.py -q -k "archetype or detail_field or one_block"
```

Expected: FAIL, `ImportError: cannot import name 'ARCHETYPES'`

- [ ] **Step 4: Add the Pydantic models**

In `apps/digest/llm.py`, immediately **before** `class EditorialEn` (near line 121), add:

```python
#: The shape of a post. Boundary definitions live in CONTENT_SCHEMA.md §5 and are quoted into
#: EDITORIAL_EN_PROMPT verbatim, because measurement showed they carry the accuracy: without
#: them the model scored 0/6 and filled six irrelevant blocks; with them, 5/6 and none.
ARCHETYPES = (
    "release",
    "agent_protocol",
    "risk_hardening",
    "policy",
    "research",
    "company_product",
)


class ReleaseDetails(BaseModel):
    what_changed_en: str = ""
    benchmarks_en: str = ""
    availability_en: str = ""


class AgentProtocolDetails(BaseModel):
    connects_en: str = ""
    deployment_en: str = ""


class RiskHardeningDetails(BaseModel):
    """No severity enum. Of 11 stored security articles, none carried a CVSS score, so a
    three-value enum with no "not stated" option would be invented nine times in eleven.
    A stated CVE or severity belongs inside `risk_en`, quoted rather than classified."""

    risk_en: str = ""
    mitigation_en: str = ""
    residual_en: str = ""


class PolicyDetails(BaseModel):
    who_issued_en: str = ""
    who_must_comply_en: str = ""
    deadline_en: str = ""


class ResearchDetails(BaseModel):
    #: Deliberately not `evidence_level`, which is a frozen enum meaning something else.
    claim_en: str = ""
    evidence_strength_en: str = ""
    reproducible_en: str = ""


class CompanyProductDetails(BaseModel):
    what_they_do_en: str = ""
    availability_en: str = ""
```

Then add these fields to `class EditorialEn`, after `evidence_level`:

```python
    archetype: str = "release"
    release_details: ReleaseDetails | None = None
    agent_protocol_details: AgentProtocolDetails | None = None
    risk_hardening_details: RiskHardeningDetails | None = None
    policy_details: PolicyDetails | None = None
    research_details: ResearchDetails | None = None
    company_product_details: CompanyProductDetails | None = None
```

- [ ] **Step 5: Add the blocks to the JSON schema**

In `EDITORIAL_EN_SCHEMA` (near line 185), add these entries to `"properties"`, after
`evidence_level`:

```python
        "archetype": {"type": "string", "enum": list(ARCHETYPES)},
        "release_details": {
            "type": "object",
            "properties": {
                "what_changed_en": {"type": "string"},
                "benchmarks_en": {"type": "string"},
                "availability_en": {"type": "string"},
            },
        },
        "agent_protocol_details": {
            "type": "object",
            "properties": {
                "connects_en": {"type": "string"},
                "deployment_en": {"type": "string"},
            },
        },
        "risk_hardening_details": {
            "type": "object",
            "properties": {
                "risk_en": {"type": "string"},
                "mitigation_en": {"type": "string"},
                "residual_en": {"type": "string"},
            },
        },
        "policy_details": {
            "type": "object",
            "properties": {
                "who_issued_en": {"type": "string"},
                "who_must_comply_en": {"type": "string"},
                "deadline_en": {"type": "string"},
            },
        },
        "research_details": {
            "type": "object",
            "properties": {
                "claim_en": {"type": "string"},
                "evidence_strength_en": {"type": "string"},
                "reproducible_en": {"type": "string"},
            },
        },
        "company_product_details": {
            "type": "object",
            "properties": {
                "what_they_do_en": {"type": "string"},
                "availability_en": {"type": "string"},
            },
        },
```

Add `"archetype"` to the schema's top-level `"required"` list. Do **not** add any `_details`
key to it, and do **not** give any block its own `"required"`.

- [ ] **Step 6: Put the definitions in the prompt**

In `apps/digest/llm.py`, define this immediately above `EDITORIAL_EN_PROMPT` (near line 225):

```python
#: Quoted verbatim from CONTENT_SCHEMA.md §5. Measured: 0/6 without, 5/6 with.
ARCHETYPE_DEFINITIONS = (
    "## Choose exactly one archetype\n"
    "release          A named product or model shipped a new version. A changelog, a release\n"
    "                 note, a version number. This is the default for any version bump.\n"
    "agent_protocol   A protocol or framework for connecting tools to models, where the news\n"
    "                 IS the connection mechanism. Not a runtime that happens to run agents.\n"
    "risk_hardening   A risk, a weakness, or work done to reduce one. There must be something\n"
    "                 that can go wrong and someone acting on it.\n"
    "policy           A rule issued by a government or standards body, with someone obliged to\n"
    "                 comply. Pricing is not policy.\n"
    "research         A method or a finding with a claim and evidence, not a shipped artifact.\n"
    "company_product  A company entering a market or making a commercial launch, where the\n"
    "                 company is the news rather than the version.\n\n"
    "Fill ONLY the detail block for the archetype you chose. Leave every other block absent.\n"
    "Omit any field whose value is not stated in the article. Never infer a severity.\n\n"
)
```

Then insert it into `EDITORIAL_EN_PROMPT`. The prompt is a parenthesised string concatenation
beginning:

```python
EDITORIAL_EN_PROMPT = (
    "You are a senior editor for a daily AI-engineering digest read by engineering "
    "leaders and AI engineers in Uzbekistan.\n\n"
    "Write the English analysis of the article below. Return JSON only.\n\n"
```

Add `ARCHETYPE_DEFINITIONS` as its own concatenated element directly after that third line, so
the definitions arrive before the voice rules.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
uv run pytest tests/test_editorial.py -q
```

Expected: PASS

- [ ] **Step 8: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `138 passed` and `All checks passed!`. If an existing editorial test fails, it is
asserting the exact set of schema keys — report it rather than loosening the assertion.

- [ ] **Step 9: Commit**

```bash
git add docs/CONTENT_SCHEMA.md apps/digest/llm.py tests/test_editorial.py
git commit -m "Give each post an archetype, chosen with boundary definitions"
```

---

## Task 2: Derive the translation schema from the English payload

**Files:**
- Modify: `apps/digest/llm.py` — `TRANSLATION_SCHEMA` near line 263, the translation call near line 965
- Test: `tests/test_editorial.py`

**Interfaces:**
- Consumes: `ARCHETYPES` and the `<archetype>_details` payload shape from Task 1
- Produces: `archetype_fields(payload: dict) -> dict[str, str]` and
  `translation_schema_for(fields: dict) -> dict`, plus a dynamically built Pydantic model. Task 3
  does not use either; it reads the translated `*_uz` keys out of the stored payload.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_editorial.py`:

```python
def test_archetype_fields_flattens_only_the_chosen_block():
    """Only the chosen block is flattened, and only its non-empty strings."""
    from apps.digest.llm import archetype_fields

    payload = {
        "archetype": "release",
        "release_details": {
            "what_changed_en": "repeat_penalty defaults to 1.0",
            "benchmarks_en": "",
            "availability_en": "   ",
        },
        "policy_details": {"who_issued_en": "should be ignored"},
    }
    assert archetype_fields(payload) == {"what_changed_en": "repeat_penalty defaults to 1.0"}


def test_archetype_fields_is_empty_when_there_is_no_block():
    """A post with no detail block translates its common fields and nothing else."""
    from apps.digest.llm import archetype_fields

    assert archetype_fields({"archetype": "release"}) == {}
    assert archetype_fields({}) == {}


def test_translation_schema_follows_the_fields_it_is_given():
    """A block absent from the schema cannot be filled by a model that felt like filling it.

    Measured 2026-08-18: given six visible blocks and no definitions, the model filled six
    irrelevant ones.
    """
    from apps.digest.llm import translation_schema_for

    schema = translation_schema_for({"headline_en": "x", "summary_en": "y", "what_changed_en": "z"})
    assert set(schema["properties"]) == {"headline_uz", "summary_uz", "what_changed_uz"}
    assert set(schema["required"]) == {"headline_uz", "summary_uz", "what_changed_uz"}
    assert "policy_details" not in schema["properties"]


def test_translation_schema_only_rewrites_a_trailing_suffix():
    """`_en` is replaced at the end of the key, never in the middle of a word."""
    from apps.digest.llm import translation_schema_for

    schema = translation_schema_for({"deployment_en": "a", "residual_en": "b"})
    assert set(schema["properties"]) == {"deployment_uz", "residual_uz"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_editorial.py -q -k "archetype_fields or translation_schema"
```

Expected: FAIL, `ImportError: cannot import name 'archetype_fields'`

- [ ] **Step 3: Write the two functions**

In `apps/digest/llm.py`, add both immediately after `TRANSLATION_SCHEMA` (near line 280):

```python
#: Fields translated for every post regardless of archetype.
COMMON_TRANSLATED_FIELDS = (
    "headline_en",
    "summary_en",
    "why_it_matters_en",
    "leadership_en",
    "uzbekistan_application_en",
)


def archetype_fields(payload: dict) -> dict[str, str]:
    """The chosen archetype's detail block, flattened to top-level keys.

    Flat rather than nested because `translation_gates.validate_translation` joins the values
    of both dicts, and a nested dict stringifies into its own repr. The gates were built
    generic over field names; only the schema was not.
    """
    block = payload.get(f"{payload.get('archetype', '')}_details") or {}
    return {k: v for k, v in block.items() if isinstance(v, str) and v.strip()}


def translation_schema_for(fields: dict) -> dict:
    """A translation schema carrying exactly the fields the English stage produced.

    Deriving it rather than fixing it removes the opportunity to fill an irrelevant block
    instead of instructing against it.
    """
    props = {(k[:-3] + "_uz" if k.endswith("_en") else k): {"type": "string"} for k in fields}
    return {"type": "object", "properties": props, "required": list(props)}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_editorial.py -q -k "archetype_fields or translation_schema"
```

Expected: PASS, 4 passed

- [ ] **Step 5: Use them in the translation call**

In `analyse_for_digest_logic`, the translation stage currently builds its fields like this
(near line 965):

```python
            fields = {k: en.payload.get(k, "") for k in (
                "headline_en", "summary_en", "why_it_matters_en",
                "leadership_en", "uzbekistan_application_en",
            )}
            payload, latency_ms, model_tag = _editorial_call(
                prompt=TRANSLATION_PROMPT.format(
                    fields=json.dumps(fields, ensure_ascii=False, indent=2)
                ),
                schema=TRANSLATION_SCHEMA,
                model_cls=Translation,
```

Replace those lines with:

```python
            fields = {k: en.payload.get(k, "") for k in COMMON_TRANSLATED_FIELDS}
            fields.update(archetype_fields(en.payload))
            uz_schema = translation_schema_for(fields)
            uz_model = create_model(
                "TranslationDynamic",
                **{k: (str, ...) for k in uz_schema["properties"]},
            )
            payload, latency_ms, model_tag = _editorial_call(
                prompt=TRANSLATION_PROMPT.format(
                    fields=json.dumps(fields, ensure_ascii=False, indent=2)
                ),
                schema=uz_schema,
                model_cls=uz_model,
```

Add `create_model` to the pydantic import at the top of the file. It currently reads:

```python
from pydantic import BaseModel, Field, ValidationError
```

Make it:

```python
from pydantic import BaseModel, Field, ValidationError, create_model
```

Then find the two later references to `schema=TRANSLATION_SCHEMA` inside the gate-retry branch
(near line 998) and change both to `schema=uz_schema`. Leave `TRANSLATION_SCHEMA` and the
`Translation` model defined — `tests/test_llm.py` refers to them.

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `142 passed` and `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add apps/digest/llm.py tests/test_editorial.py
git commit -m "Derive the translation schema from what the English stage produced"
```

---

## Task 3: Template selection with a fallback

**Files:**
- Create: `apps/digest/templates/digest/item_base.html`
- Modify: `apps/digest/ranking.py` — `_item_data` near line 405, `render_item_post` near line 415
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: `payload["archetype"]` and `payload[f"{archetype}_details"]` from Task 1
- Produces: `ARCHETYPE_TEMPLATES: dict[str, str]` and a `render_item_post` that selects from it.
  `_item_data` gains an `archetype` key and one `detail` dict. Task 4's templates read
  `{{ detail.what_changed_uz }}` and the like.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_publish.py`:

```python
@pytest.mark.django_db
def test_unknown_archetype_falls_back_without_raising(digest_item_factory):
    """An archetype we do not recognise must simplify the layout, never lose the post."""
    item = digest_item_factory(archetype="teleportation", detail={})

    html = ranking.render_item_post(item)

    assert "Yangi model chiqdi" in html


@pytest.mark.django_db
def test_missing_required_detail_falls_back(digest_item_factory):
    """A release with no `what_changed_uz` renders as a plain post rather than an empty one."""
    item = digest_item_factory(archetype="release", detail={})

    html = ranking.render_item_post(item)

    assert "Yangi model chiqdi" in html
    assert "🚀" not in html


@pytest.mark.django_db
def test_archetype_selects_its_template(digest_item_factory):
    """A release with its required field renders the release template."""
    item = digest_item_factory(
        archetype="release",
        detail={"what_changed_uz": "repeat_penalty endi 1.0 ga teng"},
    )

    html = ranking.render_item_post(item)

    assert "🚀" in html
    assert "repeat_penalty endi 1.0 ga teng" in html
```

And add this fixture near the top of `tests/test_publish.py`, after the existing imports. That
file already imports everything the fixture needs — `pytest`, `timezone`, `ranking`, `Article`,
`Digest`, `DigestItem`, `Source` and `make_editorial` — so no import line changes. Its existing
`zero_send_delay` autouse fixture only sets a send delay and does not interfere.

```python
@pytest.fixture
def digest_item_factory(db):
    """Build one renderable DigestItem with a chosen archetype and detail block."""

    def _make(archetype, detail):
        source = Source.objects.create(
            name=f"src_{archetype}",
            connector=Source.Connector.RSS,
            url="https://example.com/rss",
            priority=80,
        )
        article = Article.objects.create(
            source=source,
            canonical_url=f"https://example.com/{archetype}",
            content_hash=f"h_{archetype}",
            title="Fixture article",
            extracted_text="Body " * 60,
            status=Article.Status.CLASSIFIED,
        )
        en, uz = make_editorial(article)
        en.payload["archetype"] = archetype
        en.save(update_fields=["payload"])
        uz.payload.update(detail)
        uz.save(update_fields=["payload"])

        digest = Digest.objects.create(digest_date=timezone.localdate())
        return DigestItem.objects.create(digest=digest, article=article, position=1, score=0.9)

    return _make
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_publish.py -q -k "archetype or fallback or falls_back"
```

Expected: FAIL — `test_archetype_selects_its_template` fails because no rocket appears; the two
fallback tests pass already, and must keep passing.

- [ ] **Step 3: Create the base template**

Create `apps/digest/templates/digest/item_base.html`:

```django
{% comment %}
The invariant frame. Six archetype templates extend this and fill two blocks.

Shared rather than copied because the headline link, the tag line and the
`<blockquote expandable>` syntax would otherwise be written six times. That syntax was
verified against the live API rather than assumed, and writing it six times is six chances
to get it wrong.

  lead    the visible part — this is where channel uniformity is decided, because a reader
          scrolling past sees only this
  detail  archetype-specific lines and their order, inside the collapsed block
{% endcomment %}{% block lead %}🔥 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}

{{ summary_uz }}
<blockquote expandable>{% block detail %}{% endblock %}{% if why_it_matters_uz %}💡 <b>Nima uchun muhim</b>
{{ why_it_matters_uz }}{% endif %}{% if leadership_uz %}

💼 <b>Boshqaruv uchun</b>
{{ leadership_uz }}{% endif %}{% if uzbekistan_application_uz %}

🇺🇿 <b>O'zbekistonda</b>
{{ uzbekistan_application_uz }}{% endif %}{% if secondary_sources %}

🔗 {% for sec in secondary_sources %}<a href="{{ sec.url }}">{{ sec.source_name|default:sec.title }}</a>{% if not forloop.last %} · {% endif %}{% endfor %}{% endif %}</blockquote>
```

- [ ] **Step 4: Add selection to `ranking.py`**

Add this near the other module-level constants, above `render_item_post`:

```python
#: archetype -> template. A value missing from this map falls back to the plain post, which is
#: the rule the whole feature rests on: the archetype block is an enhancement, and its absence
#: simplifies the layout rather than losing the post.
ARCHETYPE_TEMPLATES = {
    "release": "digest/item_release.html",
    "agent_protocol": "digest/item_agent_protocol.html",
    "risk_hardening": "digest/item_risk_hardening.html",
    "policy": "digest/item_policy.html",
    "research": "digest/item_research.html",
    "company_product": "digest/item_company_product.html",
}

#: The field each template cannot render without. Absent -> fall back.
ARCHETYPE_REQUIRED = {
    "release": ("what_changed_uz",),
    "agent_protocol": ("connects_uz",),
    "risk_hardening": ("risk_uz", "mitigation_uz"),
    "policy": ("who_issued_uz", "who_must_comply_uz"),
    "research": ("claim_uz",),
    "company_product": ("what_they_do_uz",),
}
```

Replace `render_item_post` with:

```python
def render_item_post(item: DigestItem) -> str:
    """Render one channel post, choosing a template from the article's archetype."""
    data = _item_data(item)
    archetype = data.get("archetype", "")
    template = ARCHETYPE_TEMPLATES.get(archetype)

    if template is None:
        if archetype:
            log.info(
                "Unknown archetype %r on item #%s; using the plain post", archetype, item.position
            )
        return render_to_string("digest/item_post.html", data).strip()

    missing = [f for f in ARCHETYPE_REQUIRED[archetype] if not data["detail"].get(f)]
    if missing:
        log.warning(
            "Archetype %s on item #%s lacks %s; using the plain post",
            archetype,
            item.position,
            ", ".join(missing),
        )
        return render_to_string("digest/item_post.html", data).strip()

    return render_to_string(template, data).strip()
```

Add `import logging` and `log = logging.getLogger(__name__)` at the top of `ranking.py` if they
are not already there.

- [ ] **Step 5: Add the two context keys**

In `_item_data`, add these to the returned dict, after `"maturity"`:

```python
        # The archetype lives in the English payload; its translated detail lines live in the
        # Uzbek one, flattened there by the translation stage.
        "archetype": en_payload.get("archetype", ""),
        "detail": {
            k: v for k, v in uz_payload.items()
            if k.endswith("_uz") and k not in _COMMON_UZ_KEYS
        },
```

And define this constant above `_item_data`:

```python
#: Translated fields every post has. Anything else ending in `_uz` came from an archetype block.
_COMMON_UZ_KEYS = frozenset(
    {
        "headline_uz",
        "summary_uz",
        "why_it_matters_uz",
        "leadership_uz",
        "uzbekistan_application_uz",
    }
)
```

- [ ] **Step 6: Create a minimal release template so the third test can pass**

Create `apps/digest/templates/digest/item_release.html`:

```django
{% extends "digest/item_base.html" %}
{% block lead %}🚀 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}📦 <b>Nima o'zgardi</b>
{{ detail.what_changed_uz }}

{% endblock %}
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
uv run pytest tests/test_publish.py -q
```

Expected: PASS

- [ ] **Step 8: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `145 passed` and `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add apps/digest/templates/digest/item_base.html \
        apps/digest/templates/digest/item_release.html \
        apps/digest/ranking.py tests/test_publish.py
git commit -m "Select a post template from the archetype, falling back when anything is missing"
```

---

## Task 4: The five remaining templates

**Files:**
- Create: `item_agent_protocol.html`, `item_risk_hardening.html`, `item_policy.html`,
  `item_research.html`, `item_company_product.html` in `apps/digest/templates/digest/`
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: `ARCHETYPE_TEMPLATES`, `ARCHETYPE_REQUIRED` and the `detail` context key from Task 3
- Produces: no callable

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_publish.py`:

```python
ARCHETYPE_CASES = [
    (
        "agent_protocol",
        "🔌",
        {"connects_uz": "IDE ni ma'lumotlar bazasiga ulaydi"},
        {"deployment_uz": "Self-hosted va Ollama bilan ishlaydi"},
    ),
    (
        "risk_hardening",
        "🛡",
        {"risk_uz": "Suv belgisini o'chirish oson", "mitigation_uz": "Kriptografik imzo qo'shildi"},
        {"residual_uz": "Qisqa matnlarda hamon ishonchsiz"},
    ),
    (
        "policy",
        "⚖️",
        {
            "who_issued_uz": "Yevropa Ittifoqi",
            "who_must_comply_uz": "Generativ model provayderlari",
        },
        {"deadline_uz": "2027-yil 1-avgust"},
    ),
    (
        "research",
        "🔬",
        {"claim_uz": "Ixchamlash uzun sessiyalarni saqlaydi"},
        {
            "evidence_strength_uz": "Bitta laboratoriya, mustaqil takror yo'q",
            "reproducible_uz": "Kod ochiq emas",
        },
    ),
    (
        "company_product",
        "🏢",
        {"what_they_do_uz": "Konteyner obrazlarini avtomatik tozalaydi"},
        {"availability_uz": "Enterprise mijozlar uchun ochiq"},
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize("archetype, emoji, required, optional", ARCHETYPE_CASES)
def test_archetype_renders_with_every_field(
    digest_item_factory, archetype, emoji, required, optional
):
    item = digest_item_factory(archetype=archetype, detail={**required, **optional})

    html = ranking.render_item_post(item)

    assert emoji in html
    for value in {**required, **optional}.values():
        assert value in html


@pytest.mark.django_db
@pytest.mark.parametrize("archetype, emoji, required, optional", ARCHETYPE_CASES)
def test_archetype_renders_with_no_optional_fields(
    digest_item_factory, archetype, emoji, required, optional
):
    """The path most posts actually take.

    `benchmarks` is populated 40% of the time, so two release posts in three walk this branch.
    The full case is the one easy to imagine and the rarer one in production.
    """
    item = digest_item_factory(archetype=archetype, detail=required)

    html = ranking.render_item_post(item)

    assert emoji in html
    for value in required.values():
        assert value in html
    for value in optional.values():
        assert value not in html


@pytest.mark.django_db
@pytest.mark.parametrize("archetype, emoji, required, optional", ARCHETYPE_CASES)
def test_visible_part_stays_short(digest_item_factory, archetype, emoji, required, optional):
    """Everything new lives inside the collapsed block, so the visible length must not grow."""
    item = digest_item_factory(archetype=archetype, detail={**required, **optional})

    visible = ranking.render_item_post(item).split("<blockquote expandable>")[0]

    assert len(visible) < 600
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_publish.py -q -k "archetype_renders or visible_part"
```

Expected: FAIL, `TemplateDoesNotExist: digest/item_agent_protocol.html`

- [ ] **Step 3: Create the five templates**

`apps/digest/templates/digest/item_agent_protocol.html`:

```django
{% extends "digest/item_base.html" %}
{% block lead %}🔌 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}🔗 <b>Nimani nimaga ulaydi</b>
{{ detail.connects_uz }}{% if detail.deployment_uz %}

🖥 <b>Joylashtirish</b>
{{ detail.deployment_uz }}{% endif %}

{% endblock %}
```

`apps/digest/templates/digest/item_risk_hardening.html` — the risk comes first, before "why it
matters", because that is the news:

```django
{% extends "digest/item_base.html" %}
{% block lead %}🛡 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}⚠️ <b>Xavf</b>
{{ detail.risk_uz }}

🩹 <b>Nima qilingan</b>
{{ detail.mitigation_uz }}{% if detail.residual_uz %}

🔍 <b>Nima hal bo'lmagan</b>
{{ detail.residual_uz }}{% endif %}

{% endblock %}
```

`apps/digest/templates/digest/item_policy.html`:

```django
{% extends "digest/item_base.html" %}
{% block lead %}⚖️ <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}🏛 <b>Kim chiqardi</b>
{{ detail.who_issued_uz }}

👥 <b>Kim majbur</b>
{{ detail.who_must_comply_uz }}{% if detail.deadline_uz %}

📅 <b>Qachondan</b>
{{ detail.deadline_uz }}{% endif %}

{% endblock %}
```

`apps/digest/templates/digest/item_research.html`:

```django
{% extends "digest/item_base.html" %}
{% block lead %}🔬 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}🧪 <b>Da'vo</b>
{{ detail.claim_uz }}{% if detail.evidence_strength_uz %}

📊 <b>Dalil kuchi</b>
{{ detail.evidence_strength_uz }}{% endif %}{% if detail.reproducible_uz %}

♻️ <b>Takrorlanadimi</b>
{{ detail.reproducible_uz }}{% endif %}

{% endblock %}
```

`apps/digest/templates/digest/item_company_product.html`:

```django
{% extends "digest/item_base.html" %}
{% block lead %}🏢 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}💼 <b>Nima qiladi</b>
{{ detail.what_they_do_uz }}{% if detail.availability_uz %}

🔓 <b>Kimga ochiq</b>
{{ detail.availability_uz }}{% endif %}

{% endblock %}
```

- [ ] **Step 4: Extend the release template with its optional fields**

Replace the body of `apps/digest/templates/digest/item_release.html` created in Task 3:

```django
{% extends "digest/item_base.html" %}
{% block lead %}🚀 <b><a href="{{ url }}">{{ headline_uz }}</a></b>
<i>#{{ topic }} · {{ source_name }}</i>{% endblock %}
{% block detail %}📦 <b>Nima o'zgardi</b>
{{ detail.what_changed_uz }}{% if detail.benchmarks_uz %}

⚡ <b>O'lchovlar</b>
{{ detail.benchmarks_uz }}{% endif %}{% if detail.availability_uz %}

🔓 <b>Qanday olinadi</b>
{{ detail.availability_uz }}{% endif %}

{% endblock %}
```

Then add the release case to `ARCHETYPE_CASES` in the test file, as the first entry:

```python
(
    (
        "release",
        "🚀",
        {"what_changed_uz": "repeat_penalty endi 1.0 ga teng"},
        {
            "benchmarks_uz": "Prefill 7–8% tezroq",
            "availability_uz": "GitHub relizlaridan yuklab olinadi",
        },
    ),
)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_publish.py -q
```

Expected: PASS, 18 new cases (6 archetypes × 3 tests)

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `163 passed` and `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add apps/digest/templates/digest/ tests/test_publish.py
git commit -m "Add the five remaining archetype templates"
```

---

## Task 5: Turn on the link preview

**Files:**
- Modify: `config/settings.py:149`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: nothing. `publish.py` already sends `link_preview_options` built from this setting
- Produces: nothing

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings.py`:

```python
def test_link_preview_is_on_by_default():
    """Images arrive through the page's own og:image rather than sendPhoto.

    sendPhoto caps a caption at 1024 characters against a 1360-character post, and since about
    one article in ten has no image it would keep sendMessage as well — two post shapes, two
    length limits, two edit paths. The preview keeps one of each.
    """
    from django.conf import settings

    assert settings.TELEGRAM_LINK_PREVIEW is True
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_settings.py -q -k link_preview
```

Expected: FAIL, `assert False is True`

- [ ] **Step 3: Flip the default**

In `config/settings.py`, this block currently reads:

```python
#: Off by default: 15 posts a day each with a preview card makes the channel very
#: tall, and previews of arXiv and GitHub pages are generic. The headline is a link.
TELEGRAM_LINK_PREVIEW = env.bool("TELEGRAM_LINK_PREVIEW", default=False)
```

Replace it with:

```python
#: On by default since the archetype work. The earlier objection -- a preview card per post
#: makes the channel very tall -- is answered by `prefer_small_media`, which publish.py already
#: sends, and by the subject diversity rule, which removed the repeated posts that were also
#: the ones carrying near-identical preview images.
#:
#: The alternative was sendPhoto, rejected because its 1024-character caption cap does not fit
#: a 1360-character post, and because roughly one article in ten has no image, which would keep
#: sendMessage alongside it: two post shapes, two length limits, and two edit paths.
TELEGRAM_LINK_PREVIEW = env.bool("TELEGRAM_LINK_PREVIEW", default=True)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_settings.py -q -k link_preview
```

Expected: PASS

- [ ] **Step 5: Apply it to the running containers**

```bash
docker compose up -d worker-publish
```

`docker compose restart` does **not** re-read `.env`; only recreating the container does. If
`TELEGRAM_LINK_PREVIEW` is set explicitly in `.env`, change it there too — an explicit value
overrides the default.

- [ ] **Step 6: Run the whole suite and ruff**

```bash
uv run pytest -q
uv run ruff check .
```

Expected: `164 passed` and `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add config/settings.py tests/test_settings.py
git commit -m "Turn the link preview on, so each post carries the page's own image"
```

---

## After the plan: one measurement to re-run by hand

The archetype accuracy of 5/6 is not an automated test — asserting a live model's output would
spend MiMo quota on every suite run and go red for reasons unrelated to the code.

Re-run it when the definitions change, and record the result in `docs/spike/`, following the
practice already used for clustering and language quality. Compare against the 2026-08-18
baseline: **0/6 without definitions and six irrelevant blocks; 5/6 with them and none.**

---

## Self-review

**Spec coverage**

| Spec section | Task |
|---|---|
| §2.1 LinkPreviewOptions, `prefer_small_media` | Task 5 |
| §2.2 archetype chosen by the model with definitions | Task 1 Steps 1, 6 |
| §2.3 `risk_hardening`, no severity enum | Task 1 Step 4 (`RiskHardeningDetails`), Task 4 |
| §2.4 two-stage split preserved | Task 1 emits `*_en` only; Task 2 keeps translation separate |
| §2.5 `evidence_level` untouched | no task changes it; `ResearchDetails.evidence_strength_en` is a new name |
| §3 no new stage, no new model field | Task 1 adds fields to an existing payload; Task 3 reads them |
| §4 six archetypes and their fields | Task 1 Steps 4–5, Task 4 |
| §4 definitions in CONTENT_SCHEMA.md | Task 1 Step 1 |
| §5 derived translation schema | Task 2 |
| §6.1 base plus six templates | Task 3 Step 3, Task 4 |
| §6.2 `lead` and `detail` blocks | Task 3 Step 3, Task 4 Step 3 |
| §6.3 character budget | Task 4 Step 1, `test_visible_part_stays_short` |
| §7 P3 boundary | Global Constraints — the term list is not touched |
| §8 failure handling | Task 3 Step 4, pinned by the two fallback tests |
| §9 two tests per archetype | Task 4 Step 1 |

No spec requirement is without a task.

**Placeholder scan:** none. Every code step carries its code, every command its expected output.

**Type consistency:** `ARCHETYPES` (Task 1) supplies the enum that `ARCHETYPE_TEMPLATES` and
`ARCHETYPE_REQUIRED` (Task 3) key on; the names match exactly across all three.
`archetype_fields` and `translation_schema_for` (Task 2) are used only inside `llm.py`.
The English side uses `*_en` names and the templates read `*_uz` names, converted by
`translation_schema_for`'s suffix rewrite — `what_changed_en` becomes `what_changed_uz`, which is
the key `item_release.html` reads.

**One inconsistency found and fixed while reviewing:** the fallback tests in Task 3 Step 1 pass
before the implementation exists, because `render_item_post` already renders the plain template.
Task 3 Step 2 now says so explicitly rather than claiming all three tests go red.
