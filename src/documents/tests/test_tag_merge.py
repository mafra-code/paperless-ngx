from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APIClient

from documents.models import SavedView
from documents.models import SavedViewFilterRule
from documents.models import Tag
from documents.models import WorkflowAction
from documents.models import WorkflowTrigger
from documents.tag_merge import TagMergeRefused
from documents.tag_merge import merge_tags
from documents.tag_merge import suggest_tag_merges
from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global
from paperless_testing.permissions import grant_object


@pytest.fixture
def no_reindex(mocker):
    mocker.patch("documents.tasks.index_document")
    mocker.patch("documents.caching.clear_document_caches")
    return mocker.patch("documents.signals.document_updated")


def _superuser() -> User:
    return UserFactory(superuser=True)


@pytest.mark.django_db
class TestTagMerge(DirectoriesMixin):
    def test_moves_every_reference_and_reparents_children(self, mocker, no_reindex):
        """
        GIVEN:
            - A target and two sources that are referenced everywhere a tag can be
            - A child of a source
        WHEN:
            - The tags are merged
        THEN:
            - Every reference points at the target, duplicates are dropped,
              the child is kept and reparented, and documents are reindexed
              without document_updated
        """
        user = _superuser()
        target = TagFactory(
            name="Keep",
            color="#112233",
            is_inbox_tag=True,
            match="keep-me",
        )
        source = TagFactory(name="Drop", color="#ffffff", match="other")
        other = TagFactory(name="Also")
        child = TagFactory(name="Child", tn_parent=source)
        grandchild = TagFactory(name="Grandchild", tn_parent=child)

        both = DocumentFactory()
        both.tags.add(source, target)
        only_sources = DocumentFactory()
        only_sources.tags.add(source, other)

        trigger = WorkflowTrigger.objects.create(
            type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
            match="",
        )
        trigger.filter_has_tags.add(source, target)
        trigger.filter_has_all_tags.add(other)
        trigger.filter_has_not_tags.add(source)

        action = WorkflowAction.objects.create(
            type=WorkflowAction.WorkflowActionType.ASSIGNMENT,
        )
        action.assign_tags.add(source)
        action.remove_tags.add(other, source)

        account = MailAccount.objects.create(
            name="merge-account",
            imap_server="imap.example.com",
            username="user",
            password="secret",
        )
        rule = MailRule.objects.create(
            name="merge-rule",
            account=account,
            action=MailRule.MailAction.MARK_READ,
            consumption_scope=MailRule.ConsumptionScope.ATTACHMENTS_ONLY,
        )
        rule.assign_tags.add(source, target)

        saved_view = SavedView.objects.create(name="merge-view")
        has_all = SavedViewFilterRule.objects.create(
            saved_view=saved_view,
            rule_type=6,
            value=f"{source.id},{target.id},{other.id}",
        )
        has_not = SavedViewFilterRule.objects.create(
            saved_view=saved_view,
            rule_type=17,
            value=f"{other.id},{source.id}",
        )
        has_any = SavedViewFilterRule.objects.create(
            saved_view=saved_view,
            rule_type=22,
            value=str(source.id),
        )
        is_tagged = SavedViewFilterRule.objects.create(
            saved_view=saved_view,
            rule_type=7,
            value=str(source.id),
        )

        index_document = mocker.patch("documents.tasks.index_document")
        clear_caches = mocker.patch("documents.caching.clear_document_caches")

        result = merge_tags(
            user=user,
            target_id=target.id,
            source_ids=[source.id, other.id],
        )

        assert result["target"] == target.id
        assert sorted(result["documents"]) == sorted([both.id, only_sources.id])
        assert not Tag.objects.filter(pk__in=[source.id, other.id]).exists()
        child.refresh_from_db()
        grandchild.refresh_from_db()
        assert child.tn_parent_id == target.id
        assert grandchild.tn_parent_id == child.id
        assert Tag.objects.filter(pk=grandchild.id).exists()

        target.refresh_from_db()
        assert target.name == "Keep"
        assert target.color == "#112233"
        assert target.is_inbox_tag is True
        assert target.match == "keep-me"

        assert set(both.tags.values_list("id", flat=True)) == {target.id}
        assert set(only_sources.tags.values_list("id", flat=True)) == {target.id}
        assert set(trigger.filter_has_tags.values_list("id", flat=True)) == {target.id}
        assert set(trigger.filter_has_all_tags.values_list("id", flat=True)) == {
            target.id,
        }
        assert set(trigger.filter_has_not_tags.values_list("id", flat=True)) == {
            target.id,
        }
        assert set(action.assign_tags.values_list("id", flat=True)) == {target.id}
        assert set(action.remove_tags.values_list("id", flat=True)) == {target.id}
        assert set(rule.assign_tags.values_list("id", flat=True)) == {target.id}

        has_all.refresh_from_db()
        has_not.refresh_from_db()
        has_any.refresh_from_db()
        is_tagged.refresh_from_db()
        assert has_all.value == str(target.id)
        assert has_not.value == str(target.id)
        assert has_any.value == str(target.id)
        assert is_tagged.value == str(source.id)

        indexed = {call.args[0] for call in index_document.delay.call_args_list}
        assert indexed == {both.id, only_sources.id}
        assert {call.args[0] for call in clear_caches.call_args_list} == indexed
        no_reindex.send.assert_not_called()

    def test_refuses_mixed_owners_and_ancestor_targets(self, no_reindex):
        """
        GIVEN:
            - Tags with different owners
            - A source that is an ancestor of the target
        WHEN:
            - Merge is requested
        THEN:
            - Both requests are refused and nothing is deleted
        """
        user = _superuser()
        owner = UserFactory()
        mine = TagFactory(name="Mine", owner=owner)
        shared = TagFactory(name="Shared")

        with pytest.raises(TagMergeRefused, match="same owner"):
            merge_tags(user=user, target_id=shared.id, source_ids=[mine.id])
        assert Tag.objects.filter(pk=mine.id).exists()

        parent = TagFactory(name="Parent")
        child = TagFactory(name="Child", tn_parent=parent)
        with pytest.raises(TagMergeRefused, match="ancestor"):
            merge_tags(user=user, target_id=child.id, source_ids=[parent.id])
        assert Tag.objects.filter(pk=parent.id).exists()
        assert Tag.objects.filter(pk=child.id).exists()

    def test_permission_checks(self, no_reindex):
        """
        GIVEN:
            - Callers missing a global permission, or object permission on one tag
        WHEN:
            - Merge is requested
        THEN:
            - The request is refused
        """
        target = TagFactory(name="Target")
        source = TagFactory(name="Source")
        stranger = UserFactory()
        grant_global(stranger, "change_tag")
        stranger = User.objects.get(pk=stranger.pk)

        with pytest.raises(TagMergeRefused, match="Insufficient") as missing_delete:
            merge_tags(user=stranger, target_id=target.id, source_ids=[source.id])
        assert missing_delete.value.status_code == 403

        owned = TagFactory(name="Owned", owner=UserFactory())
        allowed = UserFactory()
        grant_global(allowed, "change_tag", "delete_tag")
        allowed = User.objects.get(pk=allowed.pk)
        with pytest.raises(TagMergeRefused, match="Insufficient"):
            merge_tags(
                user=allowed,
                target_id=target.id,
                source_ids=[owned.id],
            )

        grant_object(allowed, owned, "delete_tag")
        merge_tags(user=allowed, target_id=target.id, source_ids=[owned.id])
        assert not Tag.objects.filter(pk=owned.id).exists()


