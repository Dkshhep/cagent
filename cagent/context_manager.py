"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass


DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_SAFETY_MARGIN_TOKENS = 2_048
DEFAULT_INPUT_HARD_BUDGET_TOKENS = DEFAULT_CONTEXT_WINDOW_TOKENS - DEFAULT_OUTPUT_RESERVE_TOKENS - DEFAULT_SAFETY_MARGIN_TOKENS
DEFAULT_SOFT_TRIGGER_RATIO = 0.60
DEFAULT_HARD_TRIGGER_RATIO = 0.90
DEFAULT_COMPRESSION_TARGET_RATIO = 0.30
DEFAULT_TOTAL_BUDGET = DEFAULT_INPUT_HARD_BUDGET_TOKENS
DEFAULT_SECTION_TARGETS = {
    "prefix": 8_000,
    "memory": 4_000,
    "relevant_memory": 4_000,
}
DEFAULT_SECTION_MAX = {
    "prefix": 16_000,
    "memory": 4_000,
    "relevant_memory": 4_000,
    "current_request": 2400,
}
DEFAULT_CURRENT_REQUEST_SOFT_MAX_TOKENS = 1600
DEFAULT_MIN_HISTORY_TOKENS = 1_500
DEFAULT_SECTION_BUDGETS = {
    **DEFAULT_SECTION_TARGETS,
    "history": 80_000,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 2_000,
    "memory": 1_000,
    "relevant_memory": 500,
    "history": DEFAULT_MIN_HISTORY_TOKENS,
}
# 当 prompt 超预算时，会优先压缩这些 section。history 是主弹性池。
DEFAULT_REDUCTION_ORDER = ("history", "relevant_memory", "memory", "prefix")
# 顺序刻意设计：稳定的 prefix + 大块 history 放前面，让缓存切点落在 history 之后；
# 易变的短期记忆（memory/file_summary 写操作时变、relevant_memory 每轮按 query 变）
# 全部挪到 history 之后、current_request 之前，避免它们击穿 history 的前缀缓存。
CHECKPOINT_SECTION = "checkpoint"
SECTION_ORDER = ("prefix", "history", CHECKPOINT_SECTION, "memory", "relevant_memory", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3
TOKEN_SAFETY_FACTOR = 1.15
HISTORY_HEAD_KEEP = 3
DISTILLATION_HISTORY_THRESHOLD = 30
DISTILLATION_TAIL_KEEP = 27
RECENT_TOOL_WINDOW = 8
TOOL_HEAD_TOKENS = 60
TOOL_TAIL_TOKENS = 80
CLIPPED_PLACEHOLDER_TEMPLATE = "[... 中间内容已裁剪，约 {tokens} token ...]"


def estimate_tokens(text):
    """Conservative provider-neutral token estimate used for prompt budgets."""
    ascii_count = 0
    cjk_count = 0
    symbol_count = 0
    for char in str(text):
        codepoint = ord(char)
        if (
            0x4E00 <= codepoint <= 0x9FFF
            or 0x3400 <= codepoint <= 0x4DBF
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            cjk_count += 1
        elif char.isascii() and (char.isalnum() or char == "_"):
            ascii_count += 1
        else:
            symbol_count += 1
    estimated = ascii_count / 4 + cjk_count / 1.5 + symbol_count / 3
    return int(math.ceil(estimated * TOKEN_SAFETY_FACTOR))


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _token_clip(text, token_limit):
    text = str(text)
    token_limit = int(token_limit)
    if token_limit <= 0:
        return ""
    if estimate_tokens(text) <= token_limit:
        return text
    if token_limit <= estimate_tokens("..."):
        return ""
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid] + "..."
        if estimate_tokens(candidate) <= token_limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _token_suffix_clip(text, token_limit):
    text = str(text)
    token_limit = int(token_limit)
    if token_limit <= 0:
        return ""
    if estimate_tokens(text) <= token_limit:
        return text
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[len(text) - mid :]
        if estimate_tokens(candidate) <= token_limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _token_head_tail_clip(text, head_tokens=TOOL_HEAD_TOKENS, tail_tokens=TOOL_TAIL_TOKENS, token_limit=None):
    text = str(text)
    full_tokens = estimate_tokens(text)
    if token_limit is None:
        token_limit = int(head_tokens) + int(tail_tokens) + estimate_tokens(CLIPPED_PLACEHOLDER_TEMPLATE.format(tokens=full_tokens))
    token_limit = int(token_limit)
    if token_limit <= 0:
        return ""
    if full_tokens <= token_limit:
        return text

    clipped_tokens = max(1, full_tokens - int(head_tokens) - int(tail_tokens))
    placeholder = CLIPPED_PLACEHOLDER_TEMPLATE.format(tokens=clipped_tokens)
    placeholder_tokens = estimate_tokens(placeholder)
    if token_limit <= placeholder_tokens:
        return _token_clip(placeholder, token_limit)

    tail_budget = min(int(tail_tokens), max(0, token_limit - placeholder_tokens))
    head_budget = min(int(head_tokens), max(0, token_limit - placeholder_tokens - tail_budget))

    def render(head_budget, tail_budget):
        head = _token_clip(text, head_budget)
        tail = _token_suffix_clip(text, tail_budget)
        parts = []
        if head:
            parts.append(head.rstrip())
        parts.append(placeholder)
        if tail:
            parts.append(tail.lstrip())
        return "\n".join(parts)

    rendered = render(head_budget, tail_budget)
    while estimate_tokens(rendered) > token_limit and (head_budget > 0 or tail_budget > 0):
        if head_budget > 0:
            head_budget -= 1
        elif tail_budget > 0:
            tail_budget -= 1
        rendered = render(head_budget, tail_budget)
    return rendered if estimate_tokens(rendered) <= token_limit else _token_clip(placeholder, token_limit)


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)

    @property
    def raw_tokens(self):
        return estimate_tokens(self.raw)

    @property
    def rendered_tokens(self):
        return estimate_tokens(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        output_reserve_tokens=DEFAULT_OUTPUT_RESERVE_TOKENS,
        safety_margin_tokens=DEFAULT_SAFETY_MARGIN_TOKENS,
        section_max=None,
        current_request_soft_max_tokens=DEFAULT_CURRENT_REQUEST_SOFT_MAX_TOKENS,
        min_history_tokens=DEFAULT_MIN_HISTORY_TOKENS,
    ):
        self.agent = agent
        self.context_window_tokens = int(context_window_tokens)
        self.output_reserve_tokens = int(output_reserve_tokens)
        self.safety_margin_tokens = int(safety_margin_tokens)
        self.total_budget = int(total_budget)
        self.prompt_token_budget = int(total_budget)
        self.current_request_soft_max_tokens = int(current_request_soft_max_tokens)
        self.min_history_tokens = int(min_history_tokens)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self.section_max = dict(DEFAULT_SECTION_MAX)
        if section_max:
            self.section_max.update({str(key): int(value) for key, value in section_max.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `CAgent.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            CHECKPOINT_SECTION: "",
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        if estimate_tokens(section_texts[CURRENT_REQUEST_SECTION]) > self.section_max["current_request"]:
            raise ValueError("current request exceeds prompt budget hard max; put large content in a file or split the request")
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts[CHECKPOINT_SECTION] = checkpoint_text
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        budgets.update(self._dynamic_section_budgets(section_texts, selected_notes))
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []
        budget_trigger = self._budget_trigger(prompt)
        prompt_token_budget = self._input_hard_budget()
        reduction_target = self._reduction_target_tokens()

        if budget_trigger != "none" and estimate_tokens(prompt) > reduction_target:
            before_budget = int(budgets.get("history", 0))
            overflow_to_target = estimate_tokens(prompt) - reduction_target
            floor = int(self.section_floors.get("history", 0))
            after_budget = max(floor, before_budget - overflow_to_target)
            if after_budget < before_budget:
                reduction_log.append(
                    {
                        "section": "history",
                        "before_chars": before_budget,
                        "after_chars": after_budget,
                        "overflow_chars": overflow_to_target,
                        "before_tokens": before_budget,
                        "after_tokens": after_budget,
                        "overflow_tokens": overflow_to_target,
                        "target_tokens": reduction_target,
                        "trigger": budget_trigger,
                    }
                )
                budgets["history"] = after_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)

        # 如果仍然超过输入硬预算，就继续按固定顺序兜底压缩。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while estimate_tokens(prompt) > prompt_token_budget:
            overflow = estimate_tokens(prompt) - prompt_token_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                target_budget = reduction_target if section == "history" and budget_trigger == "soft" else prompt_token_budget
                token_overflow = max(overflow, estimate_tokens(prompt) - target_budget)
                new_budget = max(floor, current_budget - token_overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                        "before_tokens": current_budget,
                        "after_tokens": new_budget,
                        "overflow_tokens": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
            budget_trigger=budget_trigger,
        )
        return prompt, metadata

    def _input_hard_budget(self):
        configured = int(self.context_window_tokens) - int(self.output_reserve_tokens) - int(self.safety_margin_tokens)
        return min(int(self.total_budget), configured)

    def _soft_trigger(self):
        return int(self._input_hard_budget() * DEFAULT_SOFT_TRIGGER_RATIO)

    def _hard_trigger(self):
        return int(self._input_hard_budget() * DEFAULT_HARD_TRIGGER_RATIO)

    def _reduction_target_tokens(self):
        return int(self._input_hard_budget() * DEFAULT_COMPRESSION_TARGET_RATIO)

    def _budget_trigger(self, prompt):
        tokens = estimate_tokens(prompt)
        if tokens > self._hard_trigger():
            return "hard"
        if tokens > self._soft_trigger():
            return "soft"
        return "none"

    def _dynamic_section_budgets(self, section_texts, selected_notes):
        prefix_budget = min(int(self.section_budgets.get("prefix", DEFAULT_SECTION_TARGETS["prefix"])), self.section_max["prefix"])
        memory_budget = min(int(self.section_budgets.get("memory", DEFAULT_SECTION_TARGETS["memory"])), self.section_max["memory"])
        relevant_budget = min(
            int(self.section_budgets.get("relevant_memory", DEFAULT_SECTION_TARGETS["relevant_memory"])),
            self.section_max["relevant_memory"],
        )
        request_tokens = estimate_tokens(section_texts[CURRENT_REQUEST_SECTION])
        fixed_tokens = prefix_budget + memory_budget + relevant_budget + request_tokens
        history_budget = max(self.min_history_tokens, self._input_hard_budget() - fixed_tokens)
        if "history" in self.section_budgets and self.section_budgets["history"] != DEFAULT_SECTION_BUDGETS["history"]:
            history_budget = min(history_budget, int(self.section_budgets["history"]))
        return {
            "prefix": prefix_budget,
            "memory": memory_budget,
            "relevant_memory": relevant_budget,
            "history": history_budget,
        }

    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            CHECKPOINT_SECTION: SectionRender(
                raw=section_texts[CHECKPOINT_SECTION],
                budget=len(section_texts[CHECKPOINT_SECTION]),
                rendered=section_texts[CHECKPOINT_SECTION],
                details={},
            ),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                rendered_text = _token_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_token_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if estimate_tokens(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if estimate_tokens(rendered) > budget and budget > 0:
            rendered = _token_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = estimate_tokens(header) + note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        empty_details = {
            "rendered_entries": [],
            "history_count": len(history),
            "recent_window": 0,
            "recent_start": len(history),
            "distillation_candidate_start": None,
            "distillation_candidate_end": None,
            "distillation_candidate_count": 0,
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "collapsed_duplicate_tools": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
            "head_tail_clipped_tool_count": 0,
            "recent_tool_clipped_count": 0,
            "final_fallback_clipped_count": 0,
            "compressed": False,
        }
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details=empty_details,
            )

        # M1：未超预算时直接渲染 raw transcript，保持确定性、append-only。
        # 这样不触发压缩的轮次里，history 字节逐轮稳定（只在尾部追加），
        # 前缀缓存才能命中；只有 raw 超过 budget 才启用动态压缩。
        if estimate_tokens(raw) <= budget:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=raw,
                details=empty_details,
            )

        details = self._history_compression_details(history)
        entries, stage_details = self._build_history_entries(history, compress_recent_tools=False, fallback_token_limit=None)
        details.update(stage_details)
        rendered = self._render_history_entries(entries)

        if estimate_tokens(rendered) > budget:
            entries, stage_details = self._build_history_entries(history, compress_recent_tools=True, fallback_token_limit=None)
            details.update(stage_details)
            rendered = self._render_history_entries(entries)

        if estimate_tokens(rendered) > budget:
            entries, stage_details = self._progressive_history_fallback(history, budget)
            details.update(stage_details)
            rendered = self._render_history_entries(entries)

        if estimate_tokens(rendered) > budget and budget > 0:
            rendered = _token_head_tail_clip(rendered, head_tokens=20, tail_tokens=40, token_limit=budget)
            details["absolute_history_clip"] = True
        elif budget <= 0:
            rendered = ""
            details["absolute_history_clip"] = True

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "compressed": True,
                **details,
            },
        )

    def _history_compression_details(self, history):
        candidate_start = HISTORY_HEAD_KEEP if len(history) > DISTILLATION_HISTORY_THRESHOLD else None
        candidate_end = len(history) - DISTILLATION_TAIL_KEEP if len(history) > DISTILLATION_HISTORY_THRESHOLD else None
        if candidate_start is not None and candidate_end is not None and candidate_end <= candidate_start:
            candidate_start = None
            candidate_end = None
        candidate_count = max(0, (candidate_end or 0) - (candidate_start or 0))
        recent_start = max(0, len(history) - RECENT_TOOL_WINDOW)
        return {
            "rendered_entries": [],
            "history_count": len(history),
            "recent_window": RECENT_TOOL_WINDOW,
            "recent_start": recent_start,
            "distillation_candidate_start": candidate_start,
            "distillation_candidate_end": candidate_end,
            "distillation_candidate_count": candidate_count,
            "older_entries_count": max(0, recent_start),
            "collapsed_duplicate_reads": 0,
            "collapsed_duplicate_tools": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
            "head_tail_clipped_tool_count": 0,
            "recent_tool_clipped_count": 0,
            "final_fallback_clipped_count": 0,
            "absolute_history_clip": False,
        }

    def _build_history_entries(self, history, compress_recent_tools=False, fallback_token_limit=None):
        recent_start = max(0, len(history) - RECENT_TOOL_WINDOW)
        last_tool_index = {}
        for index, item in enumerate(history):
            if item.get("role") == "tool":
                last_tool_index[self._tool_signature(item)] = index

        entries = []
        details = {
            "collapsed_duplicate_reads": 0,
            "collapsed_duplicate_tools": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
            "head_tail_clipped_tool_count": 0,
            "recent_tool_clipped_count": 0,
            "final_fallback_clipped_count": 0,
        }
        for index, item in enumerate(history):
            lines, line_details = self._render_compressed_history_item(
                item=item,
                index=index,
                recent=index >= recent_start,
                duplicate=index < last_tool_index.get(self._tool_signature(item), index) if item.get("role") == "tool" else False,
                compress_recent_tools=compress_recent_tools,
                fallback_token_limit=fallback_token_limit if index >= HISTORY_HEAD_KEEP else None,
            )
            for key, value in line_details.items():
                details[key] = details.get(key, 0) + value
            entries.append({"index": index, "recent": index >= recent_start, "lines": lines})
        details["rendered_entries"] = [line for entry in entries for line in entry["lines"]]
        return entries, details

    def _progressive_history_fallback(self, history, budget):
        entries, details = self._build_history_entries(history, compress_recent_tools=True, fallback_token_limit=None)
        if estimate_tokens(self._render_history_entries(entries)) <= budget:
            return entries, details

        recent_start = max(0, len(history) - RECENT_TOOL_WINDOW)
        last_tool_index = {}
        for index, item in enumerate(history):
            if item.get("role") == "tool":
                last_tool_index[self._tool_signature(item)] = index

        clipped_indexes = set()
        by_index = {entry["index"]: entry for entry in entries}
        for fallback_token_limit in (80, 40, 20, 8, 1):
            for index in range(HISTORY_HEAD_KEEP, len(history)):
                if estimate_tokens(self._render_history_entries([by_index[i] for i in range(len(history))])) <= budget:
                    details["rendered_entries"] = [line for i in range(len(history)) for line in by_index[i]["lines"]]
                    details["final_fallback_clipped_count"] = len(clipped_indexes)
                    return [by_index[i] for i in range(len(history))], details
                item = history[index]
                lines, line_details = self._render_compressed_history_item(
                    item=item,
                    index=index,
                    recent=index >= recent_start,
                    duplicate=index < last_tool_index.get(self._tool_signature(item), index) if item.get("role") == "tool" else False,
                    compress_recent_tools=True,
                    fallback_token_limit=fallback_token_limit,
                )
                by_index[index] = {"index": index, "recent": index >= recent_start, "lines": lines}
                clipped_indexes.add(index)
                for key, value in line_details.items():
                    if key != "final_fallback_clipped_count":
                        details[key] = details.get(key, 0) + value

        entries = [by_index[i] for i in range(len(history))]
        details["rendered_entries"] = [line for entry in entries for line in entry["lines"]]
        details["final_fallback_clipped_count"] = len(clipped_indexes)
        return entries, details

    def _render_compressed_history_item(
        self,
        item,
        index,
        recent,
        duplicate,
        compress_recent_tools=False,
        fallback_token_limit=None,
    ):
        details = {}
        if item.get("role") != "tool":
            content = str(item.get("content", ""))
            if fallback_token_limit is not None:
                details["final_fallback_clipped_count"] = 1
                content = _token_head_tail_clip(content, head_tokens=20, tail_tokens=40, token_limit=fallback_token_limit)
            return [f"[{item.get('role', 'unknown')}] {content}"], details

        prefix = self._tool_prefix(item)
        content = str(item.get("content", ""))
        if duplicate and not recent:
            if item.get("name") == "read_file":
                details["collapsed_duplicate_reads"] = 1
            else:
                details["collapsed_duplicate_tools"] = 1
            return [prefix, "[... 该工具结果已在后续读取/执行中覆盖 ...]"], details

        if fallback_token_limit is not None:
            details["final_fallback_clipped_count"] = 1
            return [prefix, _token_head_tail_clip(content, head_tokens=20, tail_tokens=40, token_limit=fallback_token_limit)], details

        if not recent:
            details["head_tail_clipped_tool_count"] = 1
            details["summarized_tool_count"] = 1
            return [prefix, _token_head_tail_clip(content)], details

        if compress_recent_tools:
            details["recent_tool_clipped_count"] = 1
            return [prefix, _token_head_tail_clip(content)], details

        return [prefix, content], details

    def _render_history_entries(self, entries):
        return "\n".join(["Transcript:", *[line for entry in entries for line in entry["lines"]]])

    def _tool_prefix(self, item):
        return f"[tool:{item.get('name', 'unknown')}] {json.dumps(item.get('args', {}), sort_keys=True)}"

    def _tool_signature(self, item):
        if item.get("role") != "tool":
            return ("non_tool",)
        name = str(item.get("name", ""))
        args = item.get("args", {}) or {}
        if name == "read_file":
            return (name, str(args.get("path", "")).strip())
        return (name, json.dumps(args, sort_keys=True))

    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                line_limit = 900
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序与 SECTION_ORDER 一致：稳定 prefix + 大块 history 在前（利于前缀缓存），
        # 易变的 memory/relevant_memory 随 current_request 一起放最后。
        return "\n\n".join(
            section
            for section in [
                rendered["prefix"].rendered,
                rendered["history"].rendered,
                rendered[CHECKPOINT_SECTION].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
            if str(section).strip()
        ).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts, budget_trigger=None):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "raw_tokens_estimated": rendered[section].raw_tokens,
                "budget_chars": rendered[section].rendered_chars,
                "budget_tokens": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
                "estimated_tokens": rendered[section].rendered_tokens,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "raw_tokens_estimated": estimate_tokens(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "budget_tokens": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "estimated_tokens": rendered[CURRENT_REQUEST_SECTION].rendered_tokens,
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_tokens_estimated": estimate_tokens(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_token_budget": self._input_hard_budget(),
            "prompt_over_budget": estimate_tokens(prompt) > self._input_hard_budget(),
            "context_window_tokens": self.context_window_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "soft_trigger_tokens": self._soft_trigger(),
            "hard_trigger_tokens": self._hard_trigger(),
            "compression_target_tokens": self._reduction_target_tokens(),
            "budget_trigger": budget_trigger or self._budget_trigger(prompt),
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "section_token_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "history_count": int(rendered["history"].details.get("history_count", 0)),
                "recent_window": int(rendered["history"].details.get("recent_window", 0)),
                "recent_start": int(rendered["history"].details.get("recent_start", 0)),
                "distillation_candidate_start": rendered["history"].details.get("distillation_candidate_start"),
                "distillation_candidate_end": rendered["history"].details.get("distillation_candidate_end"),
                "distillation_candidate_count": int(rendered["history"].details.get("distillation_candidate_count", 0)),
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "collapsed_duplicate_tools": int(rendered["history"].details.get("collapsed_duplicate_tools", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
                "head_tail_clipped_tool_count": int(rendered["history"].details.get("head_tail_clipped_tool_count", 0)),
                "recent_tool_clipped_count": int(rendered["history"].details.get("recent_tool_clipped_count", 0)),
                "final_fallback_clipped_count": int(rendered["history"].details.get("final_fallback_clipped_count", 0)),
                "absolute_history_clip": bool(rendered["history"].details.get("absolute_history_clip", False)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
