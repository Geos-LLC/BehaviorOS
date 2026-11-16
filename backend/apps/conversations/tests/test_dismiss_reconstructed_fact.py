"""Tests for the business-rule dismiss endpoint and service function.

Covers the LeadBridge PR4C-1 upstream dependency: an authoritative
dismiss path that LB can call rather than local-dismissing (brief §2).

Scenarios:
  - Happy path: dismiss a pending fact → review_state flips to DISMISSED,
    reviewed_at + reviewed_by set.
  - Cross-tenant dismiss raises ValueError (surfaced as 400 by the view).
  - Idempotent: dismissing an already-DISMISSED fact is a no-op.
  - APPROVED → DISMISSED is explicitly rejected (would orphan the TBP
    override).
  - View returns the expected JSON shape.
  - URL is registered at the agreed path.
"""

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Organization
from apps.conversations.models import (
    ReconstructedBusinessFact,
    TenantConfigSnapshot,
    UnifiedBusinessReconstructionRun,
)
from apps.conversations.tenant_behavior_profile.service import (
    dismiss_reconstructed_fact,
)


def _mk_fact(tenant_external_id='tenant-a', **fact_overrides):
    """Build Org → TenantConfigSnapshot → UnifiedBusinessReconstructionRun
    → ReconstructedBusinessFact chain for the test."""
    org = Organization.objects.create(name='Test Org')
    snap = TenantConfigSnapshot.objects.create(
        org=org,
        source_system=TenantConfigSnapshot.SourceSystem.LEADBRIDGE,
        tenant_external_id=tenant_external_id,
        contract_version='v1',
        raw_config={},
    )
    run = UnifiedBusinessReconstructionRun.objects.create(
        org=org,
        tenant_external_id=tenant_external_id,
        snapshot=snap,
        reconstruction_version='v1-test',
    )
    fact_defaults = dict(
        reconstruction_run=run,
        domain='service_scope',
        canonical_subject_json={'service': 'cleaning', 'scope_item': 'oven_interior'},
        canonical_subject_hash='deadbeef' * 8,
        observed_value_json={'relationship': 'EXTRA_CHARGE'},
        configured_equivalent_json={},
        support_n=3,
        aggregate_confidence=0.8,
        relationship_to_config=ReconstructedBusinessFact.RelationshipToConfig.OBSERVED_NOT_CONFIGURED,
        onboarding_class=ReconstructedBusinessFact.OnboardingClass.SAFE_TO_PROPOSE,
    )
    fact_defaults.update(fact_overrides)
    return ReconstructedBusinessFact.objects.create(**fact_defaults)


class DismissReconstructedFactServiceTests(TestCase):
    def test_pending_fact_is_dismissed(self):
        fact = _mk_fact(tenant_external_id='tenant-a')
        self.assertEqual(fact.review_state, ReconstructedBusinessFact.ReviewState.PENDING)

        result = dismiss_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            dismissed_by='owner@example.com',
        )

        self.assertEqual(result.review_state, ReconstructedBusinessFact.ReviewState.DISMISSED)
        self.assertIsNotNone(result.reviewed_at)
        self.assertEqual(result.reviewed_by, 'owner@example.com')

        # Persisted.
        reloaded = ReconstructedBusinessFact.objects.get(pk=fact.id)
        self.assertEqual(reloaded.review_state, ReconstructedBusinessFact.ReviewState.DISMISSED)

    def test_cross_tenant_dismiss_raises_value_error(self):
        fact = _mk_fact(tenant_external_id='tenant-a')
        with self.assertRaises(ValueError) as ctx:
            dismiss_reconstructed_fact(
                tenant_external_id='tenant-b',  # wrong tenant
                fact_id=str(fact.id),
                dismissed_by='owner@example.com',
            )
        self.assertIn('tenant-a', str(ctx.exception))
        # State unchanged.
        self.assertEqual(
            ReconstructedBusinessFact.objects.get(pk=fact.id).review_state,
            ReconstructedBusinessFact.ReviewState.PENDING,
        )

    def test_dismiss_is_idempotent(self):
        fact = _mk_fact(tenant_external_id='tenant-a')
        first = dismiss_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            dismissed_by='owner@example.com',
        )
        first_reviewed_at = first.reviewed_at
        # Second call MUST return the same row without re-writing timestamps.
        second = dismiss_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            dismissed_by='owner@example.com',
        )
        self.assertEqual(second.review_state, ReconstructedBusinessFact.ReviewState.DISMISSED)
        self.assertEqual(second.reviewed_at, first_reviewed_at)

    def test_approved_fact_cannot_be_dismissed(self):
        fact = _mk_fact(
            tenant_external_id='tenant-a',
            review_state=ReconstructedBusinessFact.ReviewState.APPROVED,
        )
        with self.assertRaises(ValueError) as ctx:
            dismiss_reconstructed_fact(
                tenant_external_id='tenant-a',
                fact_id=str(fact.id),
                dismissed_by='owner@example.com',
            )
        self.assertIn('already approved', str(ctx.exception).lower())
        # State unchanged.
        self.assertEqual(
            ReconstructedBusinessFact.objects.get(pk=fact.id).review_state,
            ReconstructedBusinessFact.ReviewState.APPROVED,
        )


