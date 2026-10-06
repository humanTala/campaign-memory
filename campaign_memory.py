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
BUDGET_TOKENS = 1200
KEEP_LAST = 4
MIN_SIM = 0.30
SAME_NAME = 0.85
EVENT_HALF_LIFE_DAYS = 14

DB_DIR = Path(__file__).resolve().parent / "memory_store"
COLLECTION = "campaign"

FactType = Literal["npc", "item", "promise", "place", "preference", "event"]


class Fact(BaseModel):
    """One durable thing learned from one exchange."""
    type: FactType
    subject: str = Field(description="Short canonical name: 'Marra', 'the amulet', 'Old Tobb', 'the bridge'.")
    text: str = Field(description="The fact, one sentence, in the past or present tense as appropriate.")
    supersedes: Optional[str] = Field(
        default=None,
        description=(
            "If this fact CHANGES an earlier state of the same subject (alive -> dead, "
            "promised -> fulfilled, owned -> given away), say what it replaces. Else null."
        ),
    )


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
    return hashlib.sha256(
        f"{f.type}|{normalise(f.subject)}|{f.text[:80].lower()}".encode()
    ).hexdigest()[:16]


def open_store(path: Path | None = None):
    """Persistent Chroma store."""
    import chromadb

    client = chromadb.PersistentClient(path=str(path or DB_DIR))
    return client.get_or_create_collection(
        COLLECTION, metadata={"hnsw:space": "cosine"}
    )


# =========================================================================== #
# SHORT-TERM MEMORY - one game night
# =========================================================================== #

