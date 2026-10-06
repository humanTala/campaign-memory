# Module 6 Bridge Project — The Campaign

## a. My fold

I used a short-term memory budget of **1,200 tokens** and kept the last
**4 turns** verbatim.

When the short-term context exceeded the budget, `fold()` compressed the older
turns into a rolling summary while leaving the most recent turns unchanged.

My fold prompt was:

```text
You maintain a rolling short-term memory summary for a role-playing game.

Merge the previous summary with the older conversation turns below.

Preserve:
- player and character names
- companion names, including pets such as dogs
- NPC names and identities
- where the party is
- inventory and possessions
- who is alive or dead
- open or fulfilled promises
- important actions and decisions
- unresolved events

A companion's name is important continuity and must not be dropped.

Prefer concrete facts over descriptive flavour.
Do not invent anything.
Keep the result to about 120 words maximum.

PREVIOUS SUMMARY:
{self.summary or '(none)'}

OLDER TURNS:
{transcript or '(none)'}

Return only the updated summary.
```

I explicitly included companion names in the fold policy because they are
important continuity information. The fold also removes a forgotten subject
from the rolling summary when a retcon occurs.

The official memory verification passed all **17 checks**:

```text
all 17 checks passed - both memories are sound. Run night 1.
```

The final campaign receipt reported:

| Night   | Folds | Max prompt |
| ------- | ----: | ---------: |
| Night 1 |     2 |      2,407 |
| Night 2 |     2 |      2,551 |
| Night 3 |     2 |      2,629 |

The Memory GM therefore continued folding its short-term context rather than
keeping the entire conversation history verbatim.

---

## b. Structured long-term memory

The long-term memory extracts durable campaign facts from the conversation
using the structured `Facts` model.

The extraction prompt tells the model to preserve durable information such
as:

* NPC names and identities
* important locations
* inventory and possessions
* promises, quests, and plans
* player preferences
* character identity requests
* important campaign events
* state changes

It also explicitly rejects greetings, farewells, sign-offs, and general
conversation that should not become durable memories.

The extraction step uses one structured model call:

```python
result = structured(Facts, prompt)
```

If extraction fails, it returns an empty list rather than crashing the
campaign.

The verifier confirmed that extraction returned both a preference and an NPC
fact from a real exchange, while returning no facts for chatter:

```text
ok long-term: extract() returns a preference and an npc fact from a real
exchange [llm]

ok long-term: extract() returns [] for chatter [llm]
```

The test also confirmed that preferences such as:

```text
Call my character Dax.
Keep it light.
```

were stored as separate durable facts.

---

## c. Superseding and retrieval

When a new fact changes the state of an existing fact, `remember()` can mark
the older active fact as superseded rather than deleting it.

The important distinction is that superseded memories remain in the database
as history. They are no longer active memories for normal retrieval.

Each active memory stores metadata including:

```python
{
    "type": fact.type,
    "subject": fact.subject,
    "subject_norm": normalise(fact.subject),
    "text": fact.text,
    "status": "active",
    "night": int(night),
    "date": asof.isoformat(),
    "ts": float(to_ts(asof)),
}
```

Reworded duplicate facts are also detected so that the database does not
continue accumulating identical active memories.

The verifier confirmed that `remember()` writes active rows with the required
type, date, timestamp, and night information.

---

## d. NPC identity protection

The retrieval logic includes an explicit identity check for NPC memories.

The purpose is to prevent semantic similarity from returning a different NPC
with a similar name when the query clearly refers to a known character.

The relevant logic is:

```python
if mem_type == "npc" and named_npcs:
    if not any(same_subject(subject, named) for named in named_npcs):
        continue
```

This matters for similarly named characters such as **Marra** and **Mirra**.
A similarity search can find both names because they are semantically close,
but the identity check makes sure that a memory about one NPC is not treated
as a memory about the other.

The verifier included this identity requirement and all 17 verification checks
passed after the final implementation.

---

## e. Event decay and recall

Long-term recall converts Chroma distances into similarity scores and removes
memories below the minimum similarity threshold.

The configured minimum similarity is:

```python
MIN_SIM = 0.30
```

Events also receive time-based decay. Only facts of type `event` use the event
half-life:

```python
EVENT_HALF_LIFE_DAYS = 14
```

The event score is calculated using:

```python
sim * 0.5 ** (age / EVENT_HALF_LIFE_DAYS)
```

This means that older events gradually become less prominent while stable
facts such as NPC identities or preferences do not receive the same event
decay.

Recall also limits the number of returned memories per subject and sorts the
remaining memories by score before returning the top results.

---

## f. The retcon

On Night 3, the campaign retconned the Tidewater amulet so that Dax had never
taken it.

The campaign performed:

```text
-- retcon: forget 'amulet' --
[memory] x forgot 'amulet': 5 rows deleted, 0 remaining
```

The rolling short-term memory was then folded again with the forgotten subject
removed.

The three final checks were:

```text
[short-term] summary mentions 'amulet': no

[check] GM's answer mentions 'amulet': no
```

The final receipt reported:

```text
retcon 'amulet' (night 3): 5 rows deleted, 0 remaining;
summary mentions it after re-fold: no;
GM's next answer mentions it: no
```

The short-term check is important because deleting the long-term database rows
alone would not necessarily remove information that had already been copied
into the rolling summary.

The retcon therefore checks both memory layers:

```text
Long-term memory  -> 0 remaining
Short-term memory -> no mention
Next model answer -> no mention
```

The successful result demonstrates that the forgotten information was removed
from persistent memory and did not remain in the short-term context used for
the next GM response.

---

## g. Night 3 results

The Night 3 continuity quiz scored:

```text
7/10
```

The three missed questions involved:

1. What Dax kept from the forge after the fire.
2. What Dax bought from Mirra after the fire.
3. How much Dax paid Sela to cross on the ferry.

These quiz results are separate from the official implementation
verification. The authoritative `verify_memory.py` test suite passed all
**17/17 checks**.

Night 3 also successfully completed the required amulet retcon check:

```text
5 rows deleted, 0 remaining
summary mentions it after re-fold: no
GM's next answer mentions it: no
```

---

## Final results

The final campaign receipt reported:

| Night   | Folds | Max prompt | Quiz | Facts stored | Model calls |
| ------- | ----: | ---------: | ---: | -----------: | ----------: |
| Night 1 |     2 |      2,407 |    — |           64 |          38 |
| Night 2 |     2 |      2,551 | 9/10 |           50 |          44 |
| Night 3 |     2 |      2,629 | 7/10 |           51 |          42 |

At the end of Night 3, the receipt reported:

```text
158 facts on disk at end.
Now STOP this process. Next night is a new one.
```

The project demonstrates that short-term and long-term memory solve different
problems.

Short-term folding controls how much recent conversation is placed back into
the model context. Long-term memory stores typed, durable campaign facts that
can be retrieved later. Superseding allows state changes to replace active
facts while preserving historical rows, event decay reduces the influence of
older events, identity checks protect similarly named NPCs, and explicit
forgetting removes retconned information from both persistent and short-term
memory.

The final implementation passed the complete memory verifier:

```text
all 17 checks passed - both memories are sound.
```
