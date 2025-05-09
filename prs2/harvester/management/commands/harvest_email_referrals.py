import logging

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from harvester.utils import email_harvest_actions, harvest_unread_emails, import_harvested_refs
from sentry_sdk.crons import capture_checkin
from sentry_sdk.crons.consts import MonitorStatus


class Command(BaseCommand):
    help = "Harvest emailed referrals into the database."

    def add_arguments(self, parser):
        # Named (optional) arguments
        parser.add_argument(
            "--email-report",
            action="store_true",
            dest="email",
            default=False,
            help="Email a report of harvest actions to PRS power user group",
        )
        parser.add_argument(
            "--purge-email",
            action="store_true",
            dest="purge_email",
            required=False,
            help="Mark processed email as read and flag for deletion",
        )

    def handle(self, *args, **options):
        logger = logging.getLogger("harvester")

        # Optionally run this management command in the context of a Sentry cron monitor.
        if settings.SENTRY_CRON_HARVEST_EMAIL:
            logger.info(f"Checking in Sentry Cron Monitor: {settings.SENTRY_CRON_HARVEST_EMAIL}")
            check_in_id = capture_checkin(
                monitor_slug=settings.SENTRY_CRON_HARVEST_EMAIL,
                status=MonitorStatus.IN_PROGRESS,
            )

        actions = []

        # Download unread emails from each specifed source email address.
        for email in settings.PLANNING_EMAILS:
            if "purge_email" in options and options["purge_email"]:
                actions += harvest_unread_emails(from_email=email, purge_email=True)
            else:
                actions += harvest_unread_emails(from_email=email)

        # Import all harvested, unprocessed referrals.
        actions += import_harvested_refs()

        # Optionally email the power users group a report.
        if options["email"]:
            # Send an email to users in the "PRS power users" group.
            pu_group = Group.objects.get(name=settings.PRS_POWER_USER_GROUP)
            p_users = pu_group.user_set.filter(is_active=True)
            to_emails = [u.email for u in p_users]
            email_harvest_actions(to_emails, actions)
            logger.info("Completed, email sent")
        else:
            logger.info("Completed")

        if settings.SENTRY_CRON_HARVEST_EMAIL:
            capture_checkin(
                monitor_slug=settings.SENTRY_CRON_HARVEST_EMAIL,
                check_in_id=check_in_id,
                status=MonitorStatus.OK,
            )