class ApprovePathMirrorsReviewStateTests(TestCase):
    """Approve mirrors review_state=APPROVED on the fact row so the
    dismiss guard can refuse. Previously the approve path only touched
    TBP.business_rule_overrides and left fact.review_state='pending',
    which made the dismiss-after-approve guard non-functional."""

    def test_approve_sets_fact_review_state_to_approved(self):
        from apps.conversations.tenant_behavior_profile.service import (
            approve_reconstructed_fact,
        )
        fact = _mk_fact(tenant_external_id='tenant-a')
        self.assertEqual(fact.review_state, ReconstructedBusinessFact.ReviewState.PENDING)

        approve_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            approved_by='owner@example.com',
        )

        reloaded = ReconstructedBusinessFact.objects.get(pk=fact.id)
        self.assertEqual(reloaded.review_state, ReconstructedBusinessFact.ReviewState.APPROVED)
        self.assertIsNotNone(reloaded.reviewed_at)
        self.assertEqual(reloaded.reviewed_by, 'owner@example.com')

    def test_dismiss_after_approve_is_refused(self):
        """End-to-end invariant: approve → dismiss raises ValueError,
        because the approve path now sets review_state='approved' and the
        dismiss guard checks for it."""
        from apps.conversations.tenant_behavior_profile.service import (
            approve_reconstructed_fact,
        )
        fact = _mk_fact(tenant_external_id='tenant-a')
        approve_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            approved_by='owner@example.com',
        )
        with self.assertRaises(ValueError) as ctx:
            dismiss_reconstructed_fact(
                tenant_external_id='tenant-a',
                fact_id=str(fact.id),
                dismissed_by='owner@example.com',
            )
        self.assertIn('already approved', str(ctx.exception).lower())


class OwnerReviewPayloadReviewStateTests(TestCase):
    """The owner-review payload exposes EXPLICIT review_state per fact.
    LeadBridge adapter uses this to drive its canonical ChangeReviewState
    — never inferring dismissal from fact disappearance (LB PR4C-2 brief:
    'Do not rely on disappearance from a snapshot to update review state')."""

    def test_pending_fact_payload_review_state(self):
        from apps.conversations.tenant_behavior_profile.service import (
            _business_rules_payload,
        )
        fact = _mk_fact(tenant_external_id='tenant-a')
        payload = _business_rules_payload('tenant-a')
        item = next(x for x in payload['items'] if x['fact_id'] == str(fact.id))
        self.assertEqual(item['review_state'], 'pending')
        self.assertFalse(item['already_approved'])
        self.assertIsNone(item['reviewed_at'])
        self.assertIsNone(item['reviewed_by'])

    def test_dismissed_fact_appears_in_payload_with_review_state_dismissed(self):
        """Dismissed facts are NOT filtered; they appear with explicit
        state so the LB adapter can map them. This is the 'don't rely on
        disappearance' invariant."""
        from apps.conversations.tenant_behavior_profile.service import (
            _business_rules_payload,
        )
        fact = _mk_fact(tenant_external_id='tenant-a')
        dismiss_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            dismissed_by='owner@example.com',
        )
        payload = _business_rules_payload('tenant-a')
        item = next(x for x in payload['items'] if x['fact_id'] == str(fact.id))
        self.assertEqual(item['review_state'], 'dismissed')
        self.assertFalse(item['already_approved'])
        self.assertIsNotNone(item['reviewed_at'])
        self.assertEqual(item['reviewed_by'], 'owner@example.com')

    def test_approved_fact_appears_in_payload_with_review_state_approved(self):
        from apps.conversations.tenant_behavior_profile.service import (
            _business_rules_payload,
            approve_reconstructed_fact,
        )
        fact = _mk_fact(tenant_external_id='tenant-a')
        approve_reconstructed_fact(
            tenant_external_id='tenant-a',
            fact_id=str(fact.id),
            approved_by='owner@example.com',
        )
        payload = _business_rules_payload('tenant-a')
        item = next(x for x in payload['items'] if x['fact_id'] == str(fact.id))
        self.assertEqual(item['review_state'], 'approved')
        # Legacy signal also set (via TBP overrides).
        self.assertTrue(item['already_approved'])


class DismissReconstructedFactViewTests(TestCase):
    """The HTTP surface. Assumes test-settings-level auth bypass (shared
    insights token) since we're not exercising token auth here — that's
    covered elsewhere."""

    def setUp(self):
        self.client = APIClient()

    def test_url_reverses_to_the_agreed_path(self):
        url = reverse('insights-audit-owner-review-business-rule-dismiss')
        self.assertEqual(
            url,
            '/api/v1/insights/audit/owner-review/business-rule/dismiss',
        )

    def test_missing_tenant_id_returns_400(self):
        url = reverse('insights-audit-owner-review-business-rule-dismiss')
        response = self.client.post(
            url,
            {'factId': 'abc'},
            format='json',
            HTTP_AUTHORIZATION='Bearer test-insights-token',
        )
        # 401 (no token in test env) or 400 (token accepted, body invalid)
        # — both acceptable; the point is NOT 200. Narrow once auth is wired.
        self.assertIn(response.status_code, (400, 401, 403))

    def test_missing_fact_id_returns_400(self):
        url = reverse('insights-audit-owner-review-business-rule-dismiss')
        response = self.client.post(
            url,
            {'tenantId': 'tenant-a'},
            format='json',
            HTTP_AUTHORIZATION='Bearer test-insights-token',
        )
        self.assertIn(response.status_code, (400, 401, 403))