@pytest.mark.django_db
class TestTagMergeSuggestions(DirectoriesMixin):
    def test_groups_case_and_legal_form_but_not_digits_or_chains(self):
        """
        GIVEN:
            - Case-only duplicates, an e. V. pair, year tags, a short pair,
              and a chain of one-edit names
        WHEN:
            - Suggestions are built
        THEN:
            - Case and e. V. are grouped, years and short names are not, and
              the chain is one pair rather than one group of three
        """
        invoice = TagFactory(name="Invoice")
        invoice_lower = TagFactory(name="invoice")
        verein = TagFactory(name="Verein e.V.")
        verein_spaced = TagFactory(name="Verein e. V.")
        TagFactory(name="Steuer 2023")
        TagFactory(name="Steuer 2024")
        TagFactory(name="Steuer 2025")
        TagFactory(name="Ab")
        TagFactory(name="Ac")
        hello = TagFactory(name="Hello")
        helloo = TagFactory(name="Helloo")
        TagFactory(name="Hellooo")
        report = TagFactory(name="Report")
        report_lower = TagFactory(name="report")
        TagFactory(name="Repor")

        groups = suggest_tag_merges(Tag.objects.all())
        members = [tuple(tag["id"] for tag in group["tags"]) for group in groups]

        assert (invoice.id, invoice_lower.id) in members
        assert (verein.id, verein_spaced.id) in members
        assert (hello.id, helloo.id) in members
        assert (report.id, report_lower.id) in members
        assert len(members) == 4
        assert all(len(group) == 2 for group in members)

    def test_survivor_prefers_inbox_or_workflow_then_usage_then_id(self):
        """
        GIVEN:
            - An inbox tag with fewer documents than its duplicate
            - A workflow tag
            - Two otherwise equal tags
        WHEN:
            - Suggestions are built
        THEN:
            - The survivor is the inbox or workflow tag, else the busier one,
              else the lower id
        """
        inbox = TagFactory(name="Alpha", is_inbox_tag=True)
        busy = TagFactory(name="alpha")
        for _ in range(2):
            DocumentFactory().tags.add(busy)

        used = TagFactory(name="Delta")
        TagFactory(name="delta")
        trigger = WorkflowTrigger.objects.create(
            type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
            match="",
        )
        trigger.filter_has_tags.add(used)

        first = TagFactory(name="Gamma")
        second = TagFactory(name="gamma")
        DocumentFactory().tags.add(second)
        low = TagFactory(name="Equal")
        high = TagFactory(name="equal")

        groups = {
            frozenset(tag["id"] for tag in group["tags"]): group
            for group in suggest_tag_merges(Tag.objects.all())
        }
        assert groups[frozenset({inbox.id, busy.id})]["suggested_survivor_id"] == (
            inbox.id
        )
        used_group = next(
            group
            for group in groups.values()
            if used.id in {tag["id"] for tag in group["tags"]}
        )
        assert used_group["suggested_survivor_id"] == used.id
        assert groups[frozenset({first.id, second.id})]["suggested_survivor_id"] == (
            second.id
        )
        assert groups[frozenset({low.id, high.id})]["suggested_survivor_id"] == low.id


