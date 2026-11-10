"""Honor an explicit request to inspect or postpone a business write."""

import re


_SPLIT = re.compile(r"[，,；;。.!！?？\n]|但是|不过|但")
_REFERENCE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", re.I)
_NEGATIVE = re.compile(r"(?:(?:不要|别|无需|不必|暂不|先不|不能|不可|禁止|不需要|不许)\s*(?:把|将)?[^，,；;。.!！?？\n]{0,40}?|不(?:再|用|会)?\s*)(?:保存|写入|提交|确认|修改|更改|更新|撤销|填入)")
_INSPECT = re.compile(r"(?:本轮|这次|本次)?(?:只|仅)(?:是|做|进行)?(?:查看|查询|读取|检查|看看|看|预览|核对|读)")
_POSITIVE = re.compile(r"(?:保存|写入|提交|确认|修改|更改|更新|撤销|填入|填为|改为|改成)(?!的?(?:要求|规则|说明|方法|记录|流程|历史|按钮|状态))")
_EXPLICIT_POSITIVE = re.compile(r"(?:请|再|现在|直接|可以|同意|确认|继续|马上|随后)\s*(?:帮我)?\s*(?:保存|写入|提交|修改|更改|更新|撤销|填入)")
_CONDITIONAL = re.compile(r"如果|假如|倘若|仅当|只有|除非|否则|(?:(?:要是|假使).{0,40}?(?:就|才))|才(?:保存|写入|修改|填写)")


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
    for clause in _SPLIT.split(message):
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
        negative = bool(_NEGATIVE.search(clause) or _INSPECT.search(clause))
        if negative:
            named_fields = {field for field, label in (("purpose", "用途"), ("remarks", "备注")) if label in clause}
            if named_fields and not (named_fields & set(fields)):
                continue
            prohibited = True
        elif _POSITIVE.search(clause) and ((references & targets) or _EXPLICIT_POSITIVE.search(clause)):
            prohibited = False
    return prohibited


def write_conditions(message: str, identifiers: list[str], *, company_id: str | None = None,
                     companies: dict[str, list[str]] | None = None) -> tuple[bool, set[str]]:
    clauses = _SPLIT.split(message)
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
