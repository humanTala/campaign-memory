"""
campaign_memory_starter.py - the game master's two memories. THIS IS THE FILE YOU WRITE.

    copy campaign_memory_starter.py campaign_memory.py      (Windows)
    cp   campaign_memory_starter.py campaign_memory.py      (macOS / Linux)

Then fill the eight TODOs. Nothing else in the folder needs editing.

Two classes, two timescales:

    ShortTermMemory   what stays in the prompt DURING a game night. A buffer of
                      recent turns and a rolling summary of everything older,
                      folded when a TOKEN BUDGET is exceeded. Dies with the process.

    LongTermMemory    what survives BETWEEN game nights. Typed, dated facts on
                      disk (chromadb), extracted from each exchange by a model
                      call, superseded when the world changes, forgotten on request.

gm_base.py calls these, every turn:

    ltm.preferences()                 always loaded - how the player wants the game run
    ltm.recall(player_line, asof)     top-k facts relevant to THIS line, cited
    stm.context()                     summary + recent turns
    ltm.extract(player, gm)           after the reply: what in this exchange is worth keeping?
    ltm.remember(facts, asof, night)  write it, superseding what it replaces
    stm.add(...)  stm.should_fold()  stm.fold()

run_campaign.py calls, on a retcon:  ltm.forget(subject)  then  stm.fold(drop=subject)

Five numbers sit at the top. Change them if NOTES.md can say what changed on the
receipt when you did.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from common import chat, count_tokens, say, structured

# ----------------------------------------------------------------- policy ----
BUDGET_TOKENS = 1200         # short-term: fold when summary + buffer exceeds this
KEEP_LAST = 4                # ...keeping this many recent turns verbatim
MIN_SIM = 0.30               # long-term: below this a hit is noise
SAME_NAME = 0.85             # difflib ratio on normalised subjects that counts as "same thing"
EVENT_HALF_LIFE_DAYS = 14    # only `event` facts decay. NPCs, items, promises, preferences do not.

DB_DIR = Path(__file__).resolve().parent / "memory_store"    # anchored to THIS file
COLLECTION = "campaign"

FactType = Literal["npc", "item", "promise", "place", "preference", "event"]


class Fact(BaseModel):
    """One durable thing learned from one exchange."""
    type: FactType
    subject: str = Field(description="Short canonical name: 'Marra', 'the amulet', 'Old Tobb', 'the bridge'.")
    text: str = Field(description="The fact, one sentence, in the past or present tense as appropriate.")
    supersedes: Optional[str] = Field(default=None,
        description="If this fact CHANGES an earlier state of the same subject (alive -> dead, "
                    "promised -> fulfilled, owned -> given away), say what it replaces. Else null.")


class Facts(BaseModel):
    facts: list[Fact]


# ---------------------------------------------------------- given helpers ----

def normalise(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    for w in (" the ", " a ", " an ", " of ", " old "):
        s = (" " + s + " ").replace(w, " ")
    return " ".join(s.split())


def same_subject(a: str, b: str) -> bool:
    """'the amulet' ~ 'Tidewater amulet' ~ 'amulet'. 'Marra' !~ 'Mirra'."""
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= SAME_NAME


def mentions(text: str, subject: str) -> bool:
    """Does this line name this subject? Word-level, so 'Mirra' does not match 'Marra'."""
    words = set(normalise(text).split())
    return any(w in words for w in normalise(subject).split() if len(w) > 2)


def to_ts(d: dt.date) -> float:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()


def age_days(asof: dt.date, date_iso: str) -> int:
    return (asof - dt.date.fromisoformat(date_iso)).days


def mem_id(f: Fact) -> str:
    return hashlib.sha256(f"{f.type}|{normalise(f.subject)}|{f.text[:80].lower()}".encode()).hexdigest()[:16]


def open_store(path: Path | None = None):
    """The same three lines as demo 1. Given - you wrote this in the lab."""
    import chromadb
    client = chromadb.PersistentClient(path=str(path or DB_DIR))
    return client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})


# =========================================================================== #
# SHORT-TERM MEMORY - one game night
# =========================================================================== #

class ShortTermMemory:
    def __init__(self, budget: int = BUDGET_TOKENS, keep_last: int = KEEP_LAST):
        self.budget = budget
        self.keep_last = keep_last
        self.buffer: list[tuple[str, str]] = []     # ("user" | "assistant", text), verbatim
        self.summary: str = ""
        self.folds = 0

    def add(self, role: str, text: str) -> None:
        self.buffer.append((role, text))

    def tokens(self) -> int:
        """What this memory will cost in the next prompt (estimate, no API call)."""
        return count_tokens(self.summary) + sum(count_tokens(t) + 4 for _, t in self.buffer)

    # ================================================================= TODO 1
    def should_fold(self) -> bool:
        """The TRIGGER. Fold when tokens() is over the budget AND there is
        something older than the last keep_last turns to fold.

        A turn count (demo 2) gives a sawtooth that still climbs. A token budget
        gives a line that stays under a number you chose. This is the lab's
        Step 2 stretch, made mandatory: the receipt prints max prompt per night.
        """
        return self.tokens() > self.budget and len(self.buffer) > self.keep_last

    # ================================================================= TODO 2
    def fold(self, drop: str | None = None) -> None:
        """The SUMMARISE policy. Take everything older than the last keep_last
        turns and fold it into self.summary with ONE model call. Increment
        self.folds. Print a one-line receipt (how many turns folded, summary size).

        The prompt is yours. It must tell the model what MUST survive - the
        night-2 quiz will tell you what you forgot. Think: where the party is,
        what it is carrying, who is alive and who is not, open promises, names.
        Cap the summary (~120 words).

        `drop`: when the player retcons something, run_campaign.py calls
        fold(drop="amulet") AFTER ltm.forget(). Your prompt must then remove
        every mention of that subject from the summary - and you must fold even
        if the buffer is short, because the summary itself is what needs rewriting.

        Use chat([("user", prompt)]) from common.
        """
        old = self.buffer[:-self.keep_last] if self.keep_last else list(self.buffer)
        kept = self.buffer[-self.keep_last:] if self.keep_last else []
        if not old and not drop:
            return

        old_text = "\n".join(f"{role.upper()}: {text}" for role, text in old)
        drop_rule = ""
        if drop:
            drop_rule = f"\nRETCON: remove every mention of '{drop}' and any state that depends on it. Do not preserve it in the summary."
        prompt = f"""You are maintaining a compact game-state summary for a text adventure.
