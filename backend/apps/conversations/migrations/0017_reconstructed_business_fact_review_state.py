"""Add owner-review lifecycle fields to ReconstructedBusinessFact.

Enables LeadBridge PR4C-1's unified review endpoint to dismiss a
business-rule recommendation authoritatively (brief §2 "Do not allow
local-only dismissal"). Mirrors the existing CommunicationProfileDiff
review-state pattern.

Backfill: all existing rows default to `review_state='pending'`,
which matches today's behavior (no fact has an explicit owner decision
yet). The approve path is NOT updated in this migration — it stays
as-is, so existing approved facts continue to appear as `pending` on
the fact row (the approval is still persisted on
TenantBehaviorProfile.business_rule_overrides). A follow-up PR will
mirror the `review_state='approved'` write onto the fact row after
observing this field in production for a week.

Safe to apply online: three nullable-or-defaulted additions + one
small btree index. No existing row data touched beyond the defaulted
inserts Postgres fills in on ADD COLUMN.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0016_conversation_context'),
    ]

    operations = [
        migrations.AddField(
            model_name='reconstructedbusinessfact',
            name='review_state',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending review'),
                    ('approved', 'Approved — recorded as business rule override'),
                    ('dismissed', 'Dismissed — keep default behavior'),
                ],
                default='pending',
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='reconstructedbusinessfact',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reconstructedbusinessfact',
            name='reviewed_by',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Owner email or "owner" sentinel when the actor is '
                    'not identifiable.'
                ),
                max_length=128,
            ),
        ),
        migrations.AddIndex(
            model_name='reconstructedbusinessfact',
            index=models.Index(
                fields=['reconstruction_run', 'review_state'],
                name='recfact_run_review_idx',
            ),
        ),
    ]