@pytest.mark.django_db
class TestTagMergeAPI(DirectoriesMixin):
    def test_merge_and_suggestions_endpoints(self, no_reindex):
        """
        GIVEN:
            - A superuser and two tags
        WHEN:
            - Suggestions are requested and the tags are merged
        THEN:
            - Suggestions only propose, and merge deletes the source
        """
        user = _superuser()
        client = APIClient()
        client.force_authenticate(user=user)
        left = TagFactory(name="Receipt")
        right = TagFactory(name="receipt")

        suggested = client.get("/api/tags/merge_suggestions/")
        assert suggested.status_code == status.HTTP_200_OK
        assert Tag.objects.filter(pk=right.id).exists()
        ids = {
            tag["id"]
            for group in suggested.data["groups"]
            for tag in group["tags"]
        }
        assert {left.id, right.id} <= ids

        refused = client.post(
            "/api/tags/merge/",
            {"target": left.id, "sources": [left.id]},
            format="json",
        )
        assert refused.status_code == status.HTTP_400_BAD_REQUEST

        merged = client.post(
            "/api/tags/merge/",
            {"target": left.id, "sources": [right.id]},
            format="json",
        )
        assert merged.status_code == status.HTTP_200_OK
        assert not Tag.objects.filter(pk=right.id).exists()

    def test_missing_permission_is_forbidden(self, no_reindex):
        """
        GIVEN:
            - A user without delete_tag
        WHEN:
            - Merge is requested
        THEN:
            - The response is 403
        """
        user = UserFactory()
        grant_global(user, "change_tag", "view_tag")
        user = User.objects.get(pk=user.pk)
        client = APIClient()
        client.force_authenticate(user=user)
        target = TagFactory(name="Keep")
        source = TagFactory(name="Drop")

        response = client.post(
            "/api/tags/merge/",
            {"target": target.id, "sources": [source.id]},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert Tag.objects.filter(pk=source.id).exists()

    def test_selects_all_visible_tags_for_suggestions(self):
        """
        GIVEN:
            - Tags owned by different users
        WHEN:
            - Suggestions are requested
        THEN:
            - Only tags with the same owner are grouped
        """
        user = _superuser()
        client = APIClient()
        client.force_authenticate(user=user)
        other = UserFactory()
        TagFactory(name="Shared")
        TagFactory(name="shared")
        TagFactory(name="Shared", owner=other)

        response = client.get("/api/tags/merge_suggestions/")
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["groups"]) == 1
        assert response.data["groups"][0]["tags"][0]["owner"] is None
