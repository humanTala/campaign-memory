# Module 6 Bridge Project — The Campaign

**Tala Daana**

## a. My fold

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

During verification, one important piece of continuity that the summary had to preserve was the companion dog's name, **Biscuit**. I therefore made companion names explicit in the fold policy, including the instruction:

> A companion's name is important continuity and must not be dropped.

The final verification confirmed that the memory system preserved important campaign continuity.

In my final complete Memory GM run, the Night 2 continuity quiz scored **9/10** and Night 3 scored **10/10**.

The final Memory GM prompt measurements were:

| Night   | Folds | Max prompt |
| ------- | ----: | ---------: |
| Night 1 |     2 |      2,125 |
| Night 2 |     1 |      2,267 |
| Night 3 |     2 |      2,373 |

The Goldfish baseline, which does not use persistent memory or folding, reached:

| Night   | Max prompt |
| ------- | ---------: |
| Night 2 |      3,078 |
| Night 3 |      2,611 |

This shows that the Memory GM kept its active context bounded through folding instead of relying on an ever-growing conversation history.

---

## b. Marra and Mirra

The Night 3 continuity quiz correctly kept the two similarly named NPCs apart.

For **Marra**, the GM correctly remembered that she was dead.

For **Mirra**, the GM correctly remembered that she was alive and continued to run the herbalist's shop, the Green Door.

The identity protection in `recall()` is:

```python
if mem_type == "npc" and named_npcs:
    if not any(same_subject(subject, named) for named in named_npcs):
        continue
```

This guard is important because semantic similarity alone can confuse NPCs with similar names.

For example, a query about whether **Mirra** is alive could potentially retrieve memories about **Marra** because their names and descriptions are semantically similar. The explicit identity check prevents an NPC memory about the wrong character from entering the final context.

The verification script confirmed that the memory system passed all **17/17 checks**, including the long-term identity check.

The guard therefore works together with semantic retrieval:

* Semantic retrieval finds potentially relevant memories.
* The identity check filters memories belonging to a different named NPC.
* The final context contains the appropriate character information.

---

## c. The retcon

On Night 3, the campaign retconned the Tidewater amulet so that Dax had never taken it.

The retcon successfully removed the old amulet memories and prevented them from returning through short-term memory.

The complete retcon result was:

```text
4 rows deleted, 0 remaining
summary mentions the amulet after re-fold: no
GM's next answer mentions the amulet: no
```

Deleting the amulet rows from Chroma alone would not necessarily be enough. The amulet could already have been copied into the rolling short-term summary. Because the summary is later sent back to the model, the forgotten fact could otherwise leak back into the conversation.

That is why the retcon uses both:

```python
ltm.forget(subject)
```

and:

```python
stm.fold(drop=subject)
```

The three checks verify the retcon at different levels:

1. **Persistent memory** — confirms the old amulet memories were deleted.
2. **Short-term memory** — confirms the forgotten fact was removed from the rolling summary.
3. **Next GM response** — confirms the deleted information was not reintroduced into the conversation.

The successful result was:

```text
Long-term memory  -> 0 remaining
Short-term memory -> no mention
Next model answer -> no mention
```

---

## Final results

The final continuity quiz results were:

| Night   | Memory GM | Goldfish GM |
| ------- | --------: | ----------: |
| Night 2 |  **9/10** |    **3/10** |
| Night 3 | **10/10** |    **4/10** |

The verification script also reported:

```text
verify_memory.py: 17/17 checks passed
```

The project demonstrates that memory and context are separate concerns.

Long-term typed memories persisted important campaign information across separate processes. Short-term folding controlled how much recent conversation was placed back into the model context. Superseding allowed state changes to replace active facts while retaining history, event decay reduced the score of older events, the NPC identity guard protected similarly named entities, and explicit forgetting removed a retconned fact from both persistent and short-term memory.

The Night 3 retcon also demonstrated that forgetting must happen at both the long-term and short-term memory levels. The successful test showed that the deleted amulet information was not present in the database, was not present in the folded summary, and did not appear in the GM's next answer.

Fact extraction required model calls to turn free-form conversation into structured, typed long-term memories. Retrieval then allowed relevant stored facts to be placed back into later prompts instead of sending the entire campaign history every time.

Overall, the results show that the Memory GM maintained substantially better continuity than the Goldfish GM while keeping the prompt size more controlled.
