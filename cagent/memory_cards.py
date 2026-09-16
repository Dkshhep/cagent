"""用户授权的长期记忆卡片：校验、合并和 prompt 选择。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MAX_PROPOSALS = 3
MAX_CARDS_IN_PROMPT = 12
MAX_SAVED_MEMORY_TOKENS = 600
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
SECRET_PATTERN = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|secret|password|passwd|私钥|密码|令牌|密钥|"
    r"sk-[a-z0-9_-]{8,}|ghp_[a-z0-9]{8,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)"
)
UNSAFE_RULE_PATTERN = re.compile(r"(?i)(跳过审批|绕过护栏|忽略安全规则|关闭安全检查|disable approval|ignore safety rules)")
EXPLICIT_PATTERN = re.compile(r"(?i)(记住|记下|保存.*记忆|长期记忆|remember|save this|store this)")
FORGET_PATTERN = re.compile(r"(?i)(忘记|不再记住|删除.*记忆|forget)")
REFERENTIAL_MEMORY_PATTERN = re.compile(r"(?i)(记住|保存).*(刚才|上面|之前|前面).*(结论|结果|答案|决定)|remember.*(previous|earlier).*(conclusion|result|answer|decision)")
LONG_TERM_PATTERN = re.compile(r"(?i)(以后|今后|从现在起|始终|一律|默认|每次|长期|always|from now on|by default)")
REPLACE_PATTERN = re.compile(r"(?i)(改用|改为|换成|不再|不用|替换|更新|以后.*用|switch to|instead of|change to)")
TEMPORARY_PATTERN = re.compile(r"(?i)(这次|本次|暂时|临时|先用|先按|for this task|just this time)")
GLOBAL_PATTERN = re.compile(r"(?i)(所有项目|任何项目|全部项目|全局|我以后都|以后都|all projects|every project|globally)")
PROJECT_PATTERN = re.compile(r"(?i)(这个项目|当前项目|本项目|此项目|该项目|这个仓库|this project|this repo)")


class MemoryValidationError(ValueError):
    """提案与可核实用户授权不一致。"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_key(value):
    key = str(value).strip().lower().replace(" ", "_")
    if not KEY_PATTERN.fullmatch(key):
        raise MemoryValidationError("invalid_key")
    return key


def normalize_value(value):
    return re.sub(r"[\s\W_]+", "", str(value).casefold(), flags=re.UNICODE)


def same_value(left, right):
    a, b = normalize_value(left), normalize_value(right)
    if bool(re.search(r"(不|别|禁止|never|not)", a)) != bool(re.search(r"(不|别|禁止|never|not)", b)):
        return False
    if a == b:
        return True
    # 只对明确的语言值做有限同义归一；任意子串会误合并 MySQL 8/9 一类版本差异。
    for language, alternatives in (("中文", ("英文", "英语")), ("英文", ("中文", "汉语"))):
        if a == language and language in b and not any(other in b for other in alternatives):
            return True
        if b == language and language in a and not any(other in a for other in alternatives):
            return True
    return False


def has_memory_intent(text):
    text = str(text)
    return bool(EXPLICIT_PATTERN.search(text) or FORGET_PATTERN.search(text) or LONG_TERM_PATTERN.search(text))


def is_explicit_request(text):
    return bool(EXPLICIT_PATTERN.search(str(text)) or FORGET_PATTERN.search(str(text)))


def _bounded_string(value, field, limit, required=True):
    if not isinstance(value, str):
        raise MemoryValidationError(f"invalid_{field}")
    clean = value.strip()
    if (required and not clean) or len(clean) > limit:
        raise MemoryValidationError(f"invalid_{field}")
    if SECRET_PATTERN.search(clean):
        raise MemoryValidationError("secret_shaped_content")
    return clean