Previous summary:
{self.summary or '(none)'}

Older conversation to fold:
{old_text or '(none)'}

Write one factual summary of the campaign so far, about 120 words maximum. Preserve:
- the party's current location and important world-state changes
- current inventory and important possessions
- named NPCs and whether they are alive/dead when established
- open promises, quests, plans, and commitments
- important places, names, and durable facts
Do not preserve chatter, flavor, or repeated dialogue. Do not invent facts.{drop_rule}
"""
        new_summary, _ = chat([("user", prompt)], temperature=0.2)
        self.summary = (new_summary or "").strip()
        self.buffer = kept
        self.folds += 1
        print(f"[memory] fold {len(old)} turns -> {count_tokens(self.summary)} summary tokens (folds={self.folds})")

    # ================================================================= TODO 3
    def context(self) -> list[tuple[str, str]]:
        """The BOUNDED CONTEXT, as messages: a system message carrying the summary
        (if any), then the recent turns as real user/assistant messages.

            [("system", "SUMMARY OF TONIGHT SO FAR:\\n..."), ("user", ...), ("assistant", ...), ...]
        """
        messages: list[tuple[str, str]] = []
        if self.summary:
            messages.append(("system", "SUMMARY OF TONIGHT SO FAR:\n" + self.summary))
        messages.extend(self.buffer)
        return messages


# =========================================================================== #
# LONG-TERM MEMORY - between game nights
# =========================================================================== #

class LongTermMemory:
    def __init__(self, col=None):
        self.col = col if col is not None else open_store()

    # ================================================================= TODO 4
    def extract(self, player_text: str, gm_text: str) -> list[Fact]:
        """The WRITE POLICY, done by a model. One structured call:
        structured(Facts, prompt) -> Facts | None. Return .facts, or [] on failure.

        The prompt decides what a memory IS. It must produce:
          - typed facts (see FactType) with a short canonical `subject`
          - `supersedes` filled in when a fact changes an earlier state
            (an NPC dies, a promise is fulfilled, an item is given away)
          - [] for chatter, flavour, and the GM's descriptive prose
        Store what the player DID and what CHANGED. Do not store what the
        tavern smelled like.

        "Call me Dax" and "keep it light" are TWO preferences, not one. "Marra is the smith"
        is an npc fact. "Dax took the amulet" is an item fact. "Promised Tobb
        the ledger within a week" is a promise. "A traveller says the Toll-King
        collects the toll" is an event (a rumour, dated - it decays).
        """
        prompt = f"""Extract durable campaign memory from this single exchange.

