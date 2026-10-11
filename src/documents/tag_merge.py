"""
Move every reference from one or more tags onto a survivor, then delete them.

The database work is one transaction. Search updates are queued afterwards and
do not send document_updated, so an update workflow does not run.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Count

from documents.models import Document
from documents.models import SavedViewFilterRule
from documents.models import Tag
from documents.models import WorkflowAction
from documents.models import WorkflowTrigger
from documents.permissions import has_perms_owner_aware
from paperless_mail.models import MailRule

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.db.models import QuerySet

logger = logging.getLogger("paperless.tag_merge")

# Saved-view rules whose value is a comma-separated list of tag ids.
# Rule 7 is the boolean "has any tag", not an id.
TAG_ID_RULE_TYPES = (6, 17, 22)

_TRAILING_LEGAL_FORM = re.compile(r"\s+(?:e\. v\.|e\.v\.|ev)$")

_TAG_RELATIONS = (
    Document.tags,
    WorkflowTrigger.filter_has_tags,
    WorkflowTrigger.filter_has_all_tags,
    WorkflowTrigger.filter_has_not_tags,
    WorkflowAction.assign_tags,
    WorkflowAction.remove_tags,
    MailRule.assign_tags,
)


class TagMergeRefused(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_tag_name(name: str) -> str:
    """
    Case-fold, map ß to ss, collapse spaces, and drop a trailing legal form.
    """
    folded = name.replace("ß", "ss").replace("ẞ", "ss").casefold()
    collapsed = re.sub(r"\s+", " ", folded).strip()
    return _TRAILING_LEGAL_FORM.sub("", collapsed).strip()


def merge_tags(
    *,
    user: User,
    target_id: int,
    source_ids: list[int],
) -> dict:
    """
    Move references from sources onto target and delete the sources.

    Children of a source are reparented first, because deleting a tag
    cascades to its children.
    """
    unique_sources = list(dict.fromkeys(source_ids))
    if not unique_sources:
        raise TagMergeRefused("At least one source tag is required")
    if target_id in unique_sources:
        raise TagMergeRefused("The target tag cannot also be a source")

    wanted = {target_id, *unique_sources}
    found = {tag.pk: tag for tag in Tag.objects.filter(pk__in=wanted)}
    if set(found) != wanted:
        raise TagMergeRefused("One or more tags do not exist")

    target = found[target_id]
    sources = [found[pk] for pk in unique_sources]
    _refuse_unless_allowed(user, target, sources)

    with transaction.atomic():
        document_ids = _affected_document_ids(unique_sources)
        _move_relations(set(unique_sources), target.pk)
        _rewrite_saved_view_rules(set(unique_sources), target.pk)
        _reparent_children(set(unique_sources), target)
        for source_id in unique_sources:
            Tag.objects.get(pk=source_id).delete()

    _reindex_documents(document_ids)
    logger.info("Merged tags %s into %s", unique_sources, target.pk)
    return {
        "result": "OK",
        "target": target.pk,
        "sources": unique_sources,
        "documents": document_ids,
    }


def suggest_tag_merges(tags: QuerySet[Tag]) -> list[dict]:
    """
    Groups the caller might merge. Nothing is merged.
    """
    annotated = list(
        tags.annotate(_merge_document_count=Count("documents", distinct=True)),
    )
    if not annotated:
        return []

    workflow_ids = _workflow_tag_ids()
    by_owner: dict[int | None, list[Tag]] = defaultdict(list)
    for tag in annotated:
        by_owner[tag.owner_id].append(tag)

    groups: list[dict] = []
    for owner_tags in by_owner.values():
        groups.extend(_groups_for_owner(owner_tags, workflow_ids))
    groups.sort(key=lambda group: min(tag["id"] for tag in group["tags"]))
    return groups


def _refuse_unless_allowed(user: User, target: Tag, sources: list[Tag]) -> None:
    if not user.has_perm("documents.change_tag") or not user.has_perm(
        "documents.delete_tag",
    ):
        raise TagMergeRefused("Insufficient permissions", status_code=403)
    if not has_perms_owner_aware(user, "change_tag", target):
        raise TagMergeRefused("Insufficient permissions", status_code=403)
    if any(not has_perms_owner_aware(user, "delete_tag", source) for source in sources):
        raise TagMergeRefused("Insufficient permissions", status_code=403)

    owners = {target.owner_id, *(source.owner_id for source in sources)}
    if len(owners) != 1:
        raise TagMergeRefused("Tags must have the same owner")
    for source in sources:
        if source.is_ancestor_of(target):
            raise TagMergeRefused("A source tag is an ancestor of the target")


def _affected_document_ids(source_ids: list[int]) -> list[int]:
    return list(
        Document.tags.through.objects.filter(tag_id__in=source_ids)
        .values_list("document_id", flat=True)
        .distinct(),
    )


def _m2m_field_names(through) -> tuple[str, str]:
    object_field = None
    tag_field = None
    for field in through._meta.fields:
        remote = getattr(field, "related_model", None)
        if remote is None:
            continue
        if remote is Tag:
            tag_field = field.attname
        elif object_field is None:
            object_field = field.attname
    if object_field is None or tag_field is None:
        raise RuntimeError(f"Cannot resolve tag relation on {through._meta.label}")
    return object_field, tag_field


def _move_relations(source_ids: set[int], target_id: int) -> None:
    for relation in _TAG_RELATIONS:
        _move_m2m(relation.through, source_ids, target_id)


def _move_m2m(through, source_ids: set[int], target_id: int) -> None:
    object_field, tag_field = _m2m_field_names(through)
    rows = list(
        through.objects.filter(**{f"{tag_field}__in": source_ids}).values_list(
            "pk",
            object_field,
            tag_field,
        ),
    )
    if not rows:
        return

    object_ids = {row[1] for row in rows}
    already = set(
        through.objects.filter(
            **{tag_field: target_id, f"{object_field}__in": object_ids},
        ).values_list(object_field, flat=True),
    )
    delete_pks: list[int] = []
    update_pks: list[int] = []
    seen = set(already)
    for pk, object_id, _tag_id in rows:
        if object_id in seen:
            delete_pks.append(pk)
        else:
            update_pks.append(pk)
            seen.add(object_id)
    if delete_pks:
        through.objects.filter(pk__in=delete_pks).delete()
    if update_pks:
        through.objects.filter(pk__in=update_pks).update(**{tag_field: target_id})


def _rewrite_saved_view_rules(source_ids: set[int], target_id: int) -> None:
    source_text = {str(source_id) for source_id in source_ids}
    target_text = str(target_id)
    rules = SavedViewFilterRule.objects.filter(
        rule_type__in=TAG_ID_RULE_TYPES,
    ).exclude(value__isnull=True)
    for rule in rules.iterator():
        parts = [part.strip() for part in (rule.value or "").split(",") if part.strip()]
        if not any(part in source_text for part in parts):
            continue
        rewritten: list[str] = []
        seen: set[str] = set()
        for part in parts:
            replacement = target_text if part in source_text else part
            if replacement in seen:
                continue
            seen.add(replacement)
            rewritten.append(replacement)
        new_value = ",".join(rewritten)
        if new_value != rule.value:
            rule.value = new_value
            rule.save(update_fields=["value"])


def _reparent_children(source_ids: set[int], target: Tag) -> None:
    children = list(Tag.objects.filter(tn_parent_id__in=source_ids))
    children.sort(key=lambda tag: tag.get_ancestors_count(), reverse=True)
    for child in children:
        if child.pk == target.pk:
            continue
        child.set_parent(target)


def _reindex_documents(document_ids: list[int]) -> None:
    # Imported here so this module does not import tasks while tasks imports
    # models. No document_updated signal: that would run update workflows.
    from documents.caching import clear_document_caches
    from documents.tasks import index_document

    for document_id in document_ids:
        clear_document_caches(document_id)
        index_document.delay(document_id)


def _workflow_tag_ids() -> set[int]:
    relations = (
        WorkflowTrigger.filter_has_tags,
        WorkflowTrigger.filter_has_all_tags,
        WorkflowTrigger.filter_has_not_tags,
        WorkflowAction.assign_tags,
        WorkflowAction.remove_tags,
    )
    found: set[int] = set()
    for relation in relations:
        _object_field, tag_field = _m2m_field_names(relation.through)
        found.update(
            relation.through.objects.values_list(tag_field, flat=True),
        )
    return found


def _groups_for_owner(owner_tags: list[Tag], workflow_ids: set[int]) -> list[dict]:
    normalized: dict[str, list[Tag]] = defaultdict(list)
    for tag in owner_tags:
        normalized[normalize_tag_name(tag.name)].append(tag)

    grouped_ids: set[int] = set()
    exact_groups: list[list[Tag]] = []
    for members in normalized.values():
        if len(members) < 2:
            continue
        exact_groups.append(members)
        grouped_ids.update(tag.pk for tag in members)

    leftovers = sorted(
        (tag for tag in owner_tags if tag.pk not in grouped_ids),
        key=lambda tag: tag.pk,
    )
    near_groups = _greedy_pairs(leftovers)

    return [
        _group_payload(members, workflow_ids)
        for members in (*exact_groups, *near_groups)
    ]


def _greedy_pairs(tags: list[Tag]) -> list[list[Tag]]:
    pairs: list[tuple[Tag, Tag]] = []
    for index, left in enumerate(tags):
        for right in tags[index + 1 :]:
            if _is_single_nondigit_edit(left.name, right.name):
                pairs.append((left, right))

    used: set[int] = set()
    chosen: list[list[Tag]] = []
    for left, right in pairs:
        if left.pk in used or right.pk in used:
            continue
        used.add(left.pk)
        used.add(right.pk)
        chosen.append([left, right])
    return chosen


def _is_single_nondigit_edit(left_name: str, right_name: str) -> bool:
    if len(left_name) < 5 or len(right_name) < 5:
        return False
    left = normalize_tag_name(left_name)
    right = normalize_tag_name(right_name)
    if len(left) < 5 or len(right) < 5 or left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False

    if len(left) == len(right):
        diffs = [index for index, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(diffs) != 1:
            return False
        index = diffs[0]
        return not left[index].isdigit() and not right[index].isdigit()

    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    inserted = _single_insertion(longer, shorter)
    return inserted is not None and not inserted.isdigit()


def _single_insertion(longer: str, shorter: str) -> str | None:
    index = 0
    offset = 0
    inserted = None
    while index < len(longer) and offset < len(shorter):
        if longer[index] == shorter[offset]:
            index += 1
            offset += 1
            continue
        if inserted is not None:
            return None
        inserted = longer[index]
        index += 1
    if offset != len(shorter):
        return None
    if inserted is None:
        if index != len(longer) - 1:
            return None
        return longer[index]
    if index != len(longer):
        return None
    return inserted


def _group_payload(members: list[Tag], workflow_ids: set[int]) -> dict:
    ordered = sorted(members, key=lambda tag: tag.pk)
    return {
        "suggested_survivor_id": _suggested_survivor(ordered, workflow_ids),
        "tags": [_tag_payload(tag) for tag in ordered],
    }


def _suggested_survivor(members: list[Tag], workflow_ids: set[int]) -> int:
    def sort_key(tag: Tag) -> tuple:
        preferred = tag.is_inbox_tag or tag.pk in workflow_ids
        return (0 if preferred else 1, -tag._merge_document_count, tag.pk)

    return min(members, key=sort_key).pk


def _tag_payload(tag: Tag) -> dict:
    return {
        "id": tag.pk,
        "name": tag.name,
        "document_count": tag._merge_document_count,
        "is_inbox": tag.is_inbox_tag,
        "owner": tag.owner_id,
    }