def _source(proposal, turns):
    turn_id = _bounded_string(proposal.get("authorization_turn_id"), "authorization_turn_id", 80)
    authorizing = turns.get(turn_id)
    if not authorizing or authorizing.get("role") != "user":
        raise MemoryValidationError("authorization_turn_not_user")
    quote = _bounded_string(proposal.get("user_quote"), "user_quote", 300)
    if quote not in str(authorizing.get("content", "")):
        raise MemoryValidationError("user_quote_not_found")
    content_ids = proposal.get("content_turn_ids")
    if not isinstance(content_ids, list) or not content_ids or len(content_ids) > 5:
        raise MemoryValidationError("invalid_content_turn_ids")
    for content_id in content_ids:
        if not isinstance(content_id, str) or content_id not in turns:
            raise MemoryValidationError("content_turn_not_found")
    includes_prior_assistant = any(turns[content_id].get("role") == "assistant" for content_id in content_ids)
    if REFERENTIAL_MEMORY_PATTERN.search(quote) and not includes_prior_assistant:
        raise MemoryValidationError("referenced_conclusion_missing")
    if includes_prior_assistant:
        if not EXPLICIT_PATTERN.search(quote):
            raise MemoryValidationError("assistant_content_without_explicit_authorization")
    return {
        "authorization_turn_id": turn_id,
        "content_turn_ids": list(dict.fromkeys(content_ids)),
        "user_quote": quote,
    }


def validate_proposal(proposal, turns):
    if not isinstance(proposal, dict):
        raise MemoryValidationError("proposal_not_object")
    allowed = {
        "op", "target_card_id", "key", "kind", "scope", "tags", "current_value",
        "display_text", "change_note", "authorization_turn_id", "content_turn_ids", "user_quote",
    }
    if set(proposal) - allowed:
        raise MemoryValidationError("unknown_proposal_field")
    op = proposal.get("op")
    if op not in {"add", "update", "forget"}:
        raise MemoryValidationError("invalid_op")
    key = normalize_key(proposal.get("key", ""))
    kind = proposal.get("kind")
    scope = proposal.get("scope")
    if kind not in {"preference", "explicit_note"} or scope not in {"project", "global"}:
        raise MemoryValidationError("invalid_kind_or_scope")
    tags = proposal.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 6 or any(not isinstance(tag, str) for tag in tags):
        raise MemoryValidationError("invalid_tags")
    tags = list(dict.fromkeys(_bounded_string(tag, "tag", 32).casefold() for tag in tags))
    source = _source(proposal, turns)
    quote = source["user_quote"]
    if scope == "global" and (PROJECT_PATTERN.search(quote) or not GLOBAL_PATTERN.search(quote)):
        raise MemoryValidationError("global_scope_not_authorized")
    if TEMPORARY_PATTERN.search(quote) and not (LONG_TERM_PATTERN.search(quote) or EXPLICIT_PATTERN.search(quote)):
        raise MemoryValidationError("temporary_instruction")
    if op == "forget":
        if not FORGET_PATTERN.search(quote):
            raise MemoryValidationError("forget_not_authorized")
        value = ""
        display = ""
        change_note = ""
    else:
        if not (LONG_TERM_PATTERN.search(quote) or EXPLICIT_PATTERN.search(quote)):
            raise MemoryValidationError("not_long_term_or_explicit")
        if kind == "explicit_note" and not EXPLICIT_PATTERN.search(quote):
            raise MemoryValidationError("explicit_note_not_authorized")
        value = _bounded_string(proposal.get("current_value"), "current_value", 160)
        display = _bounded_string(proposal.get("display_text"), "display_text", 240)
        change_note = _bounded_string(proposal.get("change_note", ""), "change_note", 240, required=False)
        if UNSAFE_RULE_PATTERN.search(value + display):
            raise MemoryValidationError("safety_override_not_allowed")
        if re.search(r"(?i)\b(stdout|stderr|traceback|exit_code)\b", value + display):
            raise MemoryValidationError("tool_log_content")
        if op == "update" and not REPLACE_PATTERN.search(quote):
            raise MemoryValidationError("replacement_not_explicit")
    target = proposal.get("target_card_id", "")
    if op in {"update", "forget"}:
        target = _bounded_string(target, "target_card_id", 80)
    elif target:
        raise MemoryValidationError("unexpected_target_card_id")
    return {
        "op": op, "target_card_id": target, "key": key, "kind": kind, "scope": scope,
        "tags": tags, "current_value": value, "display_text": display,
        "change_note": change_note, "source": source,
    }