PLAYER:
{player_text}

GM:
{gm_text}

Return a Facts object. Allowed types are npc, item, promise, place, preference, event.
Use a short canonical subject.

Extract ALL durable facts present in the exchange, especially:
- explicit player preferences or identity requests, such as "Call me Dax" or "keep it light"
  (each preference must be a separate preference fact)
- established NPC identities, such as "the smith is named Marra"
- player actions that change durable state
- possessions, places, promises, quests, and meaningful events

If an explicit preference or NPC identity appears, you MUST include it.

IMPORTANT: casual conversation is NOT a memory. Return an empty facts list for greetings,
farewells, sign-offs, thanks, encouragement, jokes, session-closing remarks, or other
chatter that does not change durable campaign state. For example, an exchange such as
"Good session, let's stop here for tonight." / "See you next week!" MUST produce no facts.

Do not turn the GM's descriptive prose or ordinary flavor into facts.
Do not invent anything.
Use supersedes when a fact changes an earlier state, such as alive -> dead,
promised -> fulfilled, or owned -> given away.

If there is no durable campaign fact, return an empty facts list"""


        
        try:
            result = structured(Facts, prompt)
            return result.facts if result is not None else []
        except Exception:
            return []

    # ================================================================= TODO 5
    def remember(self, facts: list[Fact], asof: dt.date, night: int) -> int:
        """The WRITE PATH, with SUPERSEDE. Return how many rows you wrote.

        For each fact:
          1. The model forgets to set fact.supersedes about one time in three. Decide
             whether to trust it alone or add a guard (a death is a death whatever
             the model flagged). If fact.supersedes is set: find ACTIVE rows with the same type and
             same_subject(...) and mark them status="superseded" via
             self.col.update(ids=..., metadatas=...). Do NOT delete them - night 3
             asks "when did Marra die", and the superseded row carries the date.
          2. Do not write a fact you already hold in other words: compare text with
             the active rows of the same type and subject (difflib on normalise()).
             Restating the inventory is not a new fact. This is compress, at write time.
          3. Upsert the new row: id=mem_id(fact), document=f"{subject}: {text}",
             metadata with at least: type, subject, subject_norm, text, status="active",
             night, date (ISO), ts (to_ts(asof)).
        Print one line per row:  [memory] + npc Marra: died in the forge fire (supersedes 1)
        """
        if not facts:
            return 0

        wrote = 0
        rows = self.col.get(include=["metadatas"])
        ids = rows.get("ids", [])
        metadatas = rows.get("metadatas", [])
        active = [(i, m) for i, m in zip(ids, metadatas) if m.get("status") == "active"]

        state_words = re.compile(r"\b(died|dead|killed|alive|destroyed|broken|lost|found|gave|given|received|stole|stolen|fulfilled|completed|failed|burned|escaped|returned|left|joined)\b", re.I)

        for fact in facts:
            same_rows = [(i, m) for i, m in active
                         if m.get("type") == fact.type and same_subject(str(m.get("subject", "")), fact.subject)]

            supersede = bool(fact.supersedes) or bool(state_words.search(fact.text))
            if supersede and same_rows:
                self.col.update(
                    ids=[i for i, _ in same_rows],
                    metadatas=[dict(m, status="superseded") for _, m in same_rows],
                )
                active = [(i, dict(m, status="superseded")) if i in {x for x, _ in same_rows} else (i, m)
                          for i, m in active]

            duplicate = False
            for _, m in same_rows:
                if m.get("status") != "active":
                    continue
                ratio = difflib.SequenceMatcher(
                    None, normalise(str(m.get("text", ""))), normalise(fact.text)
                ).ratio()
                if ratio >= 0.90:
                    duplicate = True
                    break
            if duplicate:
                continue

            metadata = {
                "type": fact.type,
                "subject": fact.subject,
                "subject_norm": normalise(fact.subject),
                "text": fact.text,
                "status": "active",
                "night": int(night),
                "date": asof.isoformat(),
                "ts": float(to_ts(asof)),
            }
            self.col.upsert(
                ids=[mem_id(fact)],
                documents=[f"{fact.subject}: {fact.text}"],
                metadatas=[metadata],
            )
            active.append((mem_id(fact), metadata))
            wrote += 1
            extra = f" (supersedes {len(same_rows)})" if supersede and same_rows else ""
            print(f"[memory] + {fact.type} {fact.subject}: {fact.text}{extra}")

        return wrote

    def preferences(self) -> list[dict]:
        """ALWAYS LOADED - given. Preferences are few, small and apply to every
        turn; similarity is the wrong tool for them (demo 1's read-path rule)."""
        if self.col.count() == 0:
            return []
        rows = self.col.get(where={"$and": [{"type": "preference"}, {"status": "active"}]},
                            include=["metadatas"])
        return [dict(m, sim=None) for m in rows["metadatas"]]

    def subjects(self, mem_type: str) -> list[str]:
        """Every active subject of one type - given. Used by the identity check."""
        if self.col.count() == 0:
            return []
        rows = self.col.get(where={"$and": [{"type": mem_type}, {"status": "active"}]}, include=["metadatas"])
        return sorted({m["subject"] for m in rows["metadatas"]})

    # ================================================================= TODO 6
    def decay(self, sim: float, mem_type: str, age: int) -> float:
        """Per-type decay (demo 4, table 2). Only `event` facts go stale:
        sim * 0.5 ** (age / EVENT_HALF_LIFE_DAYS). Everything else returns sim."""
        if mem_type == "event":
            return sim * (0.5 ** (age / EVENT_HALF_LIFE_DAYS))
        return sim

    def recall(self, query: str, asof: dt.date, k: int = 4) -> list[dict]:
        """The READ PATH. Return up to k hits, best first, each a dict with the
        row's metadata plus "sim", "score", "age".

          1. Query self.col with the player's line; filter status="active" IN THE
             QUERY (where=...), not after. Ask for more than k (say 3*k) so the
             floor and the identity check have something to cut.
          2. sim = 1 - distance; drop below MIN_SIM. score = self.decay(sim, type, age).
          3. IDENTITY CHECK for npc hits: if the player's line names any known npc
             (see self.subjects("npc") and mentions()), then npc hits whose subject
             is NOT mentioned are dropped. "Is Mirra alive?" must never return
             Marra's death. If the line names nobody ("who is the smith?"), keep
             by score. Think before applying the same rule to items: "what did I
             take besides the ledger?" names one item and asks about another.
          4. Sort by score. Then think about DIVERSITY: five rows about Mirra will
             fill every slot and push out the one row about what you bought from
             her. Cap hits per subject (two is plenty) - the exercise's compress
             lever, at read time. Return the top k. Print one line per hit:
             [memory] < npc Marra (night 2, 30d, sim 0.61, score 0.61)
        """
        if k <= 0 or self.col.count() == 0:
            return []

        n = max(k * 3, k)
        rows = self.col.query(
            query_texts=[query],
            n_results=n,
            where={"status": "active"},
            include=["metadatas", "distances", "documents"],
        )
        metadatas = (rows.get("metadatas") or [[]])[0]
        distances = (rows.get("distances") or [[]])[0]

        known_npcs = self.subjects("npc")
        named_npcs = [s for s in known_npcs if mentions(query, s)]
        candidates: list[dict] = []
        for m, distance in zip(metadatas, distances):
            if not m:
                continue
            sim = 1.0 - float(distance)
            if sim < MIN_SIM:
                continue
            if m.get("type") == "npc" and named_npcs:
                if not any(same_subject(str(m.get("subject", "")), named) for named in named_npcs):
                    continue
            age = max(0, age_days(asof, str(m["date"])))
            score = self.decay(sim, str(m["type"]), age)
            if score < MIN_SIM:
                continue
            hit = dict(m)
            hit.update({"sim": sim, "score": score, "age": age})
            candidates.append(hit)

        candidates.sort(key=lambda h: h["score"], reverse=True)
        result: list[dict] = []
        per_subject: dict[str, int] = {}
        for hit in candidates:
            subject = str(hit.get("subject", ""))
            if per_subject.get(subject, 0) >= 2:
                continue
            per_subject[subject] = per_subject.get(subject, 0) + 1
            result.append(hit)
            print(f"[memory] < {hit['type']} {subject} (night {hit['night']}, {hit['age']}d, sim {hit['sim']:.2f}, score {hit['score']:.2f})")
            if len(result) >= k:
                break
        return result

    # ================================================================= TODO 7
    def forget(self, subject: str) -> tuple[int, int]:
        """EVICT BY REQUEST - the retcon. Delete EVERY row about this subject,
        whatever its status: same_subject() on the stored subject, or the subject
        named in the stored text. Then query again and return (deleted, remaining).
        remaining must be 0. Print it:

            [memory] x forgot 'amulet': 3 rows deleted, 0 remaining

        Deleting the rows is half the job. The other half is the summary - see
        fold(drop=...). run_campaign.py checks both.
        """
        rows = self.col.get(include=["metadatas"])
        ids = rows.get("ids", [])
        metadatas = rows.get("metadatas", [])
        to_delete = []
        for row_id, m in zip(ids, metadatas):
            stored_subject = str(m.get("subject", ""))
            stored_text = str(m.get("text", ""))
            if same_subject(subject, stored_subject) or mentions(stored_text, subject):
                to_delete.append(row_id)

        if to_delete:
            self.col.delete(ids=to_delete)

        remaining_rows = self.col.get(include=["metadatas"])
        remaining = 0
        for m in remaining_rows.get("metadatas", []):
            if same_subject(subject, str(m.get("subject", ""))) or mentions(str(m.get("text", "")), subject):
                remaining += 1
        print(f"[memory] x forgot '{subject}': {len(to_delete)} rows deleted, {remaining} remaining")
        return len(to_delete), remaining


# ===================================================================== TODO 8
def cite(hit: dict) -> str:
    """The parenthetical the GM appends when it uses a remembered fact. The player
    must be able to see which night it came from and how old it is:
        (remembered from night 1, 2026-08-14, 37 days ago)
    """
    return f"(remembered from night {hit['night']}, {hit['date']}, {hit['age']} days ago)"
