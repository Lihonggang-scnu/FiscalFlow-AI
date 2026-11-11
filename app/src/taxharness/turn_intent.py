"""Honor an explicit request to inspect or postpone a business write."""

import re


_SPLIT = re.compile(r"[，,；;。.!！?？\n]|但是|不过|但")
_REFERENCE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", re.I)
_NEGATIVE = re.compile(r"(?:(?:不要|(?<![分识区特个性级类告离])别|无需|不必|暂不|先不|不能|不可|禁止|不需要|不许)\s*(?:把|将)?[^，,；;。.!！?？\n]{0,40}?|不(?:再|用|会)?\s*)(?:保存|写入|提交|确认|修改|更改|更新|撤销|填入|填写|清空|改|动)")
_INSPECT = re.compile(r"(?:本轮|这次|本次)?(?:只|仅)(?:是|做|进行)?(?:查看|查询|读取|检查|看看|看|预览|核对|读)")
_QUESTION = re.compile(r"(?:该|应该|怎么|如何).{0,8}?(?:填|写|改).{0,8}?(?:什么|如何|怎么)|(?:用途|备注).{0,8}?(?:是什么|填什么|写什么|怎么填|如何填)")
_DEFER = re.compile(r"暂缓|先放草稿|稍后(?:再|处理)|回头(?:核实|处理)|先等等|先别动|不要动")
_POSITIVE = re.compile(r"(?:保存|写入|提交|确认|修改|更改|更新|撤销|填入|填写|填为|填成|改为|改成|追加|清空|清除|置空|写成|写为|写)(?!的?(?:要求|规则|说明|方法|记录|流程|历史|按钮|状态))")
_EXPLICIT_POSITIVE = re.compile(r"(?:请|再|现在|直接|可以|同意|确认|继续|马上|随后)\s*(?:帮我)?\s*(?:保存|写入|提交|修改|更改|更新|撤销|填入)")
_CONDITIONAL = re.compile(r"如果|假如|倘若|仅当|只有|除非|否则|(?:(?:要是|假使).{0,40}?(?:就|才))|才(?:保存|写入|修改|填写)")


def _without_quoted_values(message: str) -> str:
    """Leave literal values intact for saving, but not as instructions or negations."""
    result = list(message)
    end = None
    for index, char in enumerate(message):
        if end is not None:
            if char == end:
                end = None
            else:
                result[index] = " "
        elif char in {'"', '“', '‘', '「', '『'}:
            end = {'"': '"', '“': '”', '‘': '’', '「': '」', '『': '』'}[char]
    return "".join(result)


def company_aliases(database) -> dict[str, list[str]]:
    return {row["company_id"]: [value for value in (row["company_id"], row["name"], row["short_name"]) if value]
            for row in database.connection.execute("SELECT company_id,name,short_name FROM companies")}


def write_restricted(message: str, identifiers: list[str], fields: list[str], *,
                     company_id: str | None = None, companies: dict[str, list[str]] | None = None) -> bool:
    """Return whether the current user explicitly forbade the requested write."""

    targets = {str(value).casefold() for value in identifiers if value}
    prohibited = False
    prior_references: set[str] = set()
    prior_companies: set[str] = set()
    for clause in _SPLIT.split(_without_quoted_values(message)):
        mentioned = {key for key, aliases in (companies or {}).items()
                     if any(alias.casefold() in clause.casefold() for alias in aliases)}
        if mentioned:
            prior_companies = mentioned
        elif prior_companies and not re.search(r"本轮|这次|本次|所有|任何|全部|一律", clause):
            mentioned = prior_companies
        if company_id and mentioned and company_id not in mentioned:
            continue
        references = {item.casefold() for item in _REFERENCE.findall(clause)}
        if references:
            prior_references = references
        elif prior_references and not re.search(r"本轮|这次|本次|所有|任何|全部|一律", clause):
            if re.search(r"其他|其余|剩余", clause):
                if prior_references & targets:
                    continue
            else:
                references = prior_references
        if references and not (references & targets):
            continue
        negative = bool(_NEGATIVE.search(clause) or _INSPECT.search(clause) or _DEFER.search(clause) or _QUESTION.search(clause))
        named_fields = {field for field, label in (("purpose", "用途"), ("remarks", "备注")) if label in clause}
        if _CONDITIONAL.search(clause) and len(named_fields) > 1:
            matches = list(_POSITIVE.finditer(clause))
            if matches:
                after_action = clause[matches[-1].end():]
                action_fields = {field for field, label in (("purpose", "用途"), ("remarks", "备注")) if label in after_action}
                if action_fields:
                    named_fields = action_fields
        only = re.search(r"(?:只|仅)(?:需|要|能|可以)?(?:修改|更改|更新|保存|填写|改|写|填)?(?:发票的?|票据的?)?(用途|备注)", clause)
        if only and ((only.group(1) == "用途" and "remarks" in fields) or (only.group(1) == "备注" and "purpose" in fields)):
            prohibited = True
            continue
        if negative:
            if named_fields and not (named_fields & set(fields)):
                continue
            prohibited = True
        elif _POSITIVE.search(clause) and (not named_fields or named_fields & set(fields)) and ((references & targets) or _EXPLICIT_POSITIVE.search(clause)):
            prohibited = False
    return prohibited


