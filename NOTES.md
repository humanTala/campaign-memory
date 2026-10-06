# Module 6 Bridge Project — The Campaign

## a. Short-Term Memory and Folding

I used a short-term memory budget of **1,200 tokens** and kept the last **4 turns** verbatim.

When the short-term context exceeded the budget, `fold()` compressed the older turns into a rolling summary while leaving the most recent turns unchanged.

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

During verification, one important continuity detail was the companion dog's
name, **Biscuit**. I therefore made companion names explicit in the folding
policy and specifically instructed the summary not to drop them.

The final verification confirmed that the folded summary preserved this
information.

In my first complete campaign run, the Night 2 continuity quiz scored **8/10**.
The two missed questions involved the tone preference and the inventory before
bed. Night 3 then scored **10/10**.

The final Memory GM prompt measurements were:

| Night   | Folds | Max Prompt |
| ------- | ----: | ---------: |
| Night 1 |     2 |      2,140 |
| Night 2 |     2 |      2,153 |
| Night 3 |     3 |      2,398 |

I did not keep a separate pre-fold Memory-GM receipt, so I cannot give a
measured before/after comparison for the same implementation without inventing
one.

The available no-memory/no-fold Goldfish baseline reached **3,132 tokens** on
Night 2 and **2,632 tokens** on Night 3. The Memory GM reached **2,153** and
**2,398** respectively.

This shows that the Memory GM continued folding its short-term context instead
of keeping the entire conversation history verbatim.

---

## b. NPC Identity Protection

One important retrieval problem was keeping similarly named NPCs separate.

The Night 3 continuity quiz correctly distinguished the two NPCs.

For **Marra**, the GM answered:

> No, Marra is dead. The fire that claimed her life has left a shadow over
> the village, and her absence is felt deeply by the community.

For **Mirra**, the GM answered:

> Yes, Mirra is alive and continues to run the herbalist's shop, the Green
> Door, in the village.

The identity protection in `recall()` is:

```python
if mem_type == "npc" and named_npcs:
    if not any(same_subject(subject, named) for named in named_npcs):
        continue
```

I tested why this guard was necessary by temporarily removing these lines and
rerunning:

```text
python verify_memory.py
```

Without the identity guard, the query:

```text
is Mirra alive
```

retrieved both NPCs:

```text
Mirra — similarity 0.67
Marra — similarity 0.34
```

The verification then failed:

```text
FAIL  long-term: recall('is Mirra alive') never returns Marra (identity check)
```

After restoring the identity guard, the same verification returned only the
appropriate Mirra memory, and the full verification result returned to:

```text
all 17 checks passed - both memories are sound.
```

This demonstrated that semantic similarity alone is not sufficient for NPC
identity. Semantic retrieval finds potentially relevant memories, while the
explicit identity check prevents a memory about a different named NPC from
being included.

---

## c. The Retcon

On Night 3, the campaign retconned the Tidewater amulet so that Dax had never
taken it.

The three important checks were:

```text
rows remaining: 0
summary mentions amulet after re-fold: no
GM's next answer mentions amulet: no
```

The complete retcon receipt reported:

```text
4 rows deleted, 0 remaining;
summary mentions it after re-fold: no;
GM's next answer mentions it: no
```

The short-term summary was the most subtle part of this process.

Deleting the amulet rows from Chroma removes the persistent long-term memory,
but this alone is not enough. The amulet may already have been copied into the
rolling short-term summary. Since that summary is later sent back to the
model, the forgotten information could otherwise leak back into the
conversation.

For this reason, the retcon uses both:

```python
ltm.forget(subject)
```

and:

```python
stm.fold(drop=subject)
```

The checks are performed at three different levels.

First, the persistent store is checked to make sure the original memory source
has been removed.

Second, the short-term summary is checked because it may contain a cached copy
of the forgotten information.

Third, the GM's next answer is checked. This provides an end-to-end test that
the deleted information is not being reintroduced into the conversation.

The successful result was:

```text
Long-term memory  -> 0 remaining
Short-term memory -> no mention
Next model answer -> no mention
```

This confirms that the retcon affected both memory layers rather than only the
persistent database.

---

## d. Final Results

The continuity quiz results were:

| Night   | Memory GM | Goldfish GM |
| ------- | --------: | ----------: |
| Night 2 |      8/10 |        3/10 |
| Night 3 |     10/10 |        4/10 |

The results demonstrate the difference between having persistent memory and
relying on the full conversation history.

Long-term typed memories preserved important campaign information across
separate processes. Short-term folding controlled how much recent conversation
was placed back into the model context.

The implementation also supported several additional memory behaviours:

* **Superseding** allowed state changes to replace active facts while
  preserving the older facts as history.
* **Event decay** reduced the score of older event memories.
* **NPC identity protection** prevented similarly named characters from being
  confused during retrieval.
* **Explicit forgetting** removed a retconned fact from persistent memory and
  the short-term summary.
* **Structured extraction** converted free-form conversation into typed,
  durable facts.

Fact extraction required one model call per turn. This added a model-call cost,
but it allowed free-form conversation to be converted into structured
long-term memories.

Later retrieval then selected only relevant stored facts instead of sending
the entire campaign history back to the model.

Overall, the project demonstrates how short-term and long-term memory solve
different problems: short-term memory controls context size, while long-term
memory provides persistent campaign continuity across game sessions.