def parse_decision(value):
    if not isinstance(value, dict) or set(value) - {"decision", "proposals", "reason"}:
        raise MemoryValidationError("invalid_decision_object")
    decision = value.get("decision")
    proposals = value.get("proposals", [])
    if decision not in {"no_change", "change", "needs_clarification"}:
        raise MemoryValidationError("invalid_decision")
    if not isinstance(proposals, list) or len(proposals) > MAX_PROPOSALS:
        raise MemoryValidationError("invalid_proposal_count")
    if (decision == "change") != bool(proposals):
        raise MemoryValidationError("decision_proposals_mismatch")
    return decision, proposals


def apply_proposal(document, proposal):
    """在已加锁的单个 store 快照上修改，返回动作与卡片。"""
    cards = document.setdefault("cards", [])
    key, scope, op = proposal["key"], proposal["scope"], proposal["op"]
    existing = next((card for card in cards if card.get("status") == "active" and card.get("key") == key and card.get("scope") == scope), None)
    stamp = utc_now()
    if op == "add":
        if existing:
            if same_value(existing.get("current_value", ""), proposal["current_value"]):
                return "no_change", existing
            raise MemoryValidationError("conflict_requires_explicit_update")
        card = {
            "id": "mem_" + uuid.uuid4().hex[:12], "key": key, "kind": proposal["kind"],
            "scope": scope, "tags": proposal["tags"], "current_value": proposal["current_value"],
            "display_text": proposal["display_text"], "change_note": proposal["change_note"],
            "status": "active", "source": proposal["source"], "created_at": stamp,
            "updated_at": stamp, "revisions": [],
        }
        cards.append(card)
        return "added", card
    if not existing or existing.get("id") != proposal["target_card_id"]:
        raise MemoryValidationError("target_card_missing_or_changed")
    if op == "update" and same_value(existing.get("current_value", ""), proposal["current_value"]):
        return "no_change", existing
    existing.setdefault("revisions", []).append({
        "previous_value": existing.get("current_value", ""),
        "previous_display_text": existing.get("display_text", ""),
        "previous_source": existing.get("source", {}),
        "replaced_by_turn_id": proposal["source"]["authorization_turn_id"],
        "changed_at": stamp,
    })
    if op == "forget":
        existing["status"] = "forgotten"
        existing["source"] = proposal["source"]
        existing["change_note"] = "用户明确要求忘记这条记忆。"
        existing["updated_at"] = stamp
        return "forgotten", existing
    existing.update({
        "kind": proposal["kind"], "tags": proposal["tags"],
        "current_value": proposal["current_value"], "display_text": proposal["display_text"],
        "change_note": proposal["change_note"], "source": proposal["source"],
        "updated_at": stamp,
    })
    return "updated", existing


def effective_cards(project_cards, global_cards):
    project = {card["key"]: card for card in project_cards if card.get("status") == "active"}
    global_only = {card["key"]: card for card in global_cards if card.get("status") == "active" and card["key"] not in project}
    return list(project.values()) + list(global_only.values())


def select_cards(cards, query, limit=MAX_CARDS_IN_PROMPT):
    """中文用双字片段，英文用词；没有相关词时仍保留少量偏好。"""
    lowered = str(query).casefold()
    cjk = re.findall(r"[\u3400-\u9fff]", lowered)
    pieces = {"".join(cjk[index:index + 2]) for index in range(len(cjk) - 1)}
    pieces.update(re.findall(r"[a-z0-9_]{2,}", lowered))

    def score(card):
        text = " ".join([card.get("key", ""), card.get("display_text", ""), *card.get("tags", [])]).casefold()
        return sum(1 for piece in pieces if piece in text) + (1 if card.get("scope") == "project" else 0)

    ranked = sorted(cards, key=lambda card: (-score(card), card.get("key", "")))
    return ranked[:limit], [card["id"] for card in ranked[limit:]]