def write_requested(message: str, identifiers: list[str], fields: list[str], *,
                    company_id: str | None = None, companies: dict[str, list[str]] | None = None) -> bool:
    """Require an actual current instruction; questions and deferred facts are not decisions."""
    if not message or write_restricted(message, identifiers, fields, company_id=company_id, companies=companies):
        return False
    text = _without_quoted_values(message)
    if re.search(r"(?:该|应该|要|能)(?:怎么|如何|什么)|(?:用途|备注).{0,8}(?:填什么|写什么|怎么填|如何填)", text):
        return False
    targets = {str(value).casefold() for value in identifiers if value}
    prior_references: set[str] = set()
    prior_companies: set[str] = set()
    for clause in _SPLIT.split(text):
        mentioned = {key for key, aliases in (companies or {}).items()
                     if any(alias.casefold() in clause.casefold() for alias in aliases)}
        if mentioned:
            prior_companies = mentioned
        elif prior_companies:
            mentioned = prior_companies
        if company_id and mentioned and company_id not in mentioned:
            continue
        references = {item.casefold() for item in _REFERENCE.findall(clause)}
        if references:
            prior_references = references
        elif prior_references:
            references = prior_references
        if references and not references.intersection(targets):
            continue
        named = {field for field, label in (("purpose", "用途"), ("remarks", "备注")) if label in clause}
        if named and not named.intersection(fields):
            continue
        if _POSITIVE.search(clause) and not (_NEGATIVE.search(clause) or _INSPECT.search(clause) or _DEFER.search(clause)):
            return True
    return False


def draft_write_restricted(message: str, identifiers: list[str], fields: list[str], *,
                           company_id: str | None = None, companies: dict[str, list[str]] | None = None) -> bool:
    """A request to leave the invoice unsaved can still explicitly permit draft work."""
    if not write_restricted(message, identifiers, fields, company_id=company_id, companies=companies):
        return False
    text = _without_quoted_values(message)
    global_readonly = re.search(r"只读|不(?:要|做|进行).{0,4}(?:任何|全部|一切).{0,6}(?:修改|更改|变更|操作|写入)", text)
    draft_denied = re.search(r"(?:不要|别|不许|不能|禁止|勿).{0,12}(?:草稿|暂存|暂记|记下来)|(?:草稿|暂存).{0,8}(?:不要|别|不许|不能|禁止).{0,8}(?:改|写|动|记录|生成)", text)
    draft_requested = re.search(r"(?:更正|修改|调整|创建|生成|记录|改|写).{0,12}草稿|草稿.{0,12}(?:更正|修改|调整|创建|生成|记录|改|写)|暂存|暂记|记下来|先记|先整理", text)
    return bool(global_readonly or draft_denied or not draft_requested)


def write_conditions(message: str, identifiers: list[str], *, company_id: str | None = None,
                     companies: dict[str, list[str]] | None = None) -> tuple[bool, set[str]]:
    clauses = _SPLIT.split(_without_quoted_values(message))
    targets = {str(value).casefold() for value in identifiers if value}
    related = []
    for index, clause in enumerate(clauses):
        if not _CONDITIONAL.search(clause):
            continue
        combined = clause if _POSITIVE.search(clause) else clause + (clauses[index + 1] if index + 1 < len(clauses) else "")
        if not _POSITIVE.search(combined):
            continue
        mentioned = {key for key, aliases in (companies or {}).items()
                     if any(alias.casefold() in combined.casefold() for alias in aliases)}
        if company_id and mentioned and company_id not in mentioned:
            continue
        references = {item.casefold() for item in _REFERENCE.findall(combined)}
        if references and not (references & targets):
            continue
        related.append(combined)
    empty = set()
    for clause in related:
        for field, label in (("purpose", "用途"), ("remarks", "备注")):
            if re.search(label + r".{0,8}?(?<!不)(?:为空|是空|空白|未填|没填|没有填|还空着)", clause):
                empty.add(field)
    return bool(related), empty


def parsed_write_condition(message: str, identifiers: list[str], *, company_id: str | None = None,
                           companies: dict[str, list[str]] | None = None) -> tuple[bool, dict | None]:
    """Identify supported original conditions; an unknown condition remains for clarification."""
    conditional, _ = write_conditions(message, identifiers, company_id=company_id, companies=companies)
    if not conditional:
        return False, None
    text = _without_quoted_values(message)
    parts = []
    for field, label in (("purpose", "用途"), ("remarks", "备注")):
        match = re.search(label + r".{0,8}?(不为空|非空|不是空|已有内容|有内容|已填写|已填|仍为空|为空|是空|空白|未填写|未填|没填|没有填|还空着)", text)
        if match:
            operator = "not_empty" if match.group(1) in {"不为空", "非空", "不是空", "已有内容", "有内容", "已填写", "已填"} else "empty"
            parts.append({"field": field, "operator": operator})
    if not parts:
        return True, None
    if len(parts) == 1:
        return True, parts[0]
    if "或" in text and ("并且" in text or "同时" in text):
        return True, None
    return True, {"operator": "any" if "或" in text else "all", "conditions": parts}


def condition_supported_by_quote(condition: dict, quote: str) -> bool:
    """Check that the cited text names the condition's subject, operator and value."""
    operator = condition.get("operator")
    if operator in {"all", "any"}:
        parts = condition.get("conditions")
        conjunction = r"并且|同时|并|且|都" if operator == "all" else r"或|任一"
        return bool(isinstance(parts, list) and parts and re.search(conjunction, quote) and
                    all(condition_supported_by_quote(part, quote) for part in parts))
    label = {"purpose": "用途", "remarks": "备注", "revision": "版本"}.get(condition.get("field"))
    if not label or label not in quote:
        return False
    patterns = {"empty": r"(?<!不)为空|(?<!不)是空|空白|未填|没填",
                "not_empty": r"不为空|非空|不是空|已有内容|有内容|已填",
                "equals": r"(?<!不)等于|(?<!不)一致|相同",
                "not_equals": r"不等于|不一致|不同",
                "version_matches": r"版本"}
    pattern = patterns.get(operator)
    if not pattern or not re.search(pattern, quote):
        return False
    return operator in {"empty", "not_empty"} or str(condition.get("value")) in quote