class ShortTermMemory:
    def __init__(self, budget: int = BUDGET_TOKENS, keep_last: int = KEEP_LAST):
        self.budget = budget
        self.keep_last = keep_last
        self.buffer: list[tuple[str, str]] = []
        self.summary: str = ""
        self.folds = 0

    def add(self, role: str, text: str) -> None:
        self.buffer.append((role, text))

    def tokens(self) -> int:
        """What this memory will cost in the next prompt."""
        return count_tokens(self.summary) + sum(
            count_tokens(t) + 4 for _, t in self.buffer
        )

    def should_fold(self) -> bool:
        """Fold only when over budget and there is old material to compress."""
        return self.tokens() > self.budget and len(self.buffer) > self.keep_last

    def fold(self, drop: str | None = None) -> None:
        """Compress old turns into a rolling summary, or rewrite it for a retcon."""
        if drop is None and len(self.buffer) <= self.keep_last:
            return

        if self.keep_last > 0:
            old_turns = self.buffer[:-self.keep_last]
            recent_turns = self.buffer[-self.keep_last:]
        else:
            old_turns = self.buffer[:]
            recent_turns = []

        transcript = "\n".join(
            f"{role.upper()}: {text}" for role, text in old_turns
        )

        if drop:
            prompt = f"""
You maintain the short-term memory summary for a role-playing game.

Rewrite the summary so that EVERY mention, implication, possession, action,
or fact about the subject {drop!r} is removed.
Treat that subject as though that business never happened.

Preserve all unrelated continuity:
- player and character names
- companion names, including pets such as dogs
- NPC names and identities
- where the party is
- inventory and possessions
- who is alive or dead
- open or fulfilled promises
- important actions and decisions
- unresolved events

Do not invent anything.
Be factual and compact.
Maximum about 120 words.

CURRENT SUMMARY:
{self.summary or '(none)'}

OLDER TURNS:
{transcript or '(none)'}

Return only the rewritten summary.
""".strip()

        else:
            prompt = f"""
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
""".strip()

        new_summary, _ = chat([("user", prompt)])

        self.summary = (new_summary or self.summary).strip()
        self.buffer = recent_turns
        self.folds += 1

        say(
            f"[memory] fold {len(old_turns)} turns -> "
            f"{count_tokens(self.summary)} summary tokens"
        )

    def context(self) -> list[tuple[str, str]]:
        messages = []

        if self.summary:
            messages.append(
                ("system", f"SUMMARY OF TONIGHT SO FAR:\n{self.summary}")
            )

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
        """Use one structured model call to extract durable campaign facts."""
        prompt = f"""
You are the write-policy for a role-playing campaign memory system.
Extract ONLY durable facts established or changed by this exchange.

Allowed fact types:
- npc: identity, role, relationship, alive/dead state, or durable NPC change
- item: ownership, possession, purchase, loss, transfer, or durable item state
- promise: a promise/obligation and whether it is open or fulfilled
- place: a durable place/world-state change
- preference: how the player wants the game run, including player/character naming
- event: a dated occurrence, rumour, warning, or event that may become stale

Rules:
1. Use a short canonical subject such as "Marra", "Mirra", "Old Tobb",
   "the amulet", "the bridge", "Dax", or "tone".
2. Store what the PLAYER did, explicitly established, or what actually CHANGED.
3. Do NOT store ordinary chatter, atmospheric flavour, or the GM's decorative prose.
4. If a new fact changes an earlier state of the same subject, fill `supersedes`
   with a short description of the old state. Examples: alive -> dead,
   bridge standing -> destroyed, promise open -> fulfilled, item owned -> given away.
5. If nothing durable was established, return an empty facts list.
6. Separate independent memories. For example, "Call me Dax and keep it light"
   should become TWO preference facts.
7. A rumour or warning is an event, not an unquestioned world fact.
8. Do not invent facts that are not supported by the exchange.

Examples:
- "Marra is the smith" -> npc / subject "Marra"
- "Dax took the amulet" -> item / subject "the amulet"
- "Dax promised Old Tobb to return the ledger within a week" -> promise / subject "Old Tobb"
- "A traveller warned that the Toll-King collects the toll" -> event / subject "Toll-King"

PLAYER:
{player_text}

GAME MASTER:
{gm_text}
""".strip()

        try:
            result = structured(Facts, prompt)
            return result.facts if result is not None else []
        except Exception as exc:
            say(f"[memory] extraction failed: {exc}")
            return []

    # ================================================================= TODO 5
    def remember(self, facts: list[Fact], asof: dt.date, night: int) -> int:
        """Write durable facts, deduplicate them, and supersede changed state."""
        written = 0

        # Obvious state-change language is a guard for cases where the model
        # forgot to fill `supersedes`.
        change_words = re.compile(
            r"\b(die[ds]?|dead|killed?|passed away|burn(?:ed|t)?|destroyed?|"
            r"collapsed?|gone|returned?|gave|given|fulfilled?|completed?|"
            r"broke|broken|lost|left|moved|closed?|opened?|repaired?|fixed)\b",
            re.IGNORECASE,
        )

        for fact in facts:
            # Read active rows of the same type first; we use them for both
            # duplicate detection and superseding.
            active_rows: list[tuple[str, dict]] = []
            if self.col.count() > 0:
                rows = self.col.get(
                    where={
                        "$and": [
                            {"type": fact.type},
                            {"status": "active"},
                        ]
                    },
                    include=["metadatas"],
                )
                active_rows = list(zip(rows.get("ids", []), rows.get("metadatas", [])))

            same_rows = [
                (row_id, meta)
                for row_id, meta in active_rows
                if same_subject(fact.subject, str(meta.get("subject", "")))
            ]

            # Compress at write time: do not store the same fact again merely
            # because it was rephrased.
            new_text = normalise(fact.text)
            duplicate = False
            for _, meta in same_rows:
                old_text = normalise(str(meta.get("text", "")))
                if not old_text or not new_text:
                    continue
                ratio = difflib.SequenceMatcher(None, new_text, old_text).ratio()
                if new_text == old_text or new_text in old_text or old_text in new_text or ratio >= 0.88:
                    duplicate = True
                    break

            if duplicate:
                continue

            # Supersede if the model explicitly says so, OR if the new fact has
            # unmistakable state-changing language and an active fact already
            # exists for the same subject/type.
            should_supersede = bool(fact.supersedes)
            if not should_supersede and same_rows and change_words.search(fact.text):
                should_supersede = True

            superseded = 0
            if should_supersede:
                for row_id, meta in same_rows:
                    updated = dict(meta)
                    updated["status"] = "superseded"
                    self.col.update(ids=[row_id], metadatas=[updated])
                    superseded += 1

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
            if fact.supersedes:
                metadata["supersedes"] = fact.supersedes

            self.col.upsert(
                ids=[mem_id(fact)],
                documents=[f"{fact.subject}: {fact.text}"],
                metadatas=[metadata],
            )
            written += 1

            suffix = f" (supersedes {superseded})" if superseded else ""
            say(f"[memory] + {fact.type} {fact.subject}: {fact.text}{suffix}")

        return written

    def preferences(self) -> list[dict]:
        """Always load active preferences."""
        if self.col.count() == 0:
            return []
        rows = self.col.get(
            where={"$and": [{"type": "preference"}, {"status": "active"}]},
            include=["metadatas"],
        )
        return [dict(m, sim=None) for m in rows["metadatas"]]

    def subjects(self, mem_type: str) -> list[str]:
        """Every active subject of one type."""
        if self.col.count() == 0:
            return []
        rows = self.col.get(
            where={"$and": [{"type": mem_type}, {"status": "active"}]},
            include=["metadatas"],
        )
        return sorted({m["subject"] for m in rows["metadatas"]})

    # ================================================================= TODO 6
    def decay(self, sim: float, mem_type: str, age: int) -> float:
        """Only episodic/event memories decay with age."""
        if mem_type == "event":
            safe_age = max(0, age)
            return sim * (0.5 ** (safe_age / EVENT_HALF_LIFE_DAYS))
        return sim

    def recall(self, query: str, asof: str, k: int = 6) -> list[dict]:
        """
        Recall relevant active memories.

        Special handling:
        - inventory questions: search all active memories so item memories
          cannot be lost because of semantic ranking
        - purchase questions: search all active memories so purchases from
          specific NPCs are reliably retrieved
        - repair questions: search all active memories so promises/events
          such as "fix the cart wheel" are reliably retrieved
        - ordinary questions: use Chroma semantic retrieval
        """

        q = query.lower()

        # ------------------------------------------------------------
        # Query intent
        # ------------------------------------------------------------
        inventory_terms = {
            "carry", "carrying", "inventory", "possess", "possessions",
            "possessed", "holding", "have", "has", "items", "things",
            "belongings", "pack", "carried", "kept", "keep", "count",
            "counted", "owned", "owns", "belong",
        }

        purchase_terms = {
            "bought", "buy", "purchased", "purchase",
            "paid", "pay", "price", "cost", "costs",
        }

        repair_terms = {
            "fix", "fixed", "fixing", "repair", "repaired",
            "repairing", "mend", "mended", "wheel",
        }

        def has_any(words: set[str]) -> bool:
            return any(word in q for word in words)

        inventory_query = has_any(inventory_terms)
        purchase_query = has_any(purchase_terms)
        repair_query = has_any(repair_terms)

        special_query = inventory_query or purchase_query or repair_query

        # ------------------------------------------------------------
        # Candidate retrieval
        #
        # For inventory/purchase/repair questions, get ALL active
        # memories. This prevents Chroma semantic search from dropping
        # the exact memory before our intent scoring can see it.
        # ------------------------------------------------------------
        candidates: list[dict] = []

        if special_query:
            raw = self.col.get(
                where={"status": "active"},
                include=["metadatas"],
            )

            for meta in raw.get("metadatas", []):
                candidates.append({
                    "metadata": meta,
                    "sim": 0.50,
                })

        else:
            count = self.col.count()

            if count == 0:
                return []

            n_results = min(count, max(k, 20 * k))

            result = self.col.query(
                query_texts=[query],
                n_results=n_results,
                where={"status": "active"},
                include=["metadatas", "distances"],
            )

            metas = result.get("metadatas", [[]])[0]
            distances = result.get("distances", [[]])[0]

            for meta, distance in zip(metas, distances):
                candidates.append({
                    "metadata": meta,
                    "sim": 1.0 - float(distance),
                })

        # ------------------------------------------------------------
        # Query words for lexical matching
        # ------------------------------------------------------------
        stopwords = {
            "the", "a", "an", "and", "or", "but", "is", "was", "were",
            "am", "are", "be", "been", "being", "to", "of", "from",
            "for", "on", "in", "at", "by", "with", "about", "after",
            "before", "my", "me", "i", "you", "your", "did", "do",
            "does", "what", "who", "when", "where", "how", "much",
            "many", "last", "time", "night", "visit", "second",
            "first", "ask", "asked", "tell", "told", "name", "called",
        }

        query_words = {
            word.strip(".,!?;:'\"()[]{}").lower()
            for word in q.split()
        }

        query_words -= stopwords

        # ------------------------------------------------------------
        # Score candidates
        # ------------------------------------------------------------
        scored: list[dict] = []

        for candidate in candidates:
            meta = candidate["metadata"]
            sim = float(candidate.get("sim", 0.0))

            text = str(meta.get("text", "") or "")
            text_lower = text.lower()

            fact_type = str(meta.get("fact_type", "") or "").lower()
            subject = str(meta.get("subject", "") or "").lower()

            # --------------------------------------------------------
            # Ordinary semantic threshold
            #
            # Special queries deliberately bypass the threshold because
            # we retrieved every active memory and will score them using
            # their content/type.
            # --------------------------------------------------------
            if not special_query and sim < MIN_SIM:
                continue

            score = sim

            # --------------------------------------------------------
            # Lexical overlap
            # --------------------------------------------------------
            text_words = {
                word.strip(".,!?;:'\"()[]{}").lower()
                for word in text_lower.split()
            }

            overlap = query_words & text_words

            if overlap:
                score += min(0.30, 0.08 * len(overlap))

            # --------------------------------------------------------
            # Inventory intent
            # --------------------------------------------------------
            if inventory_query:
                if fact_type == "item":
                    score += 0.25

                inventory_words = {
                    "has", "have", "owns", "own", "keeps",
                    "kept", "carries", "carried", "possesses",
                    "possess", "holds", "holding", "pack",
                    "inventory", "belongings",
                }

                if any(word in text_lower for word in inventory_words):
                    score += 0.20

            # --------------------------------------------------------
            # Purchase intent
            # --------------------------------------------------------
            if purchase_query:
                if fact_type in {"item", "event"}:
                    score += 0.12

                transaction_words = {
                    "bought", "buy", "purchased", "purchase",
                    "paid", "pay", "price", "cost", "coppers",
                    "coins",
                }

                if any(word in text_lower for word in transaction_words):
                    score += 0.15

            # --------------------------------------------------------
            # Repair intent
            # --------------------------------------------------------
            if repair_query:
                if fact_type in {"promise", "event", "item"}:
                    score += 0.20

                repair_words = {
                    "fix", "fixed", "fixing",
                    "repair", "repaired", "repairing",
                    "mend", "mended", "wheel",
                }

                if any(word in text_lower for word in repair_words):
                    score += 0.25

                if fact_type == "promise":
                    score += 0.10

            # --------------------------------------------------------
            # NPC identity matching
            #
            # Keep the existing identity protection: a memory about
            # one NPC should not easily answer a question about another.
            # --------------------------------------------------------
            npc_aliases = {
                "miller": "tobb",
                "old tobb": "tobb",
                "smith": "marra",
                "herbalist": "mirra",
            }

            normalized_query = q

            for alias, canonical in npc_aliases.items():
                normalized_query = normalized_query.replace(
                    alias, canonical
                )

            if fact_type == "npc":
                if subject and subject in normalized_query:
                    score += 0.15

            # --------------------------------------------------------
            # Store the calculated score
            # --------------------------------------------------------
            hit = dict(meta)
            hit["sim"] = sim
            hit["score"] = score

            # Convert metadata values to predictable types.
            hit["night"] = meta.get("night", "?")
            hit["date"] = meta.get("date", "unknown date")
            hit["age"] = int(meta.get("age", 0) or 0)

            scored.append(hit)

        # ------------------------------------------------------------
        # Sort by score
        # ------------------------------------------------------------
        scored.sort(
            key=lambda item: float(item.get("score", 0.0)),
            reverse=True,
        )

        # ------------------------------------------------------------
        # Diversity:
        # normally keep at most two memories per subject so one NPC
        # or item does not consume the entire context window.
        # ------------------------------------------------------------
        selected: list[dict] = []
        subject_counts: dict[str, int] = {}

        for hit in scored:
            subject = str(hit.get("subject", "") or "").lower()

            if subject:
                used = subject_counts.get(subject, 0)

                if used >= 2:
                    continue

                subject_counts[subject] = used + 1

            selected.append(hit)

            if len(selected) >= k:
                break

        return selected
    # ================================================================= TODO 7
    def forget(self, subject: str) -> tuple[int, int]:
        """Delete every active or superseded row concerning the requested subject."""
        if self.col.count() == 0:
            say(f"[memory] x forgot {subject!r}: 0 rows deleted, 0 remaining")
            return 0, 0

        rows = self.col.get(include=["metadatas", "documents"])
        ids = rows.get("ids", [])
        metas = rows.get("metadatas", [])
        docs = rows.get("documents", [])

        to_delete: list[str] = []
        for row_id, meta, doc in zip(ids, metas, docs):
            meta = meta or {}
            stored_subject = str(meta.get("subject", ""))
            stored_text = str(meta.get("text", ""))
            document = str(doc or "")

            subject_match = same_subject(subject, stored_subject)
            text_match = mentions(stored_text, subject) or mentions(document, subject)
            if subject_match or text_match:
                to_delete.append(row_id)

        if to_delete:
            self.col.delete(ids=to_delete)

        # Query the store again, as required, rather than assuming deletion worked.
        remaining_rows = self.col.get(include=["metadatas", "documents"])
        remaining = 0
        for meta, doc in zip(
            remaining_rows.get("metadatas", []),
            remaining_rows.get("documents", []),
        ):
            meta = meta or {}
            stored_subject = str(meta.get("subject", ""))
            stored_text = str(meta.get("text", ""))
            document = str(doc or "")
            if (
                same_subject(subject, stored_subject)
                or mentions(stored_text, subject)
                or mentions(document, subject)
            ):
                remaining += 1

        deleted = len(to_delete)
        say(
            f"[memory] x forgot {subject!r}: "
            f"{deleted} rows deleted, {remaining} remaining"
        )
        return deleted, remaining


# ===================================================================== TODO 8

def cite(hit: dict) -> str:
    """Human-readable provenance for a recalled memory."""
    night = hit.get("night", "?")
    date = hit.get("date", "unknown date")
    age = int(hit.get("age", 0) or 0)
    day_word = "day" if age == 1 else "days"
    return f"(remembered from night {night}, {date}, {age} {day_word} ago)"
