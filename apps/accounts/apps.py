from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        # Audit-log sign-ins, sign-outs and failed sign-ins.
        from . import signals  # noqa: F401
