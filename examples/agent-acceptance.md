# Everyday agent acceptance

Use this small check when adopting an agent or changing its wiki connection.
It checks consultation and retrieval on existing knowledge. The separate
[knowledge workflow](acceptance.md) qualifies ingestion, evidence preservation
and recovery. Neither document is an automated benchmark.

Choose 10–15 everyday questions for one actual consumer. Freeze expected evidence
paths, revisions and important answer facts before running them. Keep the
questions, answers and results private when they concern real people or records.
Use existing grants; this check does not require creating a wiki or running an
ingest. Do not change permissions on a real collection for the test.

Include these kinds of questions, adapting them to the installation:

- A communication preference expressed as a natural question.
- A paraphrase that uses different words from the relevant page.
- A question whose evidence is in a different wiki from the previous question.
- A recommendation that combines a preference with a dated factual record.
- A precise value with its unit or date.
- A correction with a future effective date, and the earlier account.
- Conflicting accounts that the sources have not resolved.
- A fact the selected sources do not provide.
- A question for which browsing the wiki map is more useful than guessing terms.

For initial consultation, use the consumer's normal configuration and guidance.
Ask the question without naming a tool or revealing the expected answer. Record
whether the agent consults the wiki before answering or asking the user for an
already recorded fact. Tool availability alone does not pass this check.

Inspect the evidence separately. `wiki_list` should identify accessible scopes;
`wiki_search` returns evidence locators and accepted revisions. Follow a hit with
`wiki_read` at that revision and enough surrounding lines to retain qualifications.
Use `wiki_pages` or `wiki/index.md` when search terms are unclear. A query rewrite
or browse can recover a miss, but preserve the first attempt and its outcome.
For a long page, follow `next_start_line` or choose a heading range before
concluding that the opening response contains all its evidence.

For each question, retain a short record:

| Field | Record |
| --- | --- |
| Identity | Release, consumer/model, instructions and actual credential type |
| Expected evidence | Wiki, path, revision and facts fixed before the attempt |
| Consultation | Whether the agent used the wiki, and the tool sequence |
| Retrieval | Query, scope, first five hits, any retry/browse and elapsed time |
| Answer | Answer with citations, supported facts, omissions and unresolved conflicts |
| Outcome | Pass, failure or unverified, with the specific reason |

Judge retrieval and answers separately. A different path may contain equally
good evidence; inspect it rather than marking it correct from a filename. A
zero-result query establishes a search miss, not proof that a fact is absent
everywhere. A snippet matching a title, metadata or navigation may only locate
the evidence; read the page's actual statements before answering. Empty template
sections do not establish a preference. Preserve dates and source instructions
as attributed evidence.

Record raw counts and latency range/median for this small sample. Distinguish
initial consultation, manually guided retrieval and scripted transport checks;
one does not establish the others. Do not silently retry, adjust expected facts
after seeing answers, or claim universal accuracy from a small pilot. Keep
earlier ingestion and restore receipts with their own versions and scope.

Before adopting the consumer, resolve critical factual or permission failures.
For other misses, first improve the query, navigation or source page implicated
by the evidence. Change retrieval code only for a reproducible remaining gap.
Existing tools and a small private results file are sufficient; add no service,
paid CI model calls or evaluation framework for this check.
