"""Anti-Bloat Memo & Daily Journal service built directly on `RawNote` (`note_kind='memo'|'journal'`).

Reuses `RawNote` + `MediaAttachmentService` + `IngestService` + `DreamService` without creating
isolated note/diary tables, supporting:
- Pinned Masonry/List Memos with `#tag` filtering, Markdown + Image OCR, and optional silent memory extraction
- One-page-per-Beijing-day Mood Journal with 1-click "AI 今日小结" (summarizing completed tasks & focus time)
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
import re
from typing import Any

from trace_memory.config import TraceMemoryConfig
from trace_memory.domain.notes import (
    BEIJING_TZ,
    RawNote,
    beijing_now,
    beijing_now_iso,
    beijing_today_date,
    new_id,
)
from trace_memory.domain.proposals import NotFoundError, ValidationError
from trace_memory.llm.openai_compat import _post_json_with_retry
from trace_memory.services.dream_service import DreamService
from trace_memory.services.guard_service import RateLimitAndQuotaService
from trace_memory.services.ingest_queue import IngestJobRepository, IngestWorkerPool
from trace_memory.services.ingest_service import IngestService
from trace_memory.services.lunar_calendar import solar_to_lunar
from trace_memory.services.media_service import MediaAttachmentService
from trace_memory.services.sync_and_scheduler import SpaceSyncTracker
from trace_memory.services.task_matrix_service import TaskMatrixService
from trace_memory.storage.database import Database
from trace_memory.storage.repositories import NoteRepository, _row_to_note


MOOD_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "joy", "icon": "😊", "label": "愉悦", "color": "#f59e0b", "group": "positive"},
    {"id": "focused", "icon": "🔥", "label": "充实", "color": "#ef4444", "group": "positive"},
    {"id": "celebrate", "icon": "🎉", "label": "惊喜", "color": "#ec4899", "group": "positive"},
    {"id": "grateful", "icon": "💖", "label": "感恩", "color": "#f43f5e", "group": "positive"},
    {"id": "calm", "icon": "🌿", "label": "平静", "color": "#10b981", "group": "neutral"},
    {"id": "thinking", "icon": "🤔", "label": "思考", "color": "#6366f1", "group": "neutral"},
    {"id": "meh", "icon": "😶", "label": "麻木", "color": "#94a3b8", "group": "neutral"},
    {"id": "tired", "icon": "😴", "label": "疲惫", "color": "#64748b", "group": "neutral"},
    {"id": "sad", "icon": "😔", "label": "难过", "color": "#3b82f6", "group": "negative"},
    {"id": "anxious", "icon": "😰", "label": "焦虑", "color": "#8b5cf6", "group": "negative"},
    {"id": "angry", "icon": "😠", "label": "烦躁", "color": "#dc2626", "group": "negative"},
    {"id": "low", "icon": "🌧️", "label": "低落", "color": "#475569", "group": "negative"},
)
MOOD_BY_ID: dict[str, dict[str, str]] = {m["id"]: m for m in MOOD_CATALOG}
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_HASHTAG_RE = re.compile(r"(?:^|\s)#([\w\u4e00-\u9fff-]{1,24})")


def extract_combined_tags(content: str, explicit_tags: list[str] | tuple[str, ...] | str | None = None) -> tuple[str, ...]:
    items: list[str] = []
    if isinstance(explicit_tags, str):
        for piece in re.split(r"[,，\s]+", explicit_tags):
            clean = piece.strip().lstrip("#")[:24]
            if clean:
                items.append(clean)
    elif isinstance(explicit_tags, (list, tuple)):
        for piece in explicit_tags:
            clean = str(piece).strip().lstrip("#")[:24]
            if clean:
                items.append(clean)

    for match in _HASHTAG_RE.finditer(content or ""):
        tag_txt = match.group(1).strip()[:24]
        if tag_txt:
            items.append(tag_txt)

    return tuple(dict.fromkeys(items))


def _parse_beijing_ymd(raw: str) -> date:
    clean = (raw or "").strip()[:10]
    if clean:
        try:
            return date.fromisoformat(clean)
        except ValueError as exc:
            raise ValidationError(f"日期格式错误（需为 YYYY-MM-DD）：{raw!r}") from exc
    return date.fromisoformat(beijing_today_date())


class MemoAndJournalService:
    """Unified Memo (`note_kind='memo'`) & Daily Journal (`note_kind='journal'`) service."""

    def __init__(
        self,
        *,
        db: Database,
        config: TraceMemoryConfig,
        note_repo: NoteRepository,
        ingest_service: IngestService,
        job_repo: IngestJobRepository,
        ingest_worker: IngestWorkerPool,
        media_service: MediaAttachmentService,
        task_matrix_service: TaskMatrixService,
        dream_service: DreamService | None = None,
        quota_service: RateLimitAndQuotaService | None = None,
        sync_tracker: SpaceSyncTracker | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.note_repo = note_repo
        self.ingest_service = ingest_service
        self.job_repo = job_repo
        self.ingest_worker = ingest_worker
        self.media_service = media_service
        self.task_matrix_service = task_matrix_service
        self.dream_service = dream_service
        self.quota_service = quota_service
        self.sync_tracker = sync_tracker

    def _enrich_notes(self, space_id: str, notes: list[RawNote]) -> list[dict[str, Any]]:
        if not notes:
            return []
        media_map = self.media_service.list_for_notes(space_id, [n.note_id for n in notes])
        out: list[dict[str, Any]] = []
        for n in notes:
            d = n.to_dict()
            d["media"] = media_map.get(n.note_id, [])
            d["mood_meta"] = MOOD_BY_ID.get(n.mood) if n.mood else None
            if n.journal_date:
                try:
                    jd = date.fromisoformat(n.journal_date[:10])
                    d["lunar_label"] = solar_to_lunar(jd).short_label
                    d["weekday_label"] = WEEKDAY_CN[jd.weekday()]
                except ValueError:
                    d["lunar_label"] = ""
                    d["weekday_label"] = ""
            out.append(d)
        return out

    def _dispatch_note_processing(self, space_id: str, note: RawNote, actor_id: str) -> str:
        """Run async worker queue if running, else process synchronously so tests & CLI stay deterministic."""
        if self.ingest_worker.is_running:
            job = self.job_repo.enqueue(space_id=space_id, note_id=note.note_id, actor_id=actor_id)
            self.ingest_worker.notify()
            return str(job["job_id"])
        self.ingest_service.process_persisted_note(
            space_id=space_id,
            note_id=note.note_id,
            count_note_in_quota=note.extract_memory,
        )
        return ""

    # ------------------------------------------------------------------
    # Memos (`note_kind = 'memo'` + user quick notes)
    # ------------------------------------------------------------------

    def list_memos(
        self,
        *,
        space_id: str,
        q: str = "",
        tag: str = "",
        pinned_only: bool = False,
        limit: int = 80,
        offset: int = 0,
    ) -> dict[str, Any]:
        sp = (space_id or "default").strip() or "default"
        clean_q = (q or "").strip()
        clean_tag = (tag or "").strip().lstrip("#")
        lim = max(1, min(200, int(limit or 80)))
        off = max(0, int(offset or 0))

        # Gather all non-journal, non-synthetic active notes for tag facets, search & accurate pagination
        all_rows = self.db.query_all(
            """
            SELECT * FROM raw_notes
             WHERE space_id = ? AND is_forgotten = 0
               AND note_kind IN ('memo', 'note')
               AND content NOT LIKE '[Task %'
             ORDER BY pinned DESC, updated_at DESC
            """,
            (sp,),
        )
        all_memo_notes = [_row_to_note(r) for r in all_rows]

        tag_counter: Counter[str] = Counter()
        for n in all_memo_notes:
            for t in n.tags:
                tag_counter[t] += 1

        if clean_q:
            bm25_hits = self.note_repo.search_bm25(sp, clean_q, limit=max(lim * 2, 100))
            hit_ids = {n.note_id for n, _ in bm25_hits}
            q_lower = clean_q.lower()
            candidates = [
                n
                for n in all_memo_notes
                if n.note_id in hit_ids
                or q_lower in n.title.lower()
                or q_lower in n.visible_content().lower()
                or any(q_lower in t.lower() for t in n.tags)
            ]
        else:
            candidates = all_memo_notes

        if clean_tag:
            candidates = [n for n in candidates if clean_tag in n.tags]
        if pinned_only:
            candidates = [n for n in candidates if n.pinned]

        # Ensure pinned first, then newest updated_at descending
        pinned_part = sorted([n for n in candidates if n.pinned], key=lambda x: x.updated_at, reverse=True)
        unpinned_part = sorted([n for n in candidates if not n.pinned], key=lambda x: x.updated_at, reverse=True)
        full_ordered = pinned_part + unpinned_part
        total_cnt = len(full_ordered)
        ordered = full_ordered[off : off + lim]

        return {
            "space_id": sp,
            "memos": self._enrich_notes(sp, ordered),
            "total": total_cnt,
            "limit": lim,
            "offset": off,
            "has_more": (off + len(ordered)) < total_cnt,
            "pinned_count": len(pinned_part),
            # Unfiltered facet counts so filter pills (全部 / 置顶) don't shrink to the filtered subset
            "all_count": len(all_memo_notes),
            "all_pinned_count": sum(1 for n in all_memo_notes if n.pinned),
            "tags_catalog": [{"tag": k, "count": v} for k, v in tag_counter.most_common(30)],
        }

    def save_memo(
        self,
        *,
        space_id: str,
        payload: dict[str, Any],
        actor_id: str = "user",
    ) -> dict[str, Any]:
        sp = (space_id or "default").strip() or "default"
        note_id = str(payload.get("note_id") or "").strip()
        existing = self.note_repo.get(sp, note_id) if note_id else None
        if note_id and existing is None:
            raise NotFoundError(f"备忘录 {note_id!r} 不存在或已删除")

        title = str(payload.get("title") if "title" in payload else (existing.title if existing else "")).strip()[:200]
        raw_content = str(
            payload.get("content") if "content" in payload else (existing.content if existing else "")
        ).strip()
        media_ids = tuple(str(m).strip() for m in (payload.get("media_ids") or ()) if str(m).strip())

        if not raw_content and not title and not media_ids and existing is None:
            raise ValidationError("备忘录内容或附图不能为空")
        if not raw_content:
            raw_content = title or "[图片备忘]"

        pinned = bool(payload["pinned"]) if "pinned" in payload else (existing.pinned if existing else False)
        extract_mem = (
            bool(payload["extract_memory"])
            if "extract_memory" in payload
            else (existing.extract_memory if existing else False)
        )
        explicit_tags = payload.get("tags") if "tags" in payload else (list(existing.tags) if existing else [])
        merged_tags = extract_combined_tags(raw_content, explicit_tags)
        now_iso = beijing_now_iso()

        if existing is not None:
            content_changed = (raw_content != existing.content) or (title != existing.title)
            updated_note = RawNote(
                note_id=existing.note_id,
                space_id=sp,
                content=raw_content,
                source_type="web",
                actor_id=actor_id,
                revision=existing.revision + (1 if content_changed else 0),
                is_forgotten=False,
                forgotten_spans=existing.forgotten_spans,
                event_time=existing.event_time,
                created_at=existing.created_at,
                updated_at=now_iso,
                note_kind="memo",
                title=title,
                pinned=pinned,
                tags=merged_tags,
                mood=existing.mood,
                journal_date="",
                extract_memory=extract_mem,
            )
            self.note_repo.save(updated_note)
            if media_ids:
                self.media_service.bind_to_note(sp, updated_note.note_id, media_ids)
            job_id = self._dispatch_note_processing(sp, updated_note, actor_id)
            target_note = updated_note
        else:
            new_note = RawNote(
                note_id=new_id("note"),
                space_id=sp,
                content=raw_content,
                source_type="web",
                actor_id=actor_id,
                revision=1,
                event_time=now_iso,
                created_at=now_iso,
                updated_at=now_iso,
                note_kind="memo",
                title=title,
                pinned=pinned,
                tags=merged_tags,
                extract_memory=extract_mem,
            )
            self.note_repo.save(new_note)
            if media_ids:
                self.media_service.bind_to_note(sp, new_note.note_id, media_ids)
            job_id = self._dispatch_note_processing(sp, new_note, actor_id)
            target_note = new_note

        if self.sync_tracker is not None:
            self.sync_tracker.bump(sp, reason="memo_saved")

        enriched = self._enrich_notes(sp, [target_note])[0]
        return {"memo": enriched, "job_id": job_id}

    def toggle_pin(self, *, space_id: str, note_id: str, pinned: bool) -> dict[str, Any]:
        sp = (space_id or "default").strip() or "default"
        existing = self.note_repo.get(sp, note_id)
        if existing is None:
            raise NotFoundError(f"备忘录 {note_id!r} 不存在")
        updated = RawNote(
            note_id=existing.note_id,
            space_id=sp,
            content=existing.content,
            source_type=existing.source_type,
            actor_id=existing.actor_id,
            revision=existing.revision,
            is_forgotten=existing.is_forgotten,
            forgotten_spans=existing.forgotten_spans,
            event_time=existing.event_time,
            created_at=existing.created_at,
            updated_at=beijing_now_iso(),
            note_kind="memo" if existing.note_kind == "note" else existing.note_kind,
            title=existing.title,
            pinned=bool(pinned),
            tags=existing.tags,
            mood=existing.mood,
            journal_date=existing.journal_date,
            extract_memory=existing.extract_memory,
        )
        self.note_repo.save(updated)
        if self.sync_tracker is not None:
            self.sync_tracker.bump(sp, reason="memo_pin_toggled")
        return self._enrich_notes(sp, [updated])[0]

    # ------------------------------------------------------------------
    # Daily Journal (`note_kind = 'journal'`, One-Page-Per-Day in Beijing Time)
    # ------------------------------------------------------------------

    def _collect_day_footprint(self, space_id: str, target_ymd: str) -> dict[str, Any]:
        all_tasks = self.task_matrix_service.list_sorted_tasks(
            space_id=space_id, task_status="all", sort_by="priority"
        )
        done_today: list[dict[str, Any]] = []
        open_due_today: list[dict[str, Any]] = []
        for t in all_tasks:
            p = t.get("payload") or {}
            due_ymd = str(p.get("due_at") or "")[:10]
            upd_ymd = str(t.get("updated_at") or "")[:10]
            if p.get("task_status") == "done":
                if upd_ymd == target_ymd or due_ymd == target_ymd:
                    done_today.append(
                        {
                            "memory_id": t["memory_id"],
                            "title": t["title"],
                            "quadrant": p.get("quadrant", ""),
                        }
                    )
            elif due_ymd == target_ymd:
                open_due_today.append(
                    {
                        "memory_id": t["memory_id"],
                        "title": t["title"],
                        "quadrant": p.get("quadrant", ""),
                    }
                )

        focus_rows = self.db.query_all(
            """
            SELECT session_id, task_title, mode, duration_seconds
              FROM focus_sessions
             WHERE space_id = ? AND status = 'completed' AND focus_date = ?
             ORDER BY ended_at ASC
            """,
            (space_id, target_ymd),
        )
        focus_total_sec = sum(int(r["duration_seconds"] or 0) for r in focus_rows)
        focus_minutes = round(focus_total_sec / 60) if focus_total_sec > 0 else 0
        pomo_count = sum(1 for r in focus_rows if str(r["mode"]) == "pomodoro")
        focus_topics = list(
            dict.fromkeys(str(r["task_title"] or "自由专注") for r in focus_rows if str(r["task_title"] or "").strip())
        )

        memo_rows = self.db.query_all(
            """
            SELECT note_id, title, content
              FROM raw_notes
             WHERE space_id = ? AND is_forgotten = 0
               AND note_kind IN ('memo', 'note')
               AND content NOT LIKE '[Task %'
               AND substr(event_time, 1, 10) = ?
             ORDER BY created_at ASC
             LIMIT 15
            """,
            (space_id, target_ymd),
        )
        memo_snippets = [
            (str(r["title"] or "").strip() or str(r["content"] or "").strip()[:50])
            for r in memo_rows
        ]

        return {
            "date": target_ymd,
            "completed_tasks": done_today,
            "completed_count": len(done_today),
            "open_due_tasks": open_due_today,
            "open_due_count": len(open_due_today),
            "focus_sessions_count": len(focus_rows),
            "focus_pomodoros": pomo_count,
            "focus_minutes": focus_minutes,
            "focus_topics": focus_topics[:6],
            "memo_count": len(memo_snippets),
            "memo_snippets": memo_snippets[:6],
        }

    def get_journal_overview(
        self,
        *,
        space_id: str,
        date_str: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        sp = (space_id or "default").strip() or "default"
        lim = max(1, min(120, int(limit or 30)))
        off = max(0, int(offset or 0))
        target_d = _parse_beijing_ymd(date_str)
        target_ymd = target_d.isoformat()
        lunar_d = solar_to_lunar(target_d)

        cur_row = self.db.query_one(
            """
            SELECT * FROM raw_notes
             WHERE space_id = ? AND is_forgotten = 0 AND note_kind = 'journal'
               AND journal_date = ?
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            (sp, target_ymd),
        )
        cur_entry = self._enrich_notes(sp, [_row_to_note(cur_row)])[0] if cur_row else None

        cnt_row = self.db.query_one(
            """
            SELECT COUNT(*) AS cnt FROM raw_notes
             WHERE space_id = ? AND is_forgotten = 0 AND note_kind = 'journal'
            """,
            (sp,),
        )
        total_cnt = int(cnt_row["cnt"] or 0) if cnt_row else 0

        recent_rows = self.db.query_all(
            """
            SELECT * FROM raw_notes
             WHERE space_id = ? AND is_forgotten = 0 AND note_kind = 'journal'
             ORDER BY journal_date DESC, updated_at DESC
             LIMIT ? OFFSET ?
            """,
            (sp, lim, off),
        )
        timeline = self._enrich_notes(sp, [_row_to_note(r) for r in recent_rows])

        return {
            "space_id": sp,
            "date": target_ymd,
            "weekday_label": WEEKDAY_CN[target_d.weekday()],
            "lunar_label": lunar_d.full_label,
            "current_entry": cur_entry,
            "day_footprint": self._collect_day_footprint(sp, target_ymd),
            "timeline": timeline,
            "timeline_total": total_cnt,
            "timeline_limit": lim,
            "timeline_offset": off,
            "timeline_has_more": (off + len(timeline)) < total_cnt,
            "mood_catalog": list(MOOD_CATALOG),
        }

    def save_journal(
        self,
        *,
        space_id: str,
        payload: dict[str, Any],
        actor_id: str = "user",
    ) -> dict[str, Any]:
        sp = (space_id or "default").strip() or "default"
        target_d = _parse_beijing_ymd(str(payload.get("journal_date") or payload.get("date") or ""))
        target_ymd = target_d.isoformat()

        note_id = str(payload.get("note_id") or "").strip()
        existing: RawNote | None = None
        if note_id:
            existing = self.note_repo.get(sp, note_id)
        if existing is None:
            row = self.db.query_one(
                """
                SELECT * FROM raw_notes
                 WHERE space_id = ? AND is_forgotten = 0 AND note_kind = 'journal' AND journal_date = ?
                 ORDER BY updated_at DESC LIMIT 1
                """,
                (sp, target_ymd),
            )
            if row is not None:
                existing = _row_to_note(row)

        raw_content = str(
            payload.get("content") if "content" in payload else (existing.content if existing else "")
        ).strip()
        media_ids = tuple(str(m).strip() for m in (payload.get("media_ids") or ()) if str(m).strip())
        if not raw_content and not media_ids and existing is None:
            raise ValidationError("日记正文或附图不能为空")
        if not raw_content:
            raw_content = "[今日图片日记]"

        mood = str(payload.get("mood") if "mood" in payload else (existing.mood if existing else "calm")).strip()
        if mood and mood not in MOOD_BY_ID:
            mood = "calm"
        title = str(
            payload.get("title") if "title" in payload else (existing.title if existing else f"{target_ymd} 日记")
        ).strip()[:200]
        extract_mem = (
            bool(payload["extract_memory"])
            if "extract_memory" in payload
            else (existing.extract_memory if existing else True)
        )
        explicit_tags = payload.get("tags") if "tags" in payload else (list(existing.tags) if existing else [])
        merged_tags = extract_combined_tags(raw_content, explicit_tags)
        now_iso = beijing_now_iso()
        # Anchor event_time to target_ymd in Beijing Time so temporal memory traces match the diary date!
        now_bj = beijing_now()
        event_iso = f"{target_ymd}T{now_bj.strftime('%H:%M:%S')}+08:00"

        if existing is not None:
            content_changed = raw_content != existing.content
            saved_note = RawNote(
                note_id=existing.note_id,
                space_id=sp,
                content=raw_content,
                source_type="web",
                actor_id=actor_id,
                revision=existing.revision + (1 if content_changed else 0),
                is_forgotten=False,
                forgotten_spans=existing.forgotten_spans,
                event_time=event_iso,
                created_at=existing.created_at,
                updated_at=now_iso,
                note_kind="journal",
                title=title,
                pinned=bool(payload.get("pinned", existing.pinned)),
                tags=merged_tags,
                mood=mood,
                journal_date=target_ymd,
                extract_memory=extract_mem,
            )
        else:
            saved_note = RawNote(
                note_id=new_id("note"),
                space_id=sp,
                content=raw_content,
                source_type="web",
                actor_id=actor_id,
                revision=1,
                event_time=event_iso,
                created_at=now_iso,
                updated_at=now_iso,
                note_kind="journal",
                title=title,
                pinned=bool(payload.get("pinned", False)),
                tags=merged_tags,
                mood=mood,
                journal_date=target_ymd,
                extract_memory=extract_mem,
            )

        self.note_repo.save(saved_note)
        if media_ids:
            self.media_service.bind_to_note(sp, saved_note.note_id, media_ids)
        job_id = self._dispatch_note_processing(sp, saved_note, actor_id)

        if self.sync_tracker is not None:
            self.sync_tracker.bump(sp, reason="journal_saved")

        return {
            "journal": self._enrich_notes(sp, [saved_note])[0],
            "job_id": job_id,
        }

    def generate_ai_daily_summary(
        self,
        *,
        space_id: str,
        date_str: str = "",
        draft_content: str = "",
        mood: str = "",
        trigger_dream: bool = False,
    ) -> dict[str, Any]:
        """Synthesize 'AI 今日小结' from real completed tasks, focus sessions, and memos."""
        sp = (space_id or "default").strip() or "default"
        target_d = _parse_beijing_ymd(date_str)
        target_ymd = target_d.isoformat()
        fp = self._collect_day_footprint(sp, target_ymd)

        eff_mood = (mood or "").strip()
        if not eff_mood:
            cur_row = self.db.query_one(
                """
                SELECT mood FROM raw_notes
                 WHERE space_id = ? AND is_forgotten = 0 AND note_kind = 'journal'
                   AND journal_date = ?
                 ORDER BY updated_at DESC LIMIT 1
                """,
                (sp, target_ymd),
            )
            if cur_row is not None:
                eff_mood = str(cur_row["mood"] or "").strip()
        mood_meta = MOOD_BY_ID.get(eff_mood)
        is_negative_mood = bool(mood_meta and mood_meta.get("group") == "negative")

        done_titles = [t["title"] for t in fp["completed_tasks"]]
        open_titles = [t["title"] for t in fp["open_due_tasks"]]
        focus_min = int(fp["focus_minutes"])
        pomo_cnt = int(fp["focus_pomodoros"])
        focus_topics = fp["focus_topics"]
        memos = fp["memo_snippets"]

        summary_md = ""
        llm_calls = 0
        tokens_used = 0
        used_provider = "deterministic"

        if self.config.llm_provider == "openai" and self.config.openai_api_key:
            prompt_facts: dict[str, Any] = {
                "date": target_ymd,
                "completed_tasks": done_titles,
                "open_due_tasks": open_titles,
                "focus_minutes": focus_min,
                "pomodoro_sessions": pomo_cnt,
                "focus_topics": focus_topics,
                "memos": memos,
                "user_draft": (draft_content or "").strip()[:500],
            }
            if mood_meta:
                prompt_facts["mood"] = f"{mood_meta['icon']} {mood_meta['label']}"
                prompt_facts["mood_group"] = mood_meta.get("group", "neutral")
            sys_content = (
                "你是「有迹」的温和复盘助手。请严格依据提供的当日真实完成待办、番茄专注时长与随记片段，"
                "用第一人称生成一段简洁清爽的 Markdown「✨ AI 今日小结」（120~240字，禁止编造未发生的事实）。"
            )
            if is_negative_mood:
                sys_content += (
                    "注意：用户今日选择了偏低落/消极的心情，请用更温柔、共情且接纳的语气陪伴复盘，"
                    "避免说教或苛责未完成事项，给予柔和的肯定与喘息空间。"
                )
            url = f"{self.config.openai_base_url.rstrip('/')}/chat/completions"
            req_body = {
                "model": self.config.llm_model,
                "temperature": 0.4,
                "max_tokens": 420,
                "messages": [
                    {"role": "system", "content": sys_content},
                    {
                        "role": "user",
                        "content": f"今日真实轨迹数据：\n{json.dumps(prompt_facts, ensure_ascii=False)}",
                    },
                ],
            }
            # DashScope Qwen3 hybrid-thinking models think by default; without this the summary took 45-55s
            # (first attempt hit the socket timeout, then the retry finished) instead of a few seconds.
            if "dashscope.aliyuncs.com" in self.config.openai_base_url:
                req_body["enable_thinking"] = False
            try:
                raw_resp = _post_json_with_retry(
                    url=url,
                    api_key=self.config.openai_api_key,
                    payload=req_body,
                    timeout_seconds=self.config.llm_timeout_seconds,
                )
                choices = raw_resp.get("choices") or []
                if choices:
                    content_str = str((choices[0].get("message") or {}).get("content") or "").strip()
                    if content_str:
                        summary_md = content_str
                        used_provider = "openai_compat"
                        llm_calls = 1
                        usage = raw_resp.get("usage") or {}
                        tokens_used = int(usage.get("total_tokens") or 0)
            except Exception:
                summary_md = ""

        if not summary_md:
            lines = [f"### ✨ {target_ymd} 今日小结"]
            if done_titles:
                joined_done = "、".join(f"《{t}》" for t in done_titles[:5])
                lines.append(f"- **待办推进**：今日顺利完成 {len(done_titles)} 项事项（{joined_done}）。")
            else:
                lines.append("- **待办推进**：今日暂无打卡完成的待办事项，给自己留白沉淀。")

            if focus_min > 0:
                topic_hint = f"，聚焦于{'、'.join(focus_topics[:3])}" if focus_topics else ""
                lines.append(
                    f"- **深度专注**：累计投入 **{focus_min} 分钟**（共 {fp['focus_sessions_count']} 段记录 / {pomo_cnt} 个番茄）{topic_hint}。"
                )
            else:
                lines.append("- **深度专注**：今日尚未记录番茄钟专注时段。")

            if memos:
                lines.append(f"- **灵感随记**：记录了 {len(memos)} 条备忘片段（{'；'.join(memos[:3])}）。")

            if open_titles:
                lines.append(f"- **明日衔接**：仍有 {len(open_titles)} 项今日排期事项待推进（如《{open_titles[0]}》）。")
            if is_negative_mood and mood_meta is not None:
                lines.append(
                    f"- **情绪关照**：今天选择了「{mood_meta['icon']} {mood_meta['label']}」，允许自己先慢下来、照顾好休息与感受；不必苛求事事圆满，哪怕只照顾好眼前的这一刻也很值得肯定。"
                )
            summary_md = "\n".join(lines)

        if llm_calls > 0 and self.quota_service is not None:
            self.quota_service.record_usage(f"space:{sp}", llm_calls=llm_calls, tokens_used=tokens_used)

        dream_report: dict[str, Any] | None = None
        if trigger_dream and self.dream_service is not None:
            dream_report = self.dream_service.run_dream(space_id=sp)

        return {
            "date": target_ymd,
            "summary_markdown": summary_md,
            "provider": used_provider,
            "day_footprint": fp,
            "dream_report": dream_report,
        }
